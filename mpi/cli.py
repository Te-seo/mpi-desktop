from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import List

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .android.adb import AdbError
from .android.backend import AndroidBackend
from .backend import DeviceBackend
from .ios.backend import IOSBackend
from .ios.pmd3 import Pymobiledevice3Error
from .ios.xcrun import XcrunError
from .schema import DeviceInfo, MetricsSnapshot, PackageInfo, Platform


console = Console()

RAW_MAX_CHARS = 4000


def _backend(platform: str) -> DeviceBackend:
    if platform == "android":
        return AndroidBackend()
    if platform == "ios":
        return IOSBackend()
    raise click.BadParameter(f"unknown platform: {platform}")


@click.group()
def cli() -> None:
    """MPI - unified Android/iOS package performance measurement."""


@cli.command("devices")
@click.option(
    "--platform",
    "platform",
    type=click.Choice(["android", "ios", "all"]),
    default="all",
    show_default=True,
)
def devices_cmd(platform: str) -> None:
    """List connected devices across platforms."""
    table = Table(title="Connected devices")
    table.add_column("Serial")
    table.add_column("Platform")
    table.add_column("Model")
    table.add_column("OS")

    targets = ["android", "ios"] if platform == "all" else [platform]
    for plat in targets:
        backend = _backend(plat)
        try:
            entries = backend.list_devices()
        except NotImplementedError:
            table.add_row(
                "-", plat, "[dim](backend not yet implemented)[/dim]", "-"
            )
            continue
        except (AdbError, Pymobiledevice3Error, XcrunError) as exc:
            table.add_row("-", plat, f"[red]{type(exc).__name__}: {exc}[/red]", "-")
            continue

        if not entries:
            table.add_row("-", plat, "[dim](no devices)[/dim]", "-")
            continue

        for d in entries:
            table.add_row(d.serial, d.platform.value, d.model, d.os_version)

    console.print(table)


@cli.command("packages")
@click.option(
    "--platform",
    "platform",
    type=click.Choice(["android", "ios"]),
    required=True,
)
@click.option("--serial", required=True)
@click.option("--all", "show_all", is_flag=True, help="Include system packages.")
@click.option("--filter", "filter_", default=None, help="Case-insensitive substring.")
def packages_cmd(
    platform: str, serial: str, show_all: bool, filter_: str
) -> None:
    """List installed packages on a device."""
    backend = _backend(platform)
    try:
        pkgs = backend.list_packages(serial, user_only=not show_all)
    except NotImplementedError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        sys.exit(2)
    except (AdbError, Pymobiledevice3Error, XcrunError) as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        sys.exit(3)

    if filter_:
        needle = filter_.lower()
        pkgs = [p for p in pkgs if needle in p.bundle_id.lower()]

    table = Table(title=f"Packages on {serial}")
    table.add_column("Bundle ID")
    table.add_column("APK path")
    for p in pkgs:
        table.add_row(p.bundle_id, p.apk_path or "-")
    console.print(table)


@cli.command("check")
@click.option(
    "--platform",
    "platform",
    type=click.Choice(["android", "ios"]),
    required=True,
)
@click.option("--serial", required=True)
@click.option("--package", "package_id", required=True)
@click.option("--duration", type=float, default=10.0, show_default=True)
@click.option("--json-out", "json_out", type=click.Path(dir_okay=False), default=None)
@click.option("--show-raw/--no-show-raw", default=True, show_default=True)
def check_cmd(
    platform: str,
    serial: str,
    package_id: str,
    duration: float,
    json_out: str,
    show_raw: bool,
) -> None:
    """Capture a snapshot and print RAW + PARSED side by side."""
    backend = _backend(platform)
    pkg = PackageInfo(bundle_id=package_id)

    try:
        snap = backend.capture(serial, pkg, duration_sec=duration)
    except NotImplementedError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        sys.exit(2)
    except (AdbError, Pymobiledevice3Error, XcrunError) as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        sys.exit(3)

    _render_parsed(snap)
    if show_raw:
        _render_raw(snap)

    if json_out:
        _write_json(snap, json_out)
        console.print(f"[green]Wrote JSON to {json_out}[/green]")


def _render_parsed(snap: MetricsSnapshot) -> None:
    table = Table(title="Parsed Metrics", show_lines=False)
    table.add_column("Section", style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    d = snap.device
    p = snap.package
    table.add_row("Device", "serial", d.serial)
    table.add_row("", "platform", d.platform.value)
    table.add_row("", "model", d.model)
    table.add_row("", "os", d.os_version)
    table.add_row("Package", "bundle", p.bundle_id)
    table.add_row("", "version", f"{p.version_name or '-'} ({p.version_code or '-'})")
    table.add_row("", "uid", p.uid or "-")
    table.add_row("Capture", "captured_at", snap.captured_at)
    table.add_row("", "duration_sec", f"{snap.duration_sec:.2f}")

    f = snap.frames
    table.add_row("Frames", "total_frames", str(f.total_frames))
    table.add_row("", "janky_frames", str(f.janky_frames))
    table.add_row("", "jank_percent", f"{f.jank_percent:.2f}%")
    table.add_row("", "p50/p90/p95/p99 ms",
                  f"{f.p50_ms}/{f.p90_ms}/{f.p95_ms}/{f.p99_ms}")
    table.add_row("", "missed_vsync", str(f.missed_vsync))
    table.add_row("", "slow_ui_thread", str(f.slow_ui_thread))
    table.add_row("", "frame_deadline_missed", str(f.frame_deadline_missed))
    table.add_row(
        "",
        "fps_estimate",
        f"{f.fps_estimate:.2f}" if f.fps_estimate is not None else "-",
    )
    table.add_row("", "fps_source / scope", f"{f.source or '-'} / {f.scope or '-'}")

    m = snap.memory
    table.add_row("Memory", "pss_total_kb", str(m.pss_total_kb) if m.pss_total_kb is not None else "-")
    table.add_row("", "rss_total_kb", str(m.rss_total_kb) if m.rss_total_kb is not None else "-")
    table.add_row("", "java_heap_kb", str(m.java_heap_kb))
    table.add_row("", "native_heap_kb", str(m.native_heap_kb))
    table.add_row("", "graphics_kb", str(m.graphics_kb))
    table.add_row("", "code_kb", str(m.code_kb))
    table.add_row("", "stack_kb", str(m.stack_kb))

    c = snap.cpu
    table.add_row("CPU", "pid", str(c.pid))
    table.add_row("", "cpu_percent", f"{c.cpu_percent:.1f}%" if c.cpu_percent is not None else "-")
    table.add_row("", "mem_percent", f"{c.mem_percent:.1f}%")

    console.print(table)


def _render_raw(snap: MetricsSnapshot) -> None:
    for key, value in snap.raw.items():
        text = value if isinstance(value, str) else str(value)
        truncated = False
        if len(text) > RAW_MAX_CHARS:
            text = text[:RAW_MAX_CHARS]
            truncated = True
        if truncated:
            text = text + "\n... (truncated)"
        panel = Panel(
            text,
            title=f"RAW: {key}",
            border_style="yellow",
            expand=True,
        )
        console.print(panel, markup=False)


def _write_json(snap: MetricsSnapshot, path: str) -> None:
    payload = asdict(snap)
    # asdict preserves Enum, but Platform is a str subclass so json handles it.
    payload["device"]["platform"] = snap.device.platform.value
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    cli()
