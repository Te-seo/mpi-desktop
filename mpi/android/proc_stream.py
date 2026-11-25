"""Request fresh procfs and sysfs readings over one capture-owned adb shell."""

import queue
import subprocess
import threading
import time
import uuid

from . import adb


class _Channel:
    def __init__(self, serial: str, command: str):
        marker = "_MPI_SAMPLE_END_" + uuid.uuid4().hex
        # Execute the fixed script once for each host tick. With no ticks the
        # phone does no work. Each iteration gets clean shell variables.
        script = (
            f"while IFS= read -r tick; do ( {command} ) 2>/dev/null; "
            f"echo; echo {marker}; done"
        )
        self.process = subprocess.Popen(
            ["adb", "-s", serial, "shell", "-T", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            **adb._NO_WINDOW_KW,
        )
        self.command = command
        self.replies = queue.SimpleQueue()
        self.reader = threading.Thread(
            target=self._receive, args=(marker.encode(),), name="capture-proc", daemon=True,
        )
        self.reader.start()

    def _receive(self, marker: bytes):
        try:
            with self.process.stdout as output:
                block = bytearray()
                while line := output.readline(1024 * 1024):
                    if line.rstrip(b"\r\n") == marker:
                        self.replies.put(block.decode("utf-8", errors="replace").replace("\r\n", "\n"))
                        block.clear()
                    else:
                        block.extend(line)
                        if len(block) > 1024 * 1024:
                            break
        except (OSError, ValueError):
            pass
        finally:
            self.replies.put(None)

    def read(self, timeout: float) -> str:
        self.process.stdin.write(b"\n")
        self.process.stdin.flush()
        result = self.replies.get(timeout=timeout)
        if result is None:
            raise OSError("Android sampling shell closed")
        return result

    def close(self):
        self.process.kill()
        self.process.wait()
        self.process.stdin.close()
        self.reader.join()


class ProcStream:
    """The collector serializes reads. Capture teardown can interrupt a read."""

    def __init__(self, serial: str):
        self._serial = serial
        self._lock = threading.Lock()
        self._channel = None
        self._closed = False
        self._retry_at = 0.0

    def read(self, command: str) -> str:
        with self._lock:
            if self._closed:
                raise adb.AdbError("Android capture is closed")
            if self._channel is not None and self._channel.command != command:
                self._channel.close()
                self._channel = None
            if self._channel is None and time.monotonic() >= self._retry_at:
                try:
                    self._channel = _Channel(self._serial, command)
                except OSError:
                    self._retry_at = time.monotonic() + 5.0
            channel = self._channel
        if channel is not None:
            timeout = 2.0
            deadline = adb.shell_deadline.get()
            if deadline is not None:
                timeout = max(0.0, min(timeout, deadline - time.monotonic()))
            try:
                return channel.read(timeout)
            except (OSError, ValueError, queue.Empty):
                with self._lock:
                    if self._closed:
                        raise adb.AdbError("Android capture is closed")
                    channel.close()
                    self._channel = None
                    self._retry_at = time.monotonic() + 5.0
        return adb.shell(command, serial=self._serial, timeout=3.0)

    def close(self):
        with self._lock:
            self._closed = True
            if self._channel is not None:
                self._channel.close()
                self._channel = None
