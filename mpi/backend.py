"""Backend protocol shared by platform adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from .schema import DeviceInfo, MetricsSnapshot, PackageInfo


class AppNotRunningError(RuntimeError):
    """The selected app has no running process at capture start.

    Messages must contain the stable ``app-not-running`` sentinel — the desktop
    matches it (frontend/src/lib/friendlyError.ts) to show localized guidance.
    """


class AppExitedError(RuntimeError):
    """The measured app's process disappeared mid-capture.

    The capture pump (mpi/api.py) treats this as FATAL: it surfaces the
    error and ends the capture, instead of retrying forever against a process
    that no longer exists. Messages must contain the stable ``app-exited``
    sentinel for the desktop's localized notice.
    """


class AppBackgroundedError(AppExitedError):
    """The measured app left the foreground mid-capture (backgrounded /
    suspended / screen off), so focused measurement is no longer meaningful.

    Subclasses :class:`AppExitedError` so the capture pump ends the capture the
    same way, with its own stable ``app-backgrounded`` sentinel for a distinct
    localized notice.
    """


class DeviceBackend(ABC):
    """Platform-agnostic capture interface."""

    @abstractmethod
    def list_devices(self) -> List[DeviceInfo]:
        ...

    @abstractmethod
    def list_packages(self, serial: str, user_only: bool = True) -> List[PackageInfo]:
        ...

    @abstractmethod
    def capture(
        self,
        serial: str,
        package: PackageInfo,
        duration_sec: float = 10.0,
    ) -> MetricsSnapshot:
        ...
