"""Pid resolution for apps whose main process has a custom android:process.

`pidof <bundle_id>` only matches processes named exactly like the package, so
an app whose manifest renames its main process (e.g. android.process.acore)
looks "not running" to the exact-name check even while it runs. The fallback
matches a process by the package's uid in `ps -A -o PID,UID,NAME`, both at
capture start (open_streaming) and in the mid-run app-exited detector
(_check_app_alive).
"""

from __future__ import annotations

import pytest

from mpi.android import backend as b
from mpi.android.adb import AdbError
from mpi.backend import AppExitedError
from mpi.schema import DeviceInfo, PackageInfo, Platform

# Shaped like real toybox ps output (numeric UID column, padded header,
# kernel threads, a Secure Folder clone-user duplicate at 150xxxxx).
_PS_OUTPUT = """\
  PID   UID NAME
    1     0 init
    2     0 [kthreadd]
  354 10265 com.google.android.as
 5024 10056 android.process.acore
 5383 15010056 android.process.acore
 6001 10056 com.example.game:push
"""


# --- pure helpers -----------------------------------------------------------


def test_uid_to_android_user():
    assert b._uid_to_android_user(10056) == "u0_a56"
    assert b._uid_to_android_user(1010056) == "u10_a56"
    # System uids (system=1000 etc.) have no uN_aNN form.
    assert b._uid_to_android_user(1000) is None
    assert b._uid_to_android_user(0) is None


def test_pick_pid_prefers_custom_main_process_over_sub():
    # Both 5024 (no colon → main-looking) and 6001 (:push) share uid 10056;
    # the custom-named main process wins.
    assert b._pick_pid_by_uid(_PS_OUTPUT, "com.example.game", "10056") == 5024


def test_pick_pid_ignores_clone_user_duplicate():
    # Only the Secure Folder copy (uid 15010056) is running: uid 10056 must
    # not match it.
    ps = "  PID   UID NAME\n 5383 15010056 android.process.acore\n"
    assert b._pick_pid_by_uid(ps, "com.example.game", "10056") == 0


def test_pick_pid_accepts_username_uid_column():
    ps = "  PID   UID NAME\n 5024 u0_a56 android.process.acore\n"
    assert b._pick_pid_by_uid(ps, "com.example.game", "10056") == 5024


def test_pick_pid_prefers_bundle_sub_over_foreign_shared_uid_name():
    ps = (
        "  PID   UID NAME\n"
        "  400 10056 com.other.sharing:svc\n"
        "  500 10056 com.example.game:push\n"
    )
    assert b._pick_pid_by_uid(ps, "com.example.game", "10056") == 500


def test_pick_pid_exact_bundle_name_wins():
    ps = (
        "  PID   UID NAME\n"
        "  400 10056 android.process.acore\n"
        "  500 10056 com.example.game\n"
    )
    assert b._pick_pid_by_uid(ps, "com.example.game", "10056") == 500


def test_pick_pid_no_match_returns_zero():
    assert b._pick_pid_by_uid(_PS_OUTPUT, "com.example.game", "10999") == 0


# --- _resolve_pid: uid-authoritative resolution -----------------------------


def _patch_shell(monkeypatch, pidof, ps):
    """pidof/ps stand-ins; each may be a string result or an exception."""
    calls = []

    def fake_shell(command, serial=None, timeout=30.0):
        calls.append(command)
        result = pidof if command.startswith("pidof") else ps
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(b.adb, "shell", fake_shell)
    return calls


def test_resolve_pid_known_uid_uses_ps_not_clone_pidof(monkeypatch):
    calls = _patch_shell(monkeypatch, pidof="5383\n", ps=_PS_OUTPUT)
    assert b.AndroidBackend()._resolve_pid("S", "com.example.game", "10056") == 5024
    assert calls == ["ps -A -o PID,UID,NAME"]


def test_resolve_pid_known_uid_rejects_clone_when_selected_profile_is_absent(monkeypatch):
    ps = "  PID   UID NAME\n 5383 15010056 com.example.game\n"
    calls = _patch_shell(monkeypatch, pidof="5383\n", ps=ps)
    assert b.AndroidBackend()._resolve_pid("S", "com.example.game", "10056") == 0
    assert calls == ["ps -A -o PID,UID,NAME"]


def test_strict_resolve_pid_rejects_sibling_only_shared_uid(monkeypatch):
    ps = "  PID   UID NAME\n 400 10056 com.sibling.game\n"
    _patch_shell(monkeypatch, pidof="400\n", ps=ps)
    with pytest.raises(AdbError, match="has no target-owned process"):
        b.AndroidBackend()._resolve_pid(
            "S", "com.example.game", "10056", strict=True
        )


def test_strict_resolve_pid_prefers_exact_target_over_shared_uid_sibling(monkeypatch):
    ps = (
        "  PID   UID NAME\n"
        " 400 10056 com.sibling.game\n"
        " 500 10056 com.example.game\n"
    )
    calls = _patch_shell(monkeypatch, pidof="400\n", ps=ps)
    assert b.AndroidBackend()._resolve_pid(
        "S", "com.example.game", "10056", strict=True
    ) == 500
    assert calls == ["ps -A -o PID,UID,NAME"]


def test_strict_resolve_pid_rejects_ambiguous_custom_process(monkeypatch):
    ps = "  PID   UID NAME\n 5024 10056 android.process.acore\n"
    _patch_shell(monkeypatch, pidof="5024\n", ps=ps)
    with pytest.raises(AdbError, match="has no target-owned process"):
        b.AndroidBackend()._resolve_pid(
            "S", "com.example.game", "10056", strict=True
        )


def test_resolve_pid_without_uid_uses_pidof(monkeypatch):
    calls = _patch_shell(monkeypatch, pidof="123\n", ps=_PS_OUTPUT)
    assert b.AndroidBackend()._resolve_pid("S", "com.example.game", None) == 123
    assert not any(c.startswith("ps") for c in calls)


def test_resolve_pid_ps_failure_degrades_to_not_running(monkeypatch):
    # Ancient toolbox ps without -A -o must not masquerade as a transport
    # error — the answer is just "no process found".
    _patch_shell(monkeypatch, pidof="\n", ps=AdbError("ps: bad -o"))
    assert b.AndroidBackend()._resolve_pid("S", "com.example.game", "10056") == 0


def test_resolve_pid_transport_failure_raises(monkeypatch):
    _patch_shell(monkeypatch, pidof="123\n", ps=AdbError("device offline"))
    with pytest.raises(AdbError):
        b.AndroidBackend()._resolve_pid("S", "com.example.game", "10056", strict=True)


# --- _check_app_alive uses the same fallback --------------------------------


class _FakeBackend:
    """Duck-typed stand-in for AndroidBackend inside StreamingCaptureSession."""

    def __init__(self, resolver):
        self._resolver = resolver

    def _safe_shell(self, serial, command):
        if "_MPI_PROC_BOUNDARY_" in command:
            return "\n_MPI_PROC_BOUNDARY_\n" * 2
        if command == "dumpsys battery":
            return "level: 50"
        if "ResumedActivity" in command:
            return "com.example.game/.Main"
        return ""

    def _detect_max_refresh_hz(self, serial):
        return 60

    def _resolve_pid(self, serial, bundle_id, uid, **_kwargs):
        return self._resolver()


def _make_session(resolver):
    return b.StreamingCaptureSession(
        backend=_FakeBackend(resolver),
        serial="S",
        device=DeviceInfo(serial="S", platform=Platform.ANDROID),
        package=PackageInfo(bundle_id="com.example.game", uid="10056"),
        pid=111,
        hz=100,
        mem_total_kb=0,
        core_count=1,
        thermal_map=[],
    )


def test_check_app_alive_reattaches_via_fallback():
    session = _make_session(lambda: 222)
    session._gone_streak = 1
    session.sample(0)
    assert session.pid == 222
    assert session._gone_streak == 0
    # CPU baseline must be re-seeded for the new pid.
    assert session._last_jiffies is None and session._last_wall_ns is None


def test_check_app_alive_same_pid_resets_streak():
    session = _make_session(lambda: 111)
    session._gone_streak = 1
    session.sample(0)
    assert session.pid == 111
    assert session._gone_streak == 0


def test_check_app_alive_raises_after_two_gone_samples():
    session = _make_session(lambda: 0)
    session.sample(0)
    with pytest.raises(AppExitedError):
        session.sample(0)


def test_check_app_alive_transport_error_is_not_app_exit():
    def resolver():
        raise AdbError("device offline")

    session = _make_session(resolver)
    session._gone_streak = 1
    session.sample(0)  # must not raise, must not bump the streak
    assert session._gone_streak == 1
