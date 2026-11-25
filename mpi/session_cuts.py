"""Named, non-destructive measurement intervals stored in session metadata."""
from __future__ import annotations

import csv
import json
import math
import os
import tempfile
import threading
from typing import Any

_write_lock = threading.Lock()


def validate_cuts(value: Any, duration: float) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("cuts must be a list of at most 200 intervals")
    result = []
    identifiers = set()
    for cut in value:
        if not isinstance(cut, dict):
            raise ValueError("cut must be an object")
        identifier, name = cut.get("id"), cut.get("name")
        start, end = cut.get("start_sec"), cut.get("end_sec")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 100 or identifier in identifiers:
            raise ValueError("cut id must be unique and non-empty")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("cut name must contain 1 to 100 characters")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, end)):
            raise ValueError("cut times must be finite numbers")
        if not 0 <= start < end <= duration + 0.000001:
            raise ValueError("cut must satisfy 0 <= start_sec < end_sec <= session duration")
        identifiers.add(identifier)
        result.append({"id": identifier, "name": name.strip(), "start_sec": float(start), "end_sec": float(end)})
    return result


def read_cuts(metadata: dict[str, str], duration: float) -> list[dict[str, Any]]:
    try:
        return validate_cuts(json.loads(metadata.get("session.cuts", "[]")), duration)
    except (ValueError, TypeError):
        return []


def save_cuts(path: str, cuts: Any, expected: Any = None) -> list[dict[str, Any]]:
    # Preserve every original metric/identity row, including unknown future fields.
    from .analysis import _read_sections, _duration_seconds, _time_axis

    with _write_lock:
        metadata, _, samples = _read_sections(path)
        duration = _duration_seconds(samples, metadata)
        validated = validate_cuts(cuts, duration)
        if expected is not None and read_cuts(metadata, duration) != expected:
            raise ValueError("session cuts changed; reload before saving")
        axis = _time_axis(samples)
        if any(not any(t is not None and cut["start_sec"] <= t <= cut["end_sec"] for t in axis) for cut in validated):
            raise ValueError("cut contains no samples")
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
        section = None
        updated = False
        for index, row in enumerate(rows):
            if len(row) >= 2 and row[0] == "# Section":
                section = row[1]
            elif section == "Metadata" and row and row[0] == "session.cuts":
                rows[index] = ["session.cuts", encoded]
                updated = True
        if not updated:
            index = next(i for i, row in enumerate(rows) if row[:2] == ["# Section", "Metadata"])
            rows.insert(index + 2, ["session.cuts", encoded])
        fd, temporary = tempfile.mkstemp(prefix=".mpi-cuts-", suffix=".tmp", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerows(rows)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return validated
