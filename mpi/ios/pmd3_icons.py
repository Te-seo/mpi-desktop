"""Batch iOS app-icon fetcher (run as a standalone subprocess).

Like :mod:`mpi.ios.pmd3_stream`, this script is *not* imported by the
backend — it is executed as a separate process by :mod:`mpi.ios.pmd3`
using whichever Python interpreter has ``pymobiledevice3`` installed. It must
stay self-contained: only the standard library and ``pymobiledevice3`` may be
imported, nothing from the ``mpi`` package.

Why a batch process? ``SpringBoardServicesService.get_icon_pngdata`` returns an
app's home-screen PNG, but the expensive part is the one-off
interpreter-import + usbmux/lockdown handshake (~1 s); the per-icon fetch that
follows is ~15 ms. Spawning the CLI once per icon would re-pay that second every
time. Here we connect **once** over usbmux (SpringBoard is a plain lockdown
service — no developer tunnel/RSD needed, even on iOS 17+) and fetch every
requested icon from the single connection.

Input : a JSON array of bundle ids on stdin, e.g. ``["com.foo.bar", ...]``.
Output: one JSON object per line on stdout (newline-delimited JSON)::

    {"bundle_id": "com.foo.bar", "png_b64": "iVBORw0K..."}
    {"bundle_id": "com.baz", "error": "no icon"}

Status / diagnostics go to stderr so stdout stays a clean NDJSON stream.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import warnings
from typing import List, Optional, Tuple


# Per-icon hard cap. The handshake is already done by the time we fetch icons,
# so a single icon should return in tens of ms; a multi-second wait means the
# service wedged and we should skip that icon rather than stall the batch.
_ICON_TIMEOUT_SEC = 8.0


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _status(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()


async def _maybe_await(value):
    """Support pymobiledevice3 builds where lockdown/service calls are async."""
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _connect_usbmux(udid: str):
    """Plain lockdown over usbmux — works for USB devices and needs no tunnel."""
    from pymobiledevice3.lockdown import create_using_usbmux

    return await _maybe_await(create_using_usbmux(serial=udid or None))


async def _connect_rsd(
    udid: str,
    tunneld: Optional[Tuple[str, int]],
    rsd: Optional[Tuple[str, int]],
):
    """RemoteServiceDiscovery over the tunnel. Required for wireless devices on
    Windows, where usbmux only advertises USB-attached devices, so a Wi-Fi phone
    is invisible to ``create_using_usbmux``. SpringBoard is reachable over the
    tunnel via its ``.shim.remote`` service (selected automatically by
    ``SpringBoardServicesService`` when handed an RSD provider). Mirrors the
    streamer's proven connection path (mpi.ios.pmd3_stream._connect_rsd)."""
    from pymobiledevice3.remote.remote_service_discovery import (
        RemoteServiceDiscoveryService,
    )
    from pymobiledevice3.tunneld.api import (
        TUNNELD_DEFAULT_ADDRESS,
        get_tunneld_device_by_udid,
        get_tunneld_devices,
    )

    if rsd is not None:
        service = RemoteServiceDiscoveryService((rsd[0], int(rsd[1])))
        await _maybe_await(service.connect())
        return service

    address = tunneld or TUNNELD_DEFAULT_ADDRESS
    if udid:
        service = await get_tunneld_device_by_udid(udid, address)
        if service is not None:
            return service

    rsds = await get_tunneld_devices(address)
    if len(rsds) == 1:
        return rsds[0]
    for extra in rsds:
        try:
            await extra.close()
        except Exception:
            pass
    return None


async def _open_springboard(lockdown):
    from pymobiledevice3.services.springboard import SpringBoardServicesService

    service = SpringBoardServicesService(lockdown=lockdown)
    enter = getattr(service, "__aenter__", None)
    if enter is not None:
        await service.__aenter__()
    return service


async def _close(obj) -> None:
    aexit = getattr(obj, "__aexit__", None)
    if aexit is not None:
        try:
            await obj.__aexit__(None, None, None)
            return
        except Exception:
            pass
    closer = getattr(obj, "close", None)
    if closer is not None:
        try:
            await _maybe_await(closer())
        except Exception:
            pass


async def _fetch_one(service, bundle_id: str) -> None:
    method = service.get_icon_pngdata
    try:
        if asyncio.iscoroutinefunction(method):
            data = await asyncio.wait_for(method(bundle_id), timeout=_ICON_TIMEOUT_SEC)
        else:
            # Synchronous pymobiledevice3 build: invoke the blocking call in a
            # worker thread so _ICON_TIMEOUT_SEC can actually interrupt a wedged
            # SpringBoard request. Calling it inline would block the event loop
            # before wait_for got control, stalling the whole batch until the
            # parent subprocess timeout killed it and dropped every later icon.
            data = await asyncio.wait_for(
                asyncio.to_thread(method, bundle_id), timeout=_ICON_TIMEOUT_SEC
            )
            if asyncio.iscoroutine(data):  # rare: sync fn that returns a coroutine
                data = await data
    except asyncio.TimeoutError:
        _emit({"bundle_id": bundle_id, "error": "timeout"})
        return
    except Exception as exc:
        _emit({"bundle_id": bundle_id, "error": f"{type(exc).__name__}: {exc}"})
        return
    if not data:
        _emit({"bundle_id": bundle_id, "error": "empty"})
        return
    _emit(
        {
            "bundle_id": bundle_id,
            "png_b64": base64.b64encode(bytes(data)).decode("ascii"),
        }
    )


async def _run(
    udid: str,
    bundle_ids: List[str],
    tunneld: Optional[Tuple[str, int]] = None,
    rsd: Optional[Tuple[str, int]] = None,
) -> int:
    _status(
        f"connecting: udid={udid or '?'} icons={len(bundle_ids)} "
        f"rsd={rsd or 'auto'} tunneld={tunneld or 'default'}"
    )
    # usbmux first: it is the cheapest path and needs no tunnel, so a USB device
    # connects immediately. A wireless device is invisible to usbmux on Windows,
    # so that attempt fails fast and we fall back to the RemoteServiceDiscovery
    # tunnel — the same transport live capture already uses.
    lockdown = None
    transport = ""
    try:
        lockdown = await _connect_usbmux(udid)
        transport = "usbmux"
    except Exception as exc:
        _status(f"usbmux connect failed ({type(exc).__name__}: {exc}); trying tunnel")

    if lockdown is None:
        try:
            lockdown = await _connect_rsd(udid, tunneld, rsd)
            transport = "rsd"
        except Exception as exc:
            _status(f"rsd connect failed: {type(exc).__name__}: {exc}")

    if lockdown is None:
        _emit({"event": "fatal", "error": "no usbmux or tunnel connection to device"})
        return 1

    service = await _open_springboard(lockdown)
    _status(f"connected: SpringBoardServices established over {transport}")
    _emit({"event": "ready"})
    try:
        for bundle_id in bundle_ids:
            await _fetch_one(service, bundle_id)
    finally:
        await _close(service)
        await _close(lockdown)
    return 0


def _read_bundle_ids() -> List[str]:
    raw = sys.stdin.read().lstrip("﻿").strip()
    if not raw:
        return []
    # Parse the JSON array, tolerating a stray prefix before the opening bracket
    # (e.g. a BOM artifact from an odd stdin encoding).
    start = raw.find("[")
    if start != -1:
        try:
            parsed = json.loads(raw[start:])
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _parse_addr(raw: str) -> Optional[Tuple[str, int]]:
    raw = (raw or "").strip()
    if not raw:
        return None
    host, sep, port = raw.rpartition(":")
    if sep and host and port.isdigit():
        return (host, int(port))
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Batch iOS app-icon fetcher")
    parser.add_argument("--udid", default="")
    parser.add_argument("--tunneld", default="", help="tunneld HOST:PORT")
    parser.add_argument(
        "--rsd",
        nargs=2,
        metavar=("HOST", "PORT"),
        default=None,
        help="explicit RemoteServiceDiscovery HOST PORT (skips tunneld lookup)",
    )
    args = parser.parse_args(argv)
    tunneld = _parse_addr(args.tunneld)
    rsd = (args.rsd[0], int(args.rsd[1])) if args.rsd is not None else None

    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

    # The parent writes the bundle-id list as UTF-8; force matching decode so a
    # Windows code-page default can't corrupt it (bundle ids are ASCII, but be
    # safe), and keep stdout UTF-8 for the NDJSON replies.
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

    # pymobiledevice3 requires the selector event loop on Windows (its proactor
    # path breaks raw-socket usage); mirror what its own CLI does.
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    bundle_ids = _read_bundle_ids()
    if not bundle_ids:
        _emit({"event": "ready"})
        return 0

    try:
        return asyncio.run(_run(args.udid, bundle_ids, tunneld, rsd))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _emit({"event": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
