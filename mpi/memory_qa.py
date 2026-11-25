#!/usr/bin/env python3
"""Read-only QA for one authorized ADB target, package and device."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
from math import ceil
from pathlib import Path
import re
import subprocess
import sys
import time

RULES = json.loads(Path(__file__).with_name("play-memory-rules.json").read_text(encoding="utf-8-sig"))
FIELDS = ("RssAnon", "VmSwap")
FORM_FACTORS = ("phone_tablet", "other", "unknown")
MAX_PROCESSES = 4
COMMAND_TIMEOUT_SECONDS = 1.5
BITMAP_SOURCE = "dumpsys_meminfo_native_allocations"
BITMAP_COLUMNS = ("timestamp_utc", "sample_index", "device", "package", "process", "pid", "start_ticks",
                  "malloced_count", "malloced_kib", "nonmalloced_count", "nonmalloced_kib",
                  "bitmap_kib", "bitmap_source", "bitmap_status", "omitted_processes")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
SUFFIX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
COLUMNS = ("timestamp_utc", "sample_index", "device", "package", "category", "state",
           "state_source", "form_factor", "mem_total_kib", "sdk_int", "process", "pid", "start_ticks", "rss_anon_kib", "vm_swap_kib",
           "anon_rss_plus_swap_kib", "bitmap_kib", "bitmap_status", "sample_status", "omitted_processes")


class AdbError(RuntimeError):
    pass


def valid_serial(serial):
    """Allow USB/emulator serials and ADB IP literals, never options or shell text."""
    return (isinstance(serial, str) and not serial.startswith("-")
            and re.fullmatch(r"[A-Za-z0-9_.:%\[\]-]{1,128}", serial) is not None)


class Adb:
    def __init__(self, adb, serial):
        self.prefix = [adb, "-s", serial]

    def read(self, *args):
        try:
            result = subprocess.run(self.prefix + list(args), capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS,
                                    text=True, encoding="utf-8", errors="replace", check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdbError("ADB unavailable or read timed out") from error
        if result.returncode:
            raise AdbError("ADB read failed (device, shell permission, or process state)")
        return result.stdout

    def get_state(self):
        return self.read("get-state").strip()

    def processes(self, package):
        rows = self.read("shell", "ps", "-A", "-o", "PID,NAME").splitlines()
        if not rows or rows[0].split() != ["PID", "NAME"]:
            raise AdbError("device does not expose the supported ps PID,NAME format")
        return parse_processes(rows[1:], package)

    def status(self, pid):
        return self.read("shell", "cat", f"/proc/{pid}/status")

    def start_ticks(self, pid):
        return parse_start_ticks(self.read("shell", "cat", f"/proc/{pid}/stat"), pid)

    def bitmap_dump(self, pid):
        return self.read("shell", "dumpsys", "meminfo", "-s", str(pid))

    def oom_score_adj(self, pid):
        try:
            value = self.read("shell", "cat", f"/proc/{pid}/oom_score_adj").strip()
            return int(value) if re.fullmatch(r"-?[0-9]{1,4}", value) else None
        except AdbError:
            return None

    def sdk_int(self):
        raw = self.read("shell", "getprop", "ro.build.version.sdk").strip()
        return int(raw) if re.fullmatch(r"[0-9]{1,4}", raw) else None

    def mem_total(self):
        return read_kib(self.read("shell", "cat", "/proc/meminfo"), "MemTotal")


def read_kib(text, field):
    keys = [line for line in text.splitlines() if line.startswith(field + ":")]
    if len(keys) != 1:
        return None
    match = re.fullmatch(re.escape(field) + r":\s*([0-9]{1,18})\s+kB\s*", keys[0])
    return int(match[1]) if match else None


def parse_processes(lines, package):
    found = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        pid, name = parts
        if not pid.isascii() or not pid.isdecimal() or len(pid) > 10 or int(pid) < 1:
            continue
        if name == package or (name.startswith(package + ":") and SUFFIX.fullmatch(name[len(package)+1:])):
            found[int(pid)] = name
    return found


def parse_start_ticks(text, pid):
    """Linux proc stat field 22; comm can contain spaces and parentheses."""
    marker = text.rfind(")")
    if marker < 0 or not text.startswith(f"{pid} ("):
        return None
    fields = text[marker + 1:].split()
    return int(fields[19]) if len(fields) > 19 and re.fullmatch(r"[0-9]{1,18}", fields[19]) else None


def oom_state(score):
    if type(score) is not int or not 0 <= score <= 1000:
        return "unknown"
    if score < 200:
        return "foreground"
    if score < 250:
        return "user_perceived_services"
    if score < 900:
        return "background"
    return "cached"


def read_sample(adb, package, base):
    auto = base.get("state") == "auto"
    form_factor = base.get("form_factor", "unknown")
    if form_factor not in FORM_FACTORS:
        form_factor = "unknown"

    def row(status, pid=None, name=None, numbers=(None, None), start_ticks=None, score=None, omitted=0):
        rss, swap = numbers
        state = oom_state(score) if auto else base["state"]
        state_source = ("unknown" if state == "unknown" else "measured_oom_score_adj") if auto else base["state_source"]
        return {**base, "state": state, "state_source": state_source, "form_factor": form_factor, "process": name, "pid": pid, "start_ticks": start_ticks, "rss_anon_kib": rss, "vm_swap_kib": swap,
                "anon_rss_plus_swap_kib": rss + swap if status == "ok" else None,
                "bitmap_kib": None, "bitmap_status": "unsupported_no_bitmap_source", "sample_status": status, "omitted_processes": omitted}
    try:
        before = adb.processes(package)
    except AdbError:
        return [row("process_listing_unavailable")]
    if not before:
        return [row("process_not_observed")]
    selected = sorted(before.items(), key=lambda item:(item[1] != package, item[0]))[:MAX_PROCESSES]
    omitted = len(before) - len(selected)
    pending = {}
    for pid, name in selected:
        try:
            start_ticks = adb.start_ticks(pid)
            if start_ticks is None:
                pending[pid] = row("pid_identity_unverified", pid, name)
                continue
            score_before = adb.oom_score_adj(pid) if auto else None
            data = adb.status(pid)
            score_after = adb.oom_score_adj(pid) if auto else None
            score = score_before if score_before == score_after else None
            if adb.start_ticks(pid) != start_ticks:
                pending[pid] = row("process_changed", pid, name)
                continue
            reported_pid = [line.split(":")[1].strip() for line in data.splitlines() if line.startswith("Pid:")]
            parts = tuple(read_kib(data, key) for key in FIELDS)
            if reported_pid != [str(pid)]:
                pending[pid] = row("pid_identity_unverified", pid, name)
            elif all(v is not None for v in parts):
                pending[pid] = row("ok", pid, name, parts, start_ticks, score)
            else:
                pending[pid] = row("proc_fields_partial" if any(v is not None for v in parts) else "proc_fields_unsupported",
                                   pid, name, parts, start_ticks, score)
        except AdbError:
            pending[pid] = row("proc_status_unavailable", pid, name)
    overflow = [row("process_listing_capped", omitted=omitted)] if omitted else []
    try:
        after = adb.processes(package)
    except AdbError:
        return [row("process_identity_unverified", pid, name) for pid, name in selected] + overflow
    return [pending[pid] if after.get(pid) == name else row("process_changed", pid, name)
            for pid, name in selected] + overflow


def parse_bitmap_fields(text, pid, process):
    """Read individually observed bitmap fields inside one exact PID/process block."""
    lines = text.splitlines()
    headings = [(i,line.strip()) for i,line in enumerate(lines) if line.lstrip().startswith("** MEMINFO in pid ")]
    if len(headings) != 1 or headings[0][1] != f"** MEMINFO in pid {pid} [{process}] **":
        return None
    sections = [i for i, line in enumerate(lines) if line.strip() == "Native Allocations"]
    if len(sections) != 1 or sections[0] <= headings[0][0]:
        return None
    i = sections[0] + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not re.fullmatch(r"\s*Count\s+Total\(kB\)\s*", lines[i]):
        return None
    i += 1
    if i < len(lines) and re.fullmatch(r"\s*-{2,}\s+-{2,}\s*", lines[i]):
        i += 1
    rows = {}
    for line in lines[i:]:
        if not line.strip():
            break
        match = re.fullmatch(r"\s*([^:]+):\s*([0-9]{1,18})\s+([0-9]{1,18})\s*", line)
        if not match:
            if line.lstrip().startswith(("Bitmap (malloced)", "Bitmap (nonmalloced)")):
                return None
            break
        kind = match[1].strip()
        if kind in ("Bitmap (malloced)", "Bitmap (nonmalloced)"):
            if kind in rows:
                return None
            rows[kind] = (int(match[2]), int(match[3]))
    if not rows:
        return None
    return (*rows.get("Bitmap (malloced)", (None, None)),
            *rows.get("Bitmap (nonmalloced)", (None, None)))


def parse_bitmap_dump(text, pid, process):
    """Return a total-ready pair only if both exact bitmap categories exist."""
    values = parse_bitmap_fields(text, pid, process)
    return values if values is not None and all(value is not None for value in values) else None


def read_bitmap_sample(adb, package, base, *, limit=MAX_PROCESSES):
    """Independent timestamped local attribution; never blended into RSS or Play P90."""
    if type(limit) is not int or not 1 <= limit <= MAX_PROCESSES:
        raise ValueError("bitmap process limit out of range")
    common = {k: base.get(k) for k in ("sample_index", "device", "package")}
    def row(status, pid=None, name=None, ticks=None, numbers=None, omitted=0):
        mc, mk, nc, nk = numbers or (None, None, None, None)
        return dict(**common, timestamp_utc=datetime.now(timezone.utc).isoformat(), process=name, pid=pid,
                    start_ticks=ticks, malloced_count=mc, malloced_kib=mk, nonmalloced_count=nc, nonmalloced_kib=nk,
                    bitmap_kib=mk + nk if mk is not None and nk is not None else None, bitmap_source=BITMAP_SOURCE,
                    bitmap_status=status, omitted_processes=omitted)
    try:
        before = adb.processes(package)
    except AdbError:
        return [row("process_listing_unavailable")]
    if not before:
        return [row("process_not_observed")]
    selected = sorted(before.items(), key=lambda item:(item[1] != package, item[0]))[:limit]
    result = []
    for pid, name in selected:
        try:
            ticks = adb.start_ticks(pid)
            if ticks is None:
                result.append(row("pid_identity_unverified", pid, name))
                continue
            text = adb.bitmap_dump(pid)
            if adb.start_ticks(pid) != ticks:
                result.append(row("process_changed", pid, name))
                continue
            numbers = parse_bitmap_fields(text, pid, name)
            status = ("bitmap_fields_unavailable" if numbers is None else
                      "bitmap_fields_partial" if None in numbers else "ok")
            result.append(row(status, pid, name, ticks, numbers))
        except AdbError:
            result.append(row("bitmap_read_unavailable", pid, name))
    omitted = len(before) - len(selected)
    overflow = [row("process_listing_capped", omitted=omitted)] if omitted else []
    try:
        after = adb.processes(package)
    except AdbError:
        return [row("process_identity_unverified", pid, name) for pid, name in selected] + overflow
    return [sample if after.get(pid) == name else row("process_changed", pid, name)
            for sample, (pid, name) in zip(result, selected)] + overflow


def ram_tier_index(mem_kib):
    if (type(mem_kib) is not int or mem_kib <= 0 or
            mem_kib > RULES["max_mem_total_inclusive_mib"] * 1024):
        return None
    return max(i for i, boundary in enumerate(RULES["tier_lower_inclusive_mib"]) if mem_kib >= boundary * 1024)


def threshold(category, state, mem_kib):
    tier = ram_tier_index(mem_kib)
    if tier is None or state not in RULES["state_order"] or category not in RULES["anonymous_rss_plus_swap_limits_mib"]:
        return None
    value = RULES["anonymous_rss_plus_swap_limits_mib"][category][tier][RULES["state_order"].index(state)]
    return None if value is None else Decimal(str(value)) * 1024


def summarize(rows):
    groups = {}
    for row in rows:
        if row["process"] is None:
            continue
        key = tuple(row[k] for k in ("device", "package", "category", "state", "state_source",
                                     "mem_total_kib", "sdk_int", "process", "pid", "start_ticks"))
        raw_form = row.get("form_factor", "unknown")
        key += (raw_form if raw_form in FORM_FACTORS else "unknown",)
        groups.setdefault(key, []).append(row)
    out = []
    for key, group in groups.items():
        device, package, category, state, state_source, total, sdk, name, pid, start_ticks, form_factor = key
        good = sorted(r["anon_rss_plus_swap_kib"] for r in group if r["sample_status"] == "ok")
        p90 = good[ceil(len(good) * .9) - 1] if good else None
        cap = threshold(category, state, total) if form_factor == "phone_tablet" and type(sdk) is int and sdk >= RULES["android_min_sdk"] else None
        tier = ram_tier_index(total)
        out.append(dict(device=device, package=package, category=category, state=state, state_source=state_source, form_factor=form_factor,
                        mem_total_kib=total, ram_tier=RULES["tier_labels"][tier] if tier is not None else None, sdk_int=sdk, process=name, pid=pid, start_ticks=start_ticks, n_valid=len(good), n_rows=len(group),
                        local_p90_kib=p90, threshold_local_kib=str(cap) if cap is not None else None,
                        comparison=("not_compared" if p90 is None or cap is None else
                                    "above_reference" if p90 > cap else "at_or_below_reference"),
                        play_vitals_result=False, bitmap_status="unsupported_no_bitmap_source"))
    return out


def cli(argv=None, adb_factory=Adb, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="already-authorized device serial from adb devices")
    parser.add_argument("--package", required=True, help="exact Android package, optional :process children observed")
    parser.add_argument("--category", required=True, choices=["app", "game"], help="choose Play listing category explicitly")
    parser.add_argument("--state", default="unknown", choices=[*RULES["state_order"], "unknown", "auto"],
                        help="manual state or auto using verified per-process oom_score_adj; default unknown")
    parser.add_argument("--form-factor", default="unknown", choices=FORM_FACTORS,
                        help="confirm phone/tablet for local reference comparisons; default unknown")
    parser.add_argument("--samples", type=int, default=10, help="sampling iterations (1-600)")
    parser.add_argument("--interval", type=float, default=3.0, help="minimum seconds between iterations (1-3600)")
    parser.add_argument("--bitmap", action="store_true", help="separate slow dumpsys Native Allocations diagnostics, never changes local RSS summaries")
    parser.add_argument("--adb", default="adb", help="path to locally installed adb executable")
    parser.add_argument("--output", required=True, type=Path, help="new filename stem; creates .json and .csv, never overwrites")
    args = parser.parse_args(argv)
    if not PACKAGE.fullmatch(args.package) or not valid_serial(args.serial):
        parser.error("enter one valid Android package and authorized ADB serial")
    if not 1 <= args.samples <= 600 or not 1 <= args.interval <= 3600:
        parser.error("samples or interval out of range")
    csv_path, json_path = (Path(str(args.output) + ext) for ext in (".csv", ".json"))
    bitmap_path = Path(str(args.output) + ".bitmap.csv") if args.bitmap else None
    if csv_path.exists() or json_path.exists() or bitmap_path is not None and bitmap_path.exists():
        parser.error("output files already exist; choose another stem")
    adb = adb_factory(args.adb, args.serial)
    try:
        if adb.get_state() != "device":
            raise AdbError("not an authorized attached device")
    except AdbError as error:
        parser.error(str(error))
    try:
        total = adb.mem_total()
        total = total if total and total > 0 else None
    except AdbError:
        total = None
    try:
        sdk = adb.sdk_int()
    except AdbError:
        sdk = None
    rows, bitmap_rows = [], []
    for i in range(args.samples):
        base = dict(timestamp_utc=datetime.now(timezone.utc).isoformat(), sample_index=i+1, device=args.serial,
                    package=args.package, category=args.category, state=args.state,
                    state_source="unknown" if args.state == "unknown" else "operator_label", form_factor=args.form_factor, mem_total_kib=total, sdk_int=sdk)
        rows += read_sample(adb, args.package, base)
        if args.bitmap:
            bitmap_rows += read_bitmap_sample(adb, args.package, base)
        if i + 1 < args.samples:
            sleep(args.interval)
    with csv_path.open("x", newline="", encoding="utf-8") as fd:
        writer = csv.DictWriter(fd, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    if bitmap_path is not None:
        with bitmap_path.open("x", newline="", encoding="utf-8") as fd:
            writer = csv.DictWriter(fd, fieldnames=BITMAP_COLUMNS)
            writer.writeheader()
            writer.writerows(bitmap_rows)
    with json_path.open("x", encoding="utf-8") as fd:
        json.dump({"rules": RULES, "sample_rows": rows, "local_summary": summarize(rows),
                   **({"bitmap_diagnostics": bitmap_rows} if args.bitmap else {}),
                   "play_vitals_result": False, "rollup_scope": "one labeled workload, per process instance (name, PID, start ticks)"},
                  fd, ensure_ascii=False, indent=2)
        fd.write("\n")
    valid = sum(r["sample_status"] == "ok" for r in rows)
    print(f"Local QA only: {valid}/{len(rows)} usable process rows. {csv_path} / {json_path}")
    if bitmap_path is not None:
        print(f"Separate bitmap diagnostics: {bitmap_path}")
    return 0 if valid else 2


if __name__ == "__main__":
    sys.exit(cli())
