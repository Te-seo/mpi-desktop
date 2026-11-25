"""Offline preview of `mpi check`.

Runs the real CLI against a monkey-patched AndroidBackend that returns a
canned MetricsSnapshot sourced from tests/fixtures/*. No device or adb
required. Use this to see exactly what a live `check` will render.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner

from mpi import cli as cli_module
from mpi.android import parsers
from mpi.android.backend import AndroidBackend
from mpi.schema import (
    DeviceInfo,
    MetricsSnapshot,
    PackageInfo,
    Platform,
)


FIXTURES = Path(__file__).parent / "tests" / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fake_list_devices(self):  # type: ignore[no-untyped-def]
    return [
        DeviceInfo(
            serial="DEMOSERIAL01",
            platform=Platform.ANDROID,
            model="Pixel 8",
            os_version="Android 14 (SDK 34)",
        )
    ]


def _fake_capture(self, serial, package, duration_sec=10.0):  # type: ignore[no-untyped-def]
    gfxinfo = _load_fixture("gfxinfo_sample.txt")
    meminfo = _load_fixture("meminfo_sample.txt")
    top = _load_fixture("top_sample.txt")
    pidof = "2094\n"

    raw = {
        "gfxinfo": gfxinfo,
        "meminfo": meminfo,
        "pidof": pidof,
        "top": top,
    }

    frames = parsers.parse_gfxinfo(gfxinfo)
    memory = parsers.parse_meminfo(meminfo)
    cpu = parsers.parse_top(top, pid=2094)

    device = DeviceInfo(
        serial=serial,
        platform=Platform.ANDROID,
        model="Pixel 8",
        os_version="Android 14 (SDK 34)",
    )
    enriched = PackageInfo(
        bundle_id=package.bundle_id,
        version_name="1.2.3",
        version_code="123",
        apk_path="/data/app/com.example.app/base.apk",
        uid="10234",
    )
    return MetricsSnapshot(
        device=device,
        package=enriched,
        captured_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=duration_sec,
        frames=frames,
        memory=memory,
        cpu=cpu,
        raw=raw,
    )


def main() -> None:
    AndroidBackend.list_devices = _fake_list_devices  # type: ignore[assignment]
    AndroidBackend.capture = _fake_capture  # type: ignore[assignment]

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        [
            "check",
            "--platform",
            "android",
            "--serial",
            "DEMOSERIAL01",
            "--package",
            "com.example.app",
            "--duration",
            "0",
        ],
        color=True,
    )

    print(result.output)
    if result.exit_code != 0:
        print(f"(exit code {result.exit_code})")
        if result.exception:
            raise result.exception


if __name__ == "__main__":
    main()
