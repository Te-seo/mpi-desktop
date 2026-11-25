"""No network credentials are used: transport, cancellation and privacy contracts."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from mpi.android.companion_cloud import CompanionCloudBridge, _session, _Connection


def session():
    return {"user_id": "a09f395e-5f22-4aa4-961c-757de44d6e83", "access_token": "synthetic.access.token", "expires_at": 4102444800}


def device(serial="USB", hw_serial="", connection_type="usb"):
    return SimpleNamespace(serial=serial, hw_serial=hw_serial, connection_type=connection_type)


class Connection:
    def __init__(self, serial):
        self.serial = serial
        self.closed = False
        self.calls = []
    def sync(self, value):
        self.calls.append(value)
        return True
    def close(self):
        self.closed = True


def test_no_session_never_probes_devices():
    bridge = CompanionCloudBridge()
    def fail():
        raise AssertionError("no device scan after logout")
    assert bridge.sync(None, fail) == {"connected": 0, "signed_in": 0, "unavailable": 0}


@pytest.mark.parametrize("change", [
    {"refresh_token": "never-transfer"}, {"expires_at": True}, {"expires_at": float("nan")},
    {"expires_at": 1}, {"user_id": "not-uuid"}, {"access_token": "has whitespace value"},
])
def test_invalid_or_refresh_bearing_payload_is_rejected(change):
    value = {**session(), **change}
    with pytest.raises(ValueError, match="invalid desktop session"):
        _session(value)


def test_reuses_socket_and_deduplicates_usb_wifi_by_hardware_id():
    created = []
    bridge = CompanionCloudBridge(lambda serial: created.append(Connection(serial)) or created[-1])
    devices = lambda: [device("10.0.0.1:5555", "USB", "wireless"), device("USB")]
    assert bridge.sync(session(), devices) == {"connected": 1, "signed_in": 1, "unavailable": 0}
    bridge.sync(session(), devices)
    assert len(created) == 1
    assert created[0].serial == "USB"
    assert len(created[0].calls) == 2
    bridge.sync(session(), lambda: [])
    assert created[0].closed


def test_logout_closes_connection_without_waiting_for_pending_update():
    started, closed = threading.Event(), threading.Event()
    class Pending(Connection):
        def sync(self, _value):
            started.set()
            assert closed.wait(2)
            return True
        def close(self):
            super().close()
            closed.set()
    bridge = CompanionCloudBridge(Pending)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(bridge.sync, session(), lambda: [device()])
        assert started.wait(2)
        assert bridge.clear()["signed_in"] == 0
        assert pending.result(timeout=2)["signed_in"] == 0


def test_logout_during_socket_open_discards_late_connection_before_token_send():
    started, resume = threading.Event(), threading.Event()
    connection = Connection("USB")
    def connect(_serial):
        started.set()
        assert resume.wait(2)
        return connection
    bridge = CompanionCloudBridge(connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(bridge.sync, session(), lambda: [device()])
        assert started.wait(2)
        bridge.clear()
        resume.set()
        assert pending.result(timeout=2)["signed_in"] == 0
    assert connection.closed
    assert connection.calls == []


def test_errors_are_counts_only_and_do_not_echo_bearer_token():
    def connect(_serial):
        raise RuntimeError(session()["access_token"])
    result = CompanionCloudBridge(connect).sync(session(), lambda: [device()])
    assert result == {"connected": 1, "signed_in": 0, "unavailable": 1}
    assert session()["access_token"] not in str(result)


def test_unchanged_token_heartbeats_and_pending_verification_does_not_repeat_auth():
    class Rpc:
        def __init__(self):
            self.calls = []
            self.ready = False
        def call(self, command, args=None):
            self.calls.append((command, args))
            if command == "cloud_challenge": return {"nonce": "a" * 64}
            if command == "cloud_login": return {"accepted": True}
            return {"signed_in": self.ready, "pending": not self.ready}
    connection = _Connection.__new__(_Connection)
    connection.fingerprint = None
    connection.rpc = Rpc()
    assert connection.sync(session()) is False
    assert connection.sync(session()) is False
    connection.rpc.ready = True
    assert connection.sync(session()) is True
    assert [cmd for cmd, _ in connection.rpc.calls] == ["cloud_challenge", "cloud_login", "cloud_heartbeat", "cloud_heartbeat"]
    payload = connection.rpc.calls[1][1]
    assert set(payload) == {"nonce", "user_id", "access_token", "expires_at"}


def test_logout_before_validation_finishes_rejects_stale_session(monkeypatch):
    from mpi.android import companion_cloud
    entered, resume = threading.Event(), threading.Event()
    original = companion_cloud._session
    created = []
    def validate(raw):
        entered.set()
        assert resume.wait(2)
        return original(raw)
    monkeypatch.setattr(companion_cloud, "_session", validate)
    bridge = CompanionCloudBridge(lambda serial: created.append(Connection(serial)) or created[-1])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(bridge.sync, session(), lambda: [device()])
        assert entered.wait(2)
        bridge.clear()
        resume.set()
        assert pending.result(timeout=2)["signed_in"] == 0
    assert created == []


def test_stdin_logout_fences_login_still_waiting_in_generic_worker_pool(monkeypatch):
    import io, json
    from concurrent.futures import Future
    from mpi import api
    from mpi.android import companion_cloud
    created, replies, queued = [], [], []
    bridge = CompanionCloudBridge(lambda serial: created.append(Connection(serial)) or created[-1])
    class DelayedPool:
        def __init__(self, **kwargs): pass
        def submit(self, fn, *args):
            future = Future()
            queued.append((fn, args, future))
            return future
        def shutdown(self, **kwargs):
            for fn, args, future in queued:
                fn(*args)
                future.set_result(None)
    requests = [
        {"id": "old-login", "command": "sync_companion_cloud", "payload": {"session": session()}},
        {"id": "logout", "command": "sync_companion_cloud", "payload": {"session": None}},
    ]
    monkeypatch.setattr(companion_cloud, "bridge", bridge)
    monkeypatch.setattr(api, "ThreadPoolExecutor", DelayedPool)
    monkeypatch.setattr(api.sys, "stdin", io.StringIO("\n".join(json.dumps(r) for r in requests)))
    monkeypatch.setattr(api, "_emit", replies.append)
    monkeypatch.setattr(api, "_shutdown_captures", lambda: None)
    monkeypatch.setattr(api, "_backend_for_platform", lambda _: SimpleNamespace(list_devices=lambda: [device()]))
    assert api.main() == 0
    assert len(queued) == 1
    assert replies[0]["id"] == "logout"
    assert all(reply["result"]["signed_in"] == 0 for reply in replies)
    assert created == []
