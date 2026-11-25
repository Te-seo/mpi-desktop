"""Unit tests for the PC-side companion client (mpi.android.companion)
and the adb wrapper additions it relies on. No device required — adb and the
on-device socket are mocked."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from mpi.android import adb, companion


@pytest.mark.parametrize(("usb_state", "package_reply", "expected"), [
    ("device", "package:com.mpi.companion\n", "installed"),
    ("device", "", "companion_not_installed"),
    ("device", "package:com.mpi.companion.desktop\n", "companion_not_installed"),
    ("unauthorized", "", "usb_unauthorized"),
    ("offline", "", "device_unreachable"),
])
def test_play_presence_only_reads_exact_package_after_usb_is_authorized(monkeypatch, usb_state, package_reply, expected):
    commands = []
    def run(args, *, serial, timeout):
        commands.append(args)
        return usb_state
    def shell(command, *, serial, timeout):
        commands.append(command)
        return package_reply
    monkeypatch.setattr(adb, "run", run)
    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr(adb, "install", lambda *a, **k: pytest.fail("must never install"))
    monkeypatch.setattr(adb, "uninstall", lambda *a, **k: pytest.fail("must never uninstall"))
    assert companion.status("S1")["state"] == expected
    assert commands == [["get-state"]] + (["pm list packages com.mpi.companion"] if usb_state == "device" else [])


def test_play_presence_does_not_label_unauthorized_as_missing_when_adb_errors(monkeypatch):
    def unauthorized(*a, **k):
        raise adb.AdbError("device unauthorized")
    monkeypatch.setattr(adb, "run", unauthorized)
    assert companion.status("S1")["state"] == "usb_unauthorized"


# --- adb wrapper additions -----------------------------------------------


def test_adb_install_builds_expected_args(monkeypatch):
    seen = {}

    def fake_run(args, serial=None, timeout=30.0):
        seen["args"] = args
        seen["serial"] = serial
        return "Success"

    monkeypatch.setattr(adb, "run", fake_run)
    adb.install("/tmp/app.apk", serial="S1")
    assert seen["args"] == ["install", "-r", "-g", "/tmp/app.apk"]
    assert seen["serial"] == "S1"


def test_adb_forward_returns_port(monkeypatch):
    monkeypatch.setattr(adb, "run", lambda args, serial=None, timeout=15.0: "41234\n")
    assert adb.forward("tcp:0", "localabstract:mpi_companion", serial="S1") == "41234"


def test_adb_forward_remove_swallows_errors(monkeypatch):
    def boom(args, serial=None, timeout=15.0):
        raise adb.AdbError("no such rule")

    monkeypatch.setattr(adb, "run", boom)
    # Should not raise.
    companion.adb.forward_remove("tcp:41234", serial="S1")


# --- socket RPC framing ---------------------------------------------------


def _serve_once(server_side: socket.socket, response: dict, captured: dict):
    reader = server_side.makefile("r", encoding="utf-8", newline="\n")
    line = reader.readline()
    captured["request"] = json.loads(line)
    server_side.sendall((json.dumps(response) + "\n").encode("utf-8"))


def test_rpc_call_roundtrip():
    client_side, server_side = socket.socketpair()
    captured: dict = {}
    try:
        t = threading.Thread(
            target=_serve_once,
            args=(server_side, {"ok": True, "data": [{"package": "com.x"}]}, captured),
        )
        t.start()
        rpc = companion._Rpc(client_side)
        data = rpc.call("list_apps", {"user_only": True})
        t.join(timeout=5)
        assert captured["request"] == {"cmd": "list_apps", "args": {"user_only": True}}
        assert data == [{"package": "com.x"}]
    finally:
        client_side.close()
        server_side.close()


def test_rpc_call_raises_on_error_response():
    client_side, server_side = socket.socketpair()
    captured: dict = {}
    try:
        t = threading.Thread(
            target=_serve_once, args=(server_side, {"ok": False, "error": "boom"}, captured)
        )
        t.start()
        rpc = companion._Rpc(client_side)
        with pytest.raises(companion.CompanionError, match="boom"):
            rpc.call("metrics", {"package": "com.x"})
        t.join(timeout=5)
    finally:
        client_side.close()
        server_side.close()


# --- _try_connect liveness (reject adb half-open) -------------------------
#
# `adb forward` accepts the local TCP even when nothing is listening on the
# device's abstract socket yet, then EOFs. _try_connect must reject that dead
# half-open (the on-device bug where `ping` got an immediate EOF) while still
# accepting a live, idle server that simply sends nothing.


def _listen_localhost():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def test_try_connect_accepts_live_idle_server():
    srv, port = _listen_localhost()
    held: list = []

    def accept():
        conn, _ = srv.accept()
        held.append(conn)  # keep it open and idle

    threading.Thread(target=accept, daemon=True).start()
    sock = companion._try_connect(port, 2.0)
    try:
        assert sock is not None  # idle is not dead
    finally:
        if sock:
            sock.close()
        for c in held:
            c.close()
        srv.close()


def test_try_connect_rejects_dead_half_open():
    srv, port = _listen_localhost()

    def accept():
        conn, _ = srv.accept()
        conn.close()  # immediate EOF, like adb with nothing bound on-device

    threading.Thread(target=accept, daemon=True).start()
    sock = companion._try_connect(port, 2.0)
    try:
        assert sock is None  # dead half-open must be rejected
    finally:
        if sock:
            sock.close()
        srv.close()


def test_missing_companion_activity_fails_without_socket_retry(monkeypatch):
    removed = []
    monkeypatch.setattr(adb, "forward", lambda *a, **k: "41234")
    monkeypatch.setattr(companion, "_try_connect", lambda *a, **k: None)
    monkeypatch.setattr(
        adb,
        "shell",
        lambda *a, **k: (
            "Starting: Intent { cmp=com.mpi.companion/.MainActivity }\n"
            "Error type 3\n"
            "Error: Activity class does not exist."
        ),
    )
    monkeypatch.setattr(
        adb, "forward_remove", lambda local, serial=None: removed.append((local, serial))
    )
    monkeypatch.setattr(
        companion.time,
        "sleep",
        lambda _delay: pytest.fail("missing companion must not enter retry sleep"),
    )

    with pytest.raises(companion.CompanionError, match="not installed"):
        with companion.connect("S1", allow_cold_start=True):
            pytest.fail("absent app must not establish a connection")

    assert removed == [("tcp:41234", "S1")]


@pytest.mark.parametrize("sdk,overlay,notification,can_start", [
    (32, "allow", "-1", True),
    (36, "allow", "0", True),
    (36, "allow", "-1", False),
    (36, "deny", "0", False),
])
def test_legacy_capture_bootstrap_requires_grants(monkeypatch, sdk, overlay, notification, can_start):
    launched = []

    def shell(command, serial, timeout):
        if command.startswith("am start "):
            component = command.split()[-1]
            launched.append(component)
            if component == companion.CAPTURE_ACTIVITY:
                raise adb.AdbError("Error type 3: Activity class does not exist")
            return "Starting"
        if command.startswith("cmd appops get "):
            return f"SYSTEM_ALERT_WINDOW: {overlay}"
        if command == "getprop ro.build.version.sdk":
            return str(sdk)
        assert command.startswith("dumpsys package check-permission android.permission.POST_NOTIFICATIONS")
        return notification

    monkeypatch.setattr(adb, "shell", shell)
    if can_start:
        companion._start_capture_bridge("S1")
    else:
        with pytest.raises(companion.CompanionError, match="use Connect"):
            companion._start_capture_bridge("S1")
    assert launched == [companion.CAPTURE_ACTIVITY] + ([companion.MAIN_ACTIVITY] if can_start else [])


# --- capture HUD ----------------------------------------------------------


def _overlay_session(monkeypatch, removed=None):
    """An OverlaySession wired to a socketpair, with the adb teardown captured."""
    monkeypatch.setattr(
        adb,
        "forward_remove",
        lambda local, serial=None: (removed.append((local, serial)) if removed is not None else None),
    )
    client_side, server_side = socket.socketpair()
    session = companion.OverlaySession(
        "S1", client_side, "tcp:41234", package="com.example.game"
    )
    return session, server_side


def test_overlay_session_never_cold_starts_when_forbidden(monkeypatch):
    removed = []
    monkeypatch.setattr(adb, "forward", lambda *a, **k: "41234")
    monkeypatch.setattr(companion, "_try_connect", lambda *a, **k: None)
    monkeypatch.setattr(
        companion,
        "ensure_running",
        lambda serial: pytest.fail("a warm connect must never `am start` the companion"),
    )
    monkeypatch.setattr(
        adb, "forward_remove", lambda local, serial=None: removed.append((local, serial))
    )

    with pytest.raises(companion.CompanionError, match="cold start"):
        companion.open_overlay_session("S1", allow_cold_start=False)

    assert removed == [("tcp:41234", "S1")]  # no leaked forward rule


def test_overlay_update_is_non_blocking_and_swallows_transport_errors(monkeypatch):
    session, server_side = _overlay_session(monkeypatch)
    pushed = threading.Event()

    class _DeadTransport:
        def call(self, cmd, args=None):
            pushed.set()
            raise companion.CompanionError("device gone")

    session._rpc = _DeadTransport()
    try:
        started = time.perf_counter()
        for _ in range(20):
            session.update({"fps": 60.0, "cpu": 12.0, "gpu": None, "temp": None})
        # The capture pump samples every 0.5 s; pushing must cost it nothing.
        assert time.perf_counter() - started < 0.25
        assert pushed.wait(timeout=5)

        # Repeated failures make the session give up instead of spinning.
        deadline = time.monotonic() + 5
        while session.alive and time.monotonic() < deadline:
            session.update({"fps": 60.0, "cpu": 12.0, "gpu": None, "temp": None})
            time.sleep(0.01)
        assert not session.alive
        session.update({"fps": 1.0, "cpu": None, "gpu": None, "temp": None})  # still no raise
    finally:
        session.close()
        server_side.close()


@pytest.mark.parametrize("lose_login_during_capture", [False, True])
def test_overlay_waits_for_desktop_login_on_its_original_socket(monkeypatch, lose_login_during_capture):
    client, server = socket.socketpair()
    authorized = threading.Event()
    if lose_login_during_capture:
        authorized.set()
    denied = threading.Event()
    retried = threading.Event()
    updated = threading.Event()
    calls = []
    opens = []
    removed = []

    def open_socket(serial, timeout, start_if_cold):
        opens.append((serial, start_if_cold))
        return client, "tcp:41234"

    def serve():
        with server, server.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                request = json.loads(line)
                cmd, args = request["cmd"], request["args"]
                accepted = authorized.is_set() or cmd == "ping" or args.get("recording") is False
                calls.append((cmd, args, accepted, time.monotonic()))
                if accepted:
                    data = {"app": "mpi-companion", "protocol": 5} if cmd == "ping" else {"enabled": args.get("enabled")}
                    reply = {"ok": True, "data": data}
                    if cmd == "overlay_update":
                        updated.set()
                else:
                    reply = {"ok": False, "error": "desktop-login-required"}
                    denied.set()
                    if sum(not allowed for _, _, allowed, _ in calls) >= 4:
                        retried.set()
                server.sendall((json.dumps(reply) + "\n").encode())

    monkeypatch.setattr(companion, "_open_forwarded_socket", open_socket)
    monkeypatch.setattr(companion, "_OVERLAY_LOGIN_RETRY_SEC", 0.03)
    monkeypatch.setattr(adb, "forward_remove", lambda local, serial=None: removed.append(local))
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    session = companion.open_overlay_session("S1", enabled=True, allow_cold_start=False, session_id="capture-id")
    try:
        if lose_login_during_capture:
            assert session.hud_enabled
            authorized.clear()
            session.update({"elapsed": 1.0})
        assert denied.wait(2)
        assert retried.wait(2)
        assert session.alive and session.login_pending and not session.hud_enabled
        assert session.configure(enabled=False, metrics=["cpu"])["reason"] == "companion_sign_in_required"
        session.configure(enabled=True, metrics=["fps"])
        for elapsed in range(2, 15):
            session.update({"elapsed": elapsed})
        authorized.set()
        assert updated.wait(2)
        assert session.hud_enabled and not session.login_pending
    finally:
        session.close()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert opens == [("S1", None)] and removed == ["tcp:41234"]
    assert all(args["session_id"] == "capture-id" for cmd, args, _, _ in calls if cmd != "ping")
    denied_configs = [at for cmd, _, allowed, at in calls if cmd == "overlay_config" and not allowed]
    assert all(b - a >= 0.025 for a, b in zip(denied_configs, denied_configs[1:]))
    assert [cmd for cmd, _, _, _ in calls[-3:]] == ["overlay_config", "overlay_update", "overlay_config"]
    assert calls[-3][1]["enabled"] and calls[-3][1]["metrics"] == ["fps"]
    assert calls[-2][1]["values"] == {"elapsed": 14}
    assert calls[-1][1]["enabled"] is False and calls[-1][1]["recording"] is False


def test_overlay_close_cancels_pending_login(monkeypatch):
    session, server = _overlay_session(monkeypatch)
    monkeypatch.setattr(companion, "_OVERLAY_LOGIN_RETRY_SEC", 60.0)
    calls = []

    def serve():
        with server, server.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                request = json.loads(line)
                calls.append(request)
                server.sendall(b'{"ok":false,"error":"desktop-login-required"}\n')

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        session.configure()
        session.update({"elapsed": 1.0})
        assert session.login_pending
    finally:
        session.close()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert not session.login_pending
    assert [(r["cmd"], r["args"]["enabled"], r["args"]["recording"]) for r in calls] == [
        ("overlay_config", True, True), ("overlay_config", False, False),
    ]


def test_overlay_close_is_idempotent_and_removes_forward(monkeypatch):
    removed = []
    session, server_side = _overlay_session(monkeypatch, removed)
    # Answer the disable-on-close config so close() doesn't wait out the timeout.
    threading.Thread(
        target=_serve_once,
        args=(server_side, {"ok": True, "data": {"enabled": False}}, {}),
        daemon=True,
    ).start()
    try:
        session.close()
        session.close()
        session.close()
        assert removed == [("tcp:41234", "S1")]
        assert not session.alive
    finally:
        server_side.close()


def test_mobile_session_streams_with_hud_disabled_and_keeps_session_id(monkeypatch):
    session, server_side = _overlay_session(monkeypatch)
    calls = []
    updated = threading.Event()

    class Recorder:
        def call(self, cmd, args=None):
            calls.append((cmd, dict(args or {})))
            if cmd == "overlay_update":
                updated.set()
            return {"enabled": bool((args or {}).get("enabled"))}

    session._rpc = Recorder()
    try:
        session.configure(enabled=False)
        session.update({"elapsed": 1.0, "fps": 60.0})
        assert updated.wait(3)
        session.configure(enabled=True)
        session.configure(enabled=False)
        session.close()
        assert all(args["session_id"] == session.session_id for _, args in calls)
        configs = [args for cmd, args in calls if cmd == "overlay_config"]
        assert [args["recording"] for args in configs] == [True, True, True, False]
        assert configs[-1]["enabled"] is False
        assert any(cmd == "overlay_update" for cmd, _ in calls)
    finally:
        session.close()
        server_side.close()


def test_mobile_session_close_drains_latest_sample_before_recording_end(monkeypatch):
    session, server_side = _overlay_session(monkeypatch)
    calls = []
    first_push = threading.Event()
    release_push = threading.Event()

    class SlowRecorder:
        def call(self, cmd, args=None):
            calls.append((cmd, dict(args or {})))
            if cmd == "overlay_update" and len(calls) == 1:
                first_push.set()
                assert release_push.wait(3)
            return {"enabled": False}

    session._rpc = SlowRecorder()
    closer = threading.Thread(target=session.close)
    try:
        session.update({"fps": 1.0})
        assert first_push.wait(3)
        session.update({"fps": 2.0})
        session.update({"fps": 3.0})
        closer.start()
        deadline = time.monotonic() + 3
        while not session._closed and time.monotonic() < deadline:
            time.sleep(0.001)
        assert session._closed
        release_push.set()
        closer.join(3)
        assert not closer.is_alive()
        assert [cmd for cmd, _ in calls] == ["overlay_update", "overlay_update", "overlay_config"]
        assert [args["values"]["fps"] for cmd, args in calls if cmd == "overlay_update"] == [1.0, 3.0]
        assert calls[-1][1]["recording"] is False
    finally:
        release_push.set()
        if closer.ident is not None:
            closer.join(3)
        session.close()
        server_side.close()


def test_mobile_session_rejects_old_protocol_and_releases_connection(monkeypatch):
    client_side, server_side = socket.socketpair()
    removed, captured = [], {}
    monkeypatch.setattr(companion, "_open_forwarded_socket", lambda *a: (client_side, "tcp:41234"))
    monkeypatch.setattr(adb, "forward_remove", lambda local, serial=None: removed.append(local))
    responder = threading.Thread(
        target=_serve_once,
        args=(server_side, {"ok": True, "data": {"app": "mpi-companion", "protocol": 3}}, captured),
    )
    responder.start()
    try:
        with pytest.raises(companion.CompanionError, match="too old"):
            companion.open_overlay_session("S1", enabled=False, allow_cold_start=False)
        responder.join(3)
        assert captured["request"]["cmd"] == "ping"
        assert removed == ["tcp:41234"]
        assert client_side.fileno() == -1
    finally:
        client_side.close()
        server_side.close()


def test_overlay_configure_refused_after_close(monkeypatch):
    # A toggle can land after the capture's epilogue tore the session down. By
    # then the socket and the forward rule are gone, so a re-enable would leave a
    # HUD on the phone that nothing is left to dismiss — the companion has no
    # launcher entry.
    removed = []
    session, server_side = _overlay_session(monkeypatch, removed)
    threading.Thread(
        target=_serve_once,
        args=(server_side, {"ok": True, "data": {"enabled": False}}, {}),
        daemon=True,
    ).start()
    try:
        session.close()

        class _NoWire:
            def call(self, cmd, args=None):
                raise AssertionError("a closed session must not talk to the device")

        session._rpc = _NoWire()
        with pytest.raises(companion.CompanionError, match="closed"):
            session.configure(enabled=True, metrics=["fps"])

        assert session.enabled is False
        assert session.hud_enabled is False
        assert not session.alive
        assert removed == [("tcp:41234", "S1")]
    finally:
        server_side.close()


def test_overlay_configure_refused_once_close_starts(monkeypatch):
    # close() raises the flag before it reaches for the wire lock, so a toggle
    # that arrives mid-teardown drops out instead of queueing behind the disable
    # and re-enabling the HUD after it.
    session, server_side = _overlay_session(monkeypatch)
    calls = []

    class _Recorder:
        def call(self, cmd, args=None):
            calls.append((cmd, dict(args or {})))
            return {"enabled": bool((args or {}).get("enabled"))}

    session._rpc = _Recorder()
    try:
        session.configure(enabled=True, metrics=["fps", "cpu"])
        assert session.hud_enabled is True
        assert len(calls) == 1

        session._closed = True  # close() in flight, disable not sent yet
        with pytest.raises(companion.CompanionError, match="closed"):
            session.configure(enabled=True, metrics=["fps"])

        assert len(calls) == 1  # nothing reached the device
        assert session.metrics == ["fps", "cpu"]  # nor was the request recorded
    finally:
        session._closed = False
        session.close()
        server_side.close()


def test_overlay_support_reports_missing_companion(monkeypatch):
    monkeypatch.setattr(companion, "status", lambda serial: {"installed": False, "state": "companion_not_installed"})
    monkeypatch.setattr(
        companion,
        "connect",
        lambda *a, **k: pytest.fail("must not open a socket to an absent companion"),
    )

    result = companion.overlay_support("S1")

    assert result["installed"] is False
    assert result["supported"] is False
    assert result["reason"] == "companion_not_installed"


def test_overlay_support_reports_old_companion(monkeypatch):
    from contextlib import contextmanager

    class _OldRpc:
        def call(self, cmd, args=None):
            raise companion.CompanionError(f"companion error on '{cmd}': unknown cmd: {cmd}")

    @contextmanager
    def fake_connect(serial, connect_timeout=6.0, allow_cold_start=True):
        yield _OldRpc()

    monkeypatch.setattr(companion, "status", lambda serial: {"installed": True, "state": "installed"})
    monkeypatch.setattr(companion, "connect", fake_connect)

    result = companion.overlay_support("S1")

    assert result["installed"] is True
    assert result["supported"] is False
    assert result["protocol"] is None
    assert result["reason"] == "companion_too_old"


def test_overlay_read_is_warm_only_by_default_and_preserves_auth_gate(monkeypatch):
    from contextlib import contextmanager
    flags = []
    class _Locked:
        def call(self, command):
            assert command == "overlay_status"
            raise companion.CompanionError("desktop-login-required")
    @contextmanager
    def connect(serial, *, connect_timeout, allow_cold_start):
        flags.append(allow_cold_start)
        yield _Locked()
    monkeypatch.setattr(companion, "status", lambda serial: {"installed": True})
    monkeypatch.setattr(companion, "connect", connect)
    assert companion.overlay_support("S1")["reason"] == "companion_sign_in_required"
    companion.overlay_support("S1", allow_cold_start=True)
    assert flags == [False, True]


def test_background_play_bridge_queries_do_not_launch_the_app(monkeypatch):
    from contextlib import contextmanager
    calls = []
    class _Rpc:
        def call(self, command, args=None):
            calls.append(command)
    @contextmanager
    def connect(serial, *, connect_timeout, allow_cold_start):
        assert serial == "S1"
        assert allow_cold_start is False
        yield _Rpc()
    monkeypatch.setattr(companion, "connect", connect)
    companion.fetch_app_list("S1")
    companion.fetch_icons("S1", [])
    companion.fetch_metrics("S1")
    companion.fetch_capabilities("S1")
    assert calls == ["list_apps", "get_icons", "metrics", "capabilities"]
