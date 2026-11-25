"""Pure parsers for iOS tool output (PLAN-ios §5, Phases i2, i3, i5, i6).

Each function takes already-fetched tool output (JSON dict or XML text) and
returns unified-schema dataclasses. Zero ``subprocess`` use here; capture
orchestration lives in :mod:`mpi.ios.capture`.
"""

from __future__ import annotations

import math
import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..schema import CpuStats, DeviceInfo, FrameStats, MemStats, PackageInfo
from ..schema import Platform
from .model_catalog import MODEL_NUMBERS, PRODUCT_TYPES


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _norm(value: Any) -> str:
    return _NON_ALNUM.sub("", str(value).lower())


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _walk_lists(value: Any) -> Iterable[list]:
    if isinstance(value, list):
        yield value
        for item in value:
            yield from _walk_lists(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_lists(item)


def _walk_dicts(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _lookup(record: dict, keys: Iterable[str]) -> Optional[Any]:
    # Candidate order is part of the contract. In particular ProductVersion
    # must win over BuildVersion regardless of JSON insertion order.
    for candidate in keys:
        wanted = _norm(candidate)
        for current in _walk_dicts(record):
            for key, value in current.items():
                if _norm(key) == wanted and value not in (None, ""):
                    return value
    return None


def _contains_value(record: dict, needle: str) -> bool:
    needle = needle.lower()
    for value in _walk_values(record):
        if needle in str(value).lower():
            return True
    return False


def _walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def _candidate_records(json_obj: dict, identity_keys: Iterable[str]) -> List[dict]:
    wanted = {_norm(k) for k in identity_keys}
    for items in _walk_lists(json_obj):
        records = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if any(_norm(key) in wanted for current in _walk_dicts(item) for key in current):
                records.append(item)
        if records:
            return records
    return []


def _is_unavailable(record: dict) -> bool:
    value = _lookup(record, ("tunnelState", "availability", "state", "isAvailable"))
    if isinstance(value, bool):
        return not value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"false", "unavailable", "unpaired", "disconnected", "offline"}


def _is_simulator(record: dict) -> bool:
    return (
        _contains_value(record, "coresimulator")
        or _contains_value(record, "iphonesimulator")
        or _contains_value(record, "simulator")
    )


def _connection_type(record: dict) -> str:
    connection = _lookup(record, ("connectionType", "transportType", "transport", "interface"))
    haystack = str(connection or "")
    props = _lookup(record, ("connectionProperties",))
    if props is not None:
        haystack += f" {props}"
    text = haystack.lower()
    if "wifi" in text or "wi-fi" in text or "wireless" in text or "network" in text:
        return "wireless"
    return "usb"


def _format_connection_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if any(token in text for token in ("network", "wifi", "wi-fi", "wireless")):
        return "wireless"
    return "usb"


def _format_ios_version(value: Any) -> str:
    if value is None:
        return "unknown"
    text = str(value).strip()
    if not text:
        return "unknown"
    lowered = text.lower()
    if lowered.startswith("ios") or lowered.startswith("ipados"):
        return text
    return f"iOS {text}"


def _model_identity(record: Any, device: Optional[DeviceInfo] = None) -> dict[str, str]:
    """Separate a marketing name, ProductType and actual regulatory number.

    Lockdown's ModelNumber is often a retail part number (M…/A), so only an
    explicit A + four digits is accepted as the regulatory model number.
    ProductType identifies a family, not its region-specific A-number.
    """
    def value(keys: tuple[str, ...]) -> str:
        found = _lookup(record, keys)
        return found.strip() if isinstance(found, str) else ""

    identifier = value(("ProductType", "modelIdentifier", "deviceType"))
    if not identifier and device:
        identifier = device.model_identifier
    number = ""
    for key in ("RegulatoryModelNumber", "ModelNumber"):
        candidate = value((key,)).upper()
        if re.fullmatch(r"A\d{4}", candidate):
            number = candidate
            break
    if re.fullmatch(r"A\d{4}", identifier.upper()):
        number = number or identifier.upper()
        identifier = ""
    if not number and device:
        number = device.model_number
    name = value(("MarketingProductName", "marketingName", "modelName", "marketing-name"))
    # Some discovery tools use the generic product family as their model name.
    # A specific observed/model-catalog result is more useful than 'iPhone'.
    if name.lower() in {"unknown", "iphone", "ipad", "ipod", "ios device"}:
        name = ""
    mapped = MODEL_NUMBERS.get(number) or PRODUCT_TYPES.get(identifier)
    previous = device.model if device and device.model != "unknown" else ""
    fallback = identifier or number or value(("HardwareModel",)) or "unknown"
    return {"model": name or mapped or previous or fallback,
            "model_identifier": identifier, "model_number": number}


def parse_devicectl_devices(json_obj: dict) -> List[DeviceInfo]:
    """Parse ``xcrun devicectl list devices --json-output -`` output.

    Filters: physical devices only (drop simulators with
    ``deviceType == "com.apple.CoreSimulator.*"``) and reachable devices only
    (``connectionProperties.tunnelState != "unavailable"``).

    Returns ``[]`` on empty or malformed input; never raises.

    """
    devices: List[DeviceInfo] = []
    seen: set[str] = set()
    records = _candidate_records(
        json_obj,
        (
            "identifier",
            "udid",
            "deviceIdentifier",
            "uniqueDeviceIdentifier",
            "serialNumber",
        ),
    )
    for record in records:
        if _is_simulator(record) or _is_unavailable(record):
            continue
        serial = _lookup(
            record,
            (
                "identifier",
                "udid",
                "deviceIdentifier",
                "uniqueDeviceIdentifier",
                "serialNumber",
            ),
        )
        if serial is None:
            continue
        serial_s = str(serial).strip()
        if not serial_s or serial_s in seen:
            continue
        seen.add(serial_s)

        display_name = _lookup(record, ("name", "deviceName"))
        identity = _model_identity(record)
        os_version = _lookup(
            record,
            (
                "productVersion",
                "osVersion",
                "osVersionNumber",
            ),
        )
        devices.append(
            DeviceInfo(
                serial=serial_s,
                platform=Platform.IOS,
                **identity,
                display_name=str(display_name or ""),
                os_version=_format_ios_version(os_version),
                connection_type=_connection_type(record),
                build_version=str(_lookup(record, ("ProductBuildVersion", "BuildVersion", "osBuildUpdate")) or ""),
                build_name=str(_lookup(record, ("BuildTrain", "osVersionExtra")) or ""),
            )
        )
    return devices


def parse_pymobiledevice3_devices(json_obj: Any) -> List[DeviceInfo]:
    """Parse ``pymobiledevice3 usbmux list`` JSON output.

    The CLI is useful on Windows because it can talk to usbmux and paired
    network devices without Xcode. Output has changed shape across releases, so
    this parser accepts either a list of device records or nested dictionaries.
    """
    devices: List[DeviceInfo] = []
    seen: set[tuple[str, str]] = set()
    records = _candidate_records(
        json_obj,
        (
            "SerialNumber",
            "UniqueDeviceID",
            "UDID",
            "Identifier",
            "DeviceID",
        ),
    )
    for record in records:
        serial = _lookup(
            record,
            (
                "SerialNumber",
                "UniqueDeviceID",
                "UDID",
                "Identifier",
            ),
        )
        if serial is None:
            continue
        serial_s = str(serial).strip()
        if not serial_s:
            continue

        connection = _lookup(record, ("ConnectionType", "Transport", "Interface", "Network"))
        connection_type = _format_connection_type(connection)
        key = (serial_s, connection_type)
        if key in seen:
            continue
        seen.add(key)

        display_name = _lookup(record, ("DeviceName", "Name"))
        identity = _model_identity(record)
        os_version = _lookup(record, ("ProductVersion", "OSVersion"))
        devices.append(
            DeviceInfo(
                serial=serial_s,
                platform=Platform.IOS,
                **identity,
                display_name=str(display_name or ""),
                os_version=_format_ios_version(os_version),
                connection_type=connection_type,
                build_version=str(_lookup(record, ("ProductBuildVersion", "BuildVersion")) or ""),
                build_name=str(_lookup(record, ("BuildTrain", "osVersionExtra")) or ""),
            )
        )
    return devices


def enrich_pymobiledevice3_device(device: DeviceInfo, info: Any) -> DeviceInfo:
    display_name = _lookup(info, ("DeviceName", "Name"))
    identity = _model_identity(info, device)
    os_version = _lookup(info, ("ProductVersion", "OSVersion"))
    if display_name is not None:
        device.display_name = str(display_name)
    for key, value in identity.items():
        setattr(device, key, value)
    if os_version is not None:
        device.os_version = _format_ios_version(os_version)
    build_version = _lookup(info, ("ProductBuildVersion", "BuildVersion"))
    build_name = _lookup(info, ("BuildTrain", "osVersionExtra"))
    if build_version is not None:
        device.build_version = str(build_version)
    if build_name is not None:
        device.build_name = str(build_name)
    return device


def parse_devicectl_apps(json_obj: dict, user_only: bool) -> List[PackageInfo]:
    """Parse ``xcrun devicectl device info apps --json-output -`` output.

    When ``user_only`` is True, keeps only entries with
    ``installationType == "User"``. Results sorted by ``bundle_id``.

    """
    apps: List[PackageInfo] = []
    seen: set[str] = set()
    records = _candidate_records(
        json_obj,
        (
            "bundleIdentifier",
            "bundleID",
            "bundleId",
            "bundle_id",
            "CFBundleIdentifier",
        ),
    )
    for record in records:
        install_type = _lookup(record, ("installationType", "appType", "type"))
        if user_only and install_type is not None:
            text = str(install_type).strip().lower()
            if text in {"system", "builtin", "built-in", "internal"}:
                continue
            if not any(token in text for token in ("user", "developer", "third")):
                continue

        bundle_id = _lookup(
            record,
            (
                "bundleIdentifier",
                "bundleID",
                "bundleId",
                "bundle_id",
                "CFBundleIdentifier",
            ),
        )
        if bundle_id is None:
            continue
        bundle_s = str(bundle_id).strip()
        if not bundle_s or bundle_s in seen:
            continue
        seen.add(bundle_s)

        label = _lookup(
            record,
            (
                "displayName",
                "localizedName",
                "name",
                "bundleName",
                "CFBundleDisplayName",
                "CFBundleName",
            ),
        )
        version_name = _lookup(
            record,
            (
                "bundleShortVersionString",
                "shortVersionString",
                "CFBundleShortVersionString",
                "version",
            ),
        )
        version_code = _lookup(
            record,
            ("bundleVersion", "CFBundleVersion", "buildVersion", "versionCode"),
        )
        apps.append(
            PackageInfo(
                bundle_id=bundle_s,
                label=str(label) if label is not None else None,
                version_name=str(version_name) if version_name is not None else None,
                version_code=str(version_code) if version_code is not None else None,
            )
        )
    return sorted(apps, key=lambda item: item.bundle_id)


def parse_pymobiledevice3_apps(json_obj: Any, user_only: bool) -> List[PackageInfo]:
    """Parse ``pymobiledevice3 apps list`` JSON output."""
    apps: List[PackageInfo] = []
    seen: set[str] = set()
    records: List[tuple[Optional[str], dict]] = []

    if isinstance(json_obj, dict):
        for key, value in json_obj.items():
            if isinstance(value, dict):
                records.append((str(key), value))
        if not records:
            records = [(None, record) for record in _candidate_records(json_obj, ("CFBundleIdentifier",))]
    elif isinstance(json_obj, list):
        records = [(None, record) for record in json_obj if isinstance(record, dict)]

    for key_bundle, record in records:
        bundle_id = _lookup(
            record,
            (
                "CFBundleIdentifier",
                "bundleIdentifier",
                "BundleIdentifier",
                "bundle_id",
            ),
        )
        bundle_s = str(bundle_id or key_bundle or "").strip()
        if not bundle_s or bundle_s in seen:
            continue

        app_type = _lookup(record, ("ApplicationType", "applicationType", "Type"))
        if user_only and app_type is not None:
            text = str(app_type).strip().lower()
            if text in {"system", "internal", "hidden"}:
                continue
            if not any(token in text for token in ("user", "developer", "third")):
                continue

        seen.add(bundle_s)
        label = _lookup(
            record,
            (
                "CFBundleDisplayName",
                "CFBundleName",
                "DisplayName",
                "Name",
            ),
        )
        version_name = _lookup(
            record,
            (
                "CFBundleShortVersionString",
                "ShortVersionString",
                "Version",
            ),
        )
        version_code = _lookup(record, ("CFBundleVersion", "BundleVersion"))
        apps.append(
            PackageInfo(
                bundle_id=bundle_s,
                label=str(label) if label is not None else None,
                version_name=str(version_name) if version_name is not None else None,
                version_code=str(version_code) if version_code is not None else None,
            )
        )
    return sorted(apps, key=lambda item: item.bundle_id)


def _parse_xml(xml_text: str) -> Optional[ET.Element]:
    if not xml_text.strip():
        return None
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            return ET.fromstring(f"<root>{xml_text}</root>")
        except ET.ParseError:
            return None


def _flatten_xml_element(elem: ET.Element) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for key, value in elem.attrib.items():
        if value.strip():
            data[_norm(key)] = value.strip()
    for child in elem.iter():
        if child is elem:
            continue
        name = _norm(_strip_ns(child.tag))
        text = " ".join(t.strip() for t in child.itertext() if t and t.strip())
        if name and text and name not in data:
            data[name] = text
        for key, value in child.attrib.items():
            if value.strip():
                data.setdefault(f"{name}{_norm(key)}", value.strip())
    return data


def _xml_rows(root: ET.Element) -> List[Dict[str, str]]:
    row_tags = {"row", "sample", "record", "event", "point"}
    preferred = [
        elem
        for elem in root.iter()
        if _norm(_strip_ns(elem.tag)) in row_tags and len(_flatten_xml_element(elem)) > 0
    ]
    elems = preferred or [
        elem for elem in root.iter() if len(list(elem)) > 0 and len(_flatten_xml_element(elem)) > 0
    ]
    return [_flatten_xml_element(elem) for elem in elems]


def _parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    match = _NUMBER.search(str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_percent(value: Any) -> Optional[float]:
    return _parse_number(value)


def _parse_size_kb(value: Any) -> Optional[int]:
    number = _parse_number(value)
    if number is None:
        return None
    text = str(value).strip().lower()
    if "gib" in text or "gb" in text:
        kb = number * 1024 * 1024
    elif "mib" in text or "mb" in text:
        kb = number * 1024
    elif "kib" in text or "kb" in text:
        kb = number
    elif "byte" in text or re.search(r"\bb\b", text):
        kb = number / 1024
    elif number > 10_000_000:
        kb = number / 1024
    elif number < 10_000:
        # Activity Monitor often emits resident memory as MB when the unit is
        # only carried by column metadata outside the row.
        kb = number * 1024
    else:
        kb = number
    return int(round(kb))


def _find_value(data: Dict[str, str], names: Iterable[str], skip: Iterable[str] = ()) -> Optional[str]:
    normalized = [_norm(name) for name in names]
    skipped = [_norm(name) for name in skip]
    for wanted in normalized:
        for key, value in data.items():
            if wanted in key and not any(part in key for part in skipped):
                return value
    return None


def _row_matches_pid(data: Dict[str, str], pid: int) -> bool:
    pid_s = str(pid)
    for key, value in data.items():
        if ("pid" in key or "processid" in key or "processidentifier" in key) and str(value).strip() == pid_s:
            return True
    return False


def _find_record_for_pid(json_obj: Any, pid: int) -> Optional[dict]:
    pid_s = str(pid)
    for record in _walk_dicts(json_obj):
        value = _lookup(record, ("pid", "processIdentifier", "processID"))
        if value is not None and str(value).strip() == pid_s:
            return record
    return None


# Absolute sanity ceiling for iOS FPS. ProMotion tops out at 120 Hz today; 240
# leaves generous headroom for future high-refresh panels while still rejecting
# impossible bursts. It is decoupled from the per-sample refresh hint so a real
# 120 fps game is never pinned at a lower value — the iOS twin of the Android
# refresh-ceiling fix. Direct/timestamp readings are reported at their true
# magnitude; only integer-ms duration fallback buckets may be restored to a
# supplied refresh cadence (see ``_fps_from_durations``).
_IOS_FPS_SANITY_CEILING = 240.0


def _max_ios_fps(refresh_hz: int) -> float:
    return max(float(refresh_hz), _IOS_FPS_SANITY_CEILING)


def _clamp_fps(fps: float, *, refresh_hz: int) -> float:
    return min(max(fps, 0.0), _max_ios_fps(refresh_hz))


def _coarse_duration_refresh_multiple(
    duration_ms: float, refresh_hz: int
) -> Optional[int]:
    """Return the refresh-period count represented by a coarse ms bucket.

    Some Core Animation exports truncate frame durations to integer milliseconds,
    so a 60 fps frame is emitted as ``16 ms`` and a 120 fps frame as ``8 ms``.
    Treat an integer bucket ``b`` as the quantization interval ``[b, b + 1)`` and
    restore it only when exactly one positive multiple of the supplied refresh
    period falls inside that interval.  Non-integer or non-cadence durations are
    deliberately rejected so high-resolution/VRR data keeps its raw estimate.
    """
    if refresh_hz <= 0 or duration_ms <= 0:
        return None
    bucket = round(duration_ms)
    if not math.isclose(duration_ms, float(bucket), rel_tol=0.0, abs_tol=1e-9):
        return None

    refresh_period_ms = 1000.0 / float(refresh_hz)
    # Subtract a tiny epsilon before ceil so an exact lower-bound cadence is not
    # advanced to the following multiple by floating-point noise.
    multiple = max(1, math.ceil(float(bucket) / refresh_period_ms - 1e-12))
    cadence_ms = multiple * refresh_period_ms
    next_cadence_ms = (multiple + 1) * refresh_period_ms
    if (
        float(bucket) - 1e-9 <= cadence_ms < float(bucket + 1) - 1e-9
        and next_cadence_ms >= float(bucket + 1) - 1e-9
    ):
        return multiple
    return None


def _fps_from_durations(
    durations_ms: List[float], *, refresh_hz: Optional[int] = None
) -> Optional[float]:
    valid_durations = [value for value in durations_ms if value > 0]
    if not valid_durations:
        return None

    # Reconstruct integer-ms Core Animation buckets only when the *entire*
    # sample is consistent with one refresh cadence.  This turns 16 ms at
    # 60/120 Hz back into the appropriate one/two-vsync duration and lets mixed
    # 16/33 ms buckets account for dropped refreshes.  Any unmatched bucket
    # (including VRR/high-resolution data) keeps the historical p50 estimate.
    if refresh_hz is not None and refresh_hz > 0:
        refresh_multiples = [
            _coarse_duration_refresh_multiple(value, refresh_hz)
            for value in valid_durations
        ]
        matched_multiples = [
            value for value in refresh_multiples if value is not None
        ]
        if len(matched_multiples) == len(refresh_multiples):
            total_refreshes = sum(matched_multiples)
            if total_refreshes > 0:
                return float(refresh_hz) * len(refresh_multiples) / total_refreshes

    p50_ms = _percentile(valid_durations, 50)
    if p50_ms <= 0:
        return None
    return 1000.0 / p50_ms


def lookup_pymobiledevice3_pid(record: dict) -> int:
    value = _lookup(record, ("pid", "processIdentifier", "processID"))
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def find_pymobiledevice3_process(
    json_obj: Any,
    *,
    pid: int = 0,
    bundle_id: str = "",
    label: str = "",
) -> Optional[dict]:
    if pid:
        record = _find_record_for_pid(json_obj, pid)
        if record is not None:
            return record

    needles = [value.lower() for value in (bundle_id, label) if value]
    if needles:
        for record in _walk_dicts(json_obj):
            if any(_contains_value(record, needle) for needle in needles):
                return record
    return None


def parse_pymobiledevice3_process_snapshot(
    json_obj: Any,
    *,
    pid: int = 0,
    bundle_id: str = "",
    label: str = "",
) -> dict:
    """Parse one ``pymobiledevice3 developer dvt sysmon process single`` sample."""
    record = find_pymobiledevice3_process(
        json_obj,
        pid=pid,
        bundle_id=bundle_id,
        label=label,
    )
    if record is None:
        return {
            "cpu": CpuStats(pid=pid),
            "memory": MemStats(),
            "frames": FrameStats(),
            "process": {},
        }

    cpu_value = _lookup(
        record,
        (
            "cpuUsage",
            "cpuPercent",
            "percentCPU",
            "CPU",
        ),
    )
    mem_value = _lookup(
        record,
        (
            "physFootprint",
            "residentSize",
            "residentMemory",
            "realMemory",
            "memory",
        ),
    )
    fps_value = _lookup(record, ("fps", "framesPerSecond", "frameRate", "displayFPS"))
    mem_percent = _lookup(record, ("memPercent", "memoryPercent"))
    actual_pid = lookup_pymobiledevice3_pid(record) or pid
    cpu = CpuStats(
        pid=actual_pid,
        cpu_percent=_parse_percent(cpu_value),
        mem_percent=_parse_percent(mem_percent) or 0.0,
    )
    mem_kb = _parse_size_kb(mem_value)
    memory = MemStats(pss_total_kb=mem_kb, rss_total_kb=mem_kb)
    fps = _parse_number(fps_value)
    frames = (
        FrameStats(fps_estimate=_clamp_fps(fps, refresh_hz=120), source="ios_sysmontap_reported_fps", scope="unverified")
        if fps is not None
        else FrameStats()
    )
    return {"cpu": cpu, "memory": memory, "frames": frames, "process": record}


def parse_pymobiledevice3_process_sample(json_obj: Any, pid: int) -> Tuple[CpuStats, MemStats]:
    parsed = parse_pymobiledevice3_process_snapshot(json_obj, pid=pid)
    return parsed["cpu"], parsed["memory"]


def parse_activity_monitor(xml_text: str, pid: int) -> Tuple[CpuStats, MemStats]:
    """Parse Activity Monitor XML export for a single pid.

    Aggregation rule: mean of the last N=5 samples (see PLAN-ios §5, Phase i5).
    Memory bytes are converted to KB; on iOS, ``pss`` and ``rss`` are both set
    to the resident value. Java/native/graphics heaps stay 0.

    Returns ``(CpuStats(pid=pid), MemStats())`` on empty/missing input.

    """
    root = _parse_xml(xml_text)
    if root is None:
        return CpuStats(pid=pid), MemStats()

    cpu_samples: List[float] = []
    mem_samples: List[int] = []
    for row in _xml_rows(root):
        if pid and not _row_matches_pid(row, pid):
            continue
        cpu_value = _find_value(
            row,
            ("cpuusage", "cpupercent", "percentcpu", "cpu"),
            skip=("time", "duration"),
        )
        mem_value = _find_value(
            row,
            (
                "residentmemorysize",
                "residentmemory",
                "realmemory",
                "physicalmemory",
                "dirtymemory",
                "memory",
            ),
            skip=("virtual", "compressed", "percent", "cpu"),
        )
        cpu = _parse_percent(cpu_value)
        mem = _parse_size_kb(mem_value)
        if cpu is not None:
            cpu_samples.append(cpu)
        if mem is not None:
            mem_samples.append(mem)

    tail_cpu = cpu_samples[-5:]
    tail_mem = mem_samples[-5:]
    cpu_avg = sum(tail_cpu) / len(tail_cpu) if tail_cpu else 0.0
    mem_avg = int(round(sum(tail_mem) / len(tail_mem))) if tail_mem else 0
    return CpuStats(pid=pid, cpu_percent=cpu_avg), MemStats(
        pss_total_kb=mem_avg,
        rss_total_kb=mem_avg,
    )


def _parse_duration_ms(value: Any) -> Optional[float]:
    number = _parse_number(value)
    if number is None:
        return None
    text = str(value).strip().lower()
    if "ns" in text:
        return number / 1_000_000
    if "us" in text or "µs" in text:
        return number / 1_000
    if "ms" in text:
        return number
    if re.search(r"\bs\b", text):
        return number * 1000
    if number > 1_000_000:
        return number / 1_000_000
    if 0 < number < 1:
        return number * 1000
    return number


def _parse_seconds(value: Any) -> Optional[float]:
    number = _parse_number(value)
    if number is None:
        return None
    text = str(value).strip().lower()
    if "ns" in text:
        return number / 1_000_000_000
    if "us" in text or "µs" in text:
        return number / 1_000_000
    if "ms" in text:
        return number / 1000
    return number


def _percentile(values: List[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * pct / 100) - 1))
    return ordered[idx]


def parse_core_animation(xml_text: str, refresh_hz: int = 60) -> FrameStats:
    """Parse Core Animation XML export into :class:`FrameStats`.

    Jank threshold is ``1000 / refresh_hz`` ms (60 Hz -> 16.666 ms,
    120 Hz ProMotion -> 8.333 ms). Percentiles come from the full frame
    duration list; ``fps_estimate`` normally uses the monotonic-timestamp
    algorithm shared with Android (``(n-1) / (t_last - t_first)``), but falls
    back to frame durations when burst timestamps would exceed the iOS display
    ceiling. Integer-ms duration buckets are cadence-restored only when every
    bucket matches the supplied refresh period; high-resolution/VRR durations
    retain the historical raw p50 estimate.

    The Android-specific counters (``missed_vsync``, ``slow_ui_thread``,
    ``frame_deadline_missed``) stay 0.

    """
    root = _parse_xml(xml_text)
    if root is None:
        return FrameStats()

    durations_ms: List[float] = []
    timestamps_s: List[float] = []
    fps_values: List[float] = []
    raw_events = []
    raw_seen = set()
    raw_dropped = 0
    stream_id = hashlib.sha256(xml_text.encode("utf-8")).hexdigest()[:16]
    for row in _xml_rows(root):
        duration_value = _find_value(
            row,
            (
                "frameduration",
                "displayduration",
                "renderduration",
                "duration",
            ),
            skip=("timestamp", "start", "end", "cpu"),
        )
        timestamp_value = _find_value(
            row,
            ("timestamp", "starttime", "sampletime", "time"),
            skip=("duration",),
        )
        fps_value = _find_value(row, ("framespersecond", "fps"), skip=("drop",))

        duration = _parse_duration_ms(duration_value)
        timestamp = _parse_seconds(timestamp_value)
        fps = _parse_number(fps_value)
        if duration is not None and duration > 0:
            durations_ms.append(duration)
            if timestamp is not None and timestamp >= 0:
                timestamp_ns = round(timestamp * 1_000_000_000)
                if timestamp_ns not in raw_seen:
                    raw_seen.add(timestamp_ns)
                    if len(raw_events) < 4096:
                        raw_events.append({"timestamp_ns": timestamp_ns, "duration_ns": round(duration * 1_000_000),
                                           "source": "ios_xctrace_core_animation", "scope": "trace_unverified", "stream_id": stream_id})
                    else:
                        raw_dropped += 1
        if timestamp is not None:
            timestamps_s.append(timestamp)
        if fps is not None:
            fps_values.append(fps)

    stats = FrameStats(source="ios_xctrace_core_animation", scope="trace_unverified", raw_events=raw_events, raw_events_dropped=raw_dropped)
    threshold_ms = 1000.0 / max(refresh_hz, 1)
    if durations_ms:
        # A frame slower than one display interval is janky; one slower than two
        # intervals is a big jank. small_jank + big_jank == janky_frames.
        big_threshold_ms = threshold_ms * 2.0
        stats.total_frames = len(durations_ms)
        stats.small_jank = sum(
            1 for value in durations_ms if threshold_ms < value <= big_threshold_ms
        )
        stats.big_jank = sum(1 for value in durations_ms if value > big_threshold_ms)
        stats.janky_frames = stats.small_jank + stats.big_jank
        stats.jank_percent = stats.janky_frames / stats.total_frames * 100.0
        stats.p50_ms = _percentile(durations_ms, 50)
        stats.p90_ms = _percentile(durations_ms, 90)
        stats.p95_ms = _percentile(durations_ms, 95)
        stats.p99_ms = _percentile(durations_ms, 99)
    elif fps_values:
        stats.total_frames = len(fps_values)

    unique_times = sorted(set(timestamps_s))
    duration_fps = _fps_from_durations(durations_ms, refresh_hz=refresh_hz)
    max_fps = _max_ios_fps(refresh_hz)
    if len(unique_times) >= 2:
        span = unique_times[-1] - unique_times[0]
        if span > 0:
            fps_estimate = (len(unique_times) - 1) / span
            if fps_estimate <= max_fps:
                stats.fps_estimate = fps_estimate
            elif duration_fps is not None:
                # The timestamp span implies an impossible rate (burst export or
                # coarse 1 Hz sampling); fall back to the per-frame duration
                # estimate. Only fully cadence-matching integer-ms buckets are
                # reconstructed; direct/timestamp FPS is never tier-snapped.
                stats.fps_estimate = _clamp_fps(duration_fps, refresh_hz=refresh_hz)
            # else: leave fps_estimate unset rather than fabricating the ceiling
            # as if it were a real reading.
    elif fps_values:
        stats.fps_estimate = _clamp_fps(
            sum(fps_values) / len(fps_values),
            refresh_hz=refresh_hz,
        )

    return stats
