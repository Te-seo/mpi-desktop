"""Ephemeral Electron-main access-token bridge to connected MPI companions.

No refresh token, password, token file, or renderer response is involved. A null
update can cancel in-flight socket work without waiting for the normal sync lock.
"""
from __future__ import annotations

import hashlib
import math
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable

from . import adb, companion


def _session(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"user_id", "access_token", "expires_at"}:
        raise ValueError("invalid desktop session")
    user = raw.get("user_id")
    token = raw.get("access_token")
    expiry = raw.get("expires_at")
    if not isinstance(user, str):
        raise ValueError("invalid desktop session")
    try:
        uuid.UUID(user)
    except ValueError:
        raise ValueError("invalid desktop session") from None
    if (not isinstance(token, str) or not 16 <= len(token) <= 16_384
            or any(char.isspace() for char in token)
            or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry) or expiry <= time.time()):
        raise ValueError("invalid desktop session")
    return {"user_id": user, "access_token": token, "expires_at": int(expiry)}


class _Connection:
    def __init__(self, serial: str):
        self.serial = serial
        self.sock = None
        self.rpc = None
        self.forward = None
        self.fingerprint = None
        try:
            self.sock, self.forward = companion._open_forwarded_socket(serial, 1.0, False)
            self.sock.settimeout(5.0)
            self.rpc = companion._Rpc(self.sock)
            hello = self.rpc.call("ping")
            if not isinstance(hello, dict) or hello.get("protocol", 0) < 5:
                raise ValueError("companion update required")
        except Exception:
            self.close()
            raise

    def sync(self, session: Dict[str, Any]) -> bool:
        identity = hashlib.sha256((session["user_id"] + ":" + session["access_token"]
                                   + ":" + str(session["expires_at"])).encode()).digest()
        if identity == self.fingerprint:
            state = self.rpc.call("cloud_heartbeat")
            if isinstance(state, dict):
                if state.get("signed_in") is True:
                    return True
                if state.get("pending") is True:
                    return False
        challenge = self.rpc.call("cloud_challenge")
        nonce = challenge.get("nonce") if isinstance(challenge, dict) else None
        if not isinstance(nonce, str) or len(nonce) != 64:
            raise ValueError("invalid companion challenge")
        reply = self.rpc.call("cloud_login", {**session, "nonce": nonce})
        if not isinstance(reply, dict) or reply.get("accepted") is not True:
            # Keep this socket: a manual mobile login/logout suppresses delegation
            # until an actual reconnection, not until the next automatic retry.
            self.fingerprint = identity
            return False
        self.fingerprint = identity
        return False  # remote verification is asynchronous; a heartbeat confirms it

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        rpc, self.rpc = self.rpc, None
        if rpc is not None:
            try:
                rpc._reader.close()
            except (OSError, ValueError):
                pass
        forward, self.forward = self.forward, None
        if forward:
            try:
                adb.forward_remove(forward, serial=self.serial)
            except adb.AdbError:
                pass
        self.fingerprint = None


class CompanionCloudBridge:
    def __init__(self, connect: Callable[[str], Any] = _Connection):
        self._connect = connect
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._connections: Dict[str, Any] = {}
        self._generation = 0

    def clear(self) -> Dict[str, int]:
        with self._lock:
            self._generation += 1
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            connection.close()
        return {"connected": 0, "signed_in": 0, "unavailable": 0}

    def reserve(self) -> int:
        """Capture stdin arrival order before any worker or token validation can wait."""
        with self._lock:
            return self._generation

    def sync(self, raw: Any, devices: Callable[[], Iterable[Any]],
             generation: int | None = None) -> Dict[str, int]:
        if raw is None:
            return self.clear()
        if generation is None:
            generation = self.reserve()
        session = _session(raw)
        with self._sync_lock:
            with self._lock:
                if generation != self._generation:
                    return {"connected": 0, "signed_in": 0, "unavailable": 0}
            # Prefer one transport per physical device so USB+Wi-Fi do not create
            # competing auth sockets on the same companion.
            selected: Dict[str, str] = {}
            for device in devices():
                identity = getattr(device, "hw_serial", "") or device.serial
                if identity not in selected or getattr(device, "connection_type", "") == "usb":
                    selected[identity] = device.serial
            serials = set(selected.values())
            with self._lock:
                obsolete = [self._connections.pop(key) for key in list(self._connections) if key not in serials]
            for connection in obsolete:
                connection.close()

            def update(serial: str) -> bool:
                connection = None
                try:
                    with self._lock:
                        if generation != self._generation:
                            return False
                        connection = self._connections.get(serial)
                    if connection is None:
                        connection = self._connect(serial)
                        with self._lock:
                            if generation != self._generation:
                                connection.close()
                                return False
                            self._connections[serial] = connection
                    return connection.sync(session)
                except Exception:
                    # Device replies and exception strings must never enter logs:
                    # a compromised/malformed reply could reflect a bearer token.
                    with self._lock:
                        if self._connections.get(serial) is connection:
                            self._connections.pop(serial, None)
                    if connection is not None:
                        connection.close()
                    return False

            if not serials:
                return {"connected": 0, "signed_in": 0, "unavailable": 0}
            with ThreadPoolExecutor(max_workers=min(4, len(serials)), thread_name_prefix="companion-auth") as pool:
                signed_in = sum(pool.map(update, sorted(serials)))
            with self._lock:
                if generation != self._generation:
                    return {"connected": 0, "signed_in": 0, "unavailable": 0}
            return {"connected": len(serials), "signed_in": signed_in, "unavailable": len(serials) - signed_in}


bridge = CompanionCloudBridge()
