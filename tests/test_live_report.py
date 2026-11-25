"""End-to-end checks for the live-capture Excel report (api._handle_export_live_xlsx).

Guards the four report fields that drift from the real measurement:
  * Date     -> normalized to YYYY-MM-DD (no time component)
  * Duration -> real elapsed wall-clock span, NOT the sample count
  * Temperature(Start/End/Max) -> fed from the battery thermal zone
  * Avg Power -> mean of |V(mV) x I(mA)| / 1000 over the samples
"""

from __future__ import annotations

import re

import openpyxl
import pytest

from mpi import api

# Column layout of a data row in the generated workbook (1-based), per
# mpi/reports/excel_export.py._write_data_row.
COL_DATE = 8
COL_DURATION = 9
COL_TEMP_START = 15
COL_TEMP_END = 16
COL_TEMP_MAX = 17
COL_POWER = 18


def _snapshot(captured_at: str, batt_temp: float) -> dict:
    """One serialized snapshot: 4000 mV x 1000 mA => 4000 mW power each."""
    return {
        "device": {"serial": "S1", "platform": "android", "model": "GalaxyTest", "os_version": "14"},
        "package": {"bundle_id": "com.test.game", "label": "TestGame", "version_name": "1.2.3"},
        "captured_at": captured_at,
        "duration_sec": 30.0,
        "frames": {"fps_estimate": 60.0},
        "memory": {"rss_total_kb": 100000},
        "cpu": {"cpu_percent": 25.0, "total_percent": 40.0},
        "gpu": {"busy_percent": 50.0},
        "thermal": {"zones_c": {"battery": batt_temp}},
        "battery": {"level_percent": 80, "voltage_mv": 4000.0, "current_ma": 1000.0},
    }


def _find_data_row(ws) -> int:
    """Locate the single data row by its YYYY-MM-DD Date cell."""
    for r in range(1, ws.max_row + 1):
        val = ws.cell(row=r, column=COL_DATE).value
        if isinstance(val, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
            return r
    raise AssertionError("no data row with a YYYY-MM-DD Date cell found")


@pytest.fixture
def report_ws(tmp_path):
    # Five samples spanning exactly 120 s (10:00:00 -> 10:02:00). The old
    # count-based duration would report 5 s; the real span is 2m 0s.
    times = [
        "2026-06-26T10:00:00",
        "2026-06-26T10:00:30",
        "2026-06-26T10:01:00",
        "2026-06-26T10:01:30",
        "2026-06-26T10:02:00",
    ]
    temps = [30.0, 31.5, 35.0, 34.0, 33.0]  # start 30.0, end 33.0, max 35.0
    snapshots = [_snapshot(t, temp) for t, temp in zip(times, temps)]

    save_path = tmp_path / "live_report.xlsx"
    result = api._handle_export_live_xlsx({"save_path": str(save_path), "snapshots": snapshots})
    assert result["count"] == 5
    assert save_path.exists()

    wb = openpyxl.load_workbook(save_path)
    return wb.active


def test_date_is_yyyy_mm_dd(report_ws):
    row = _find_data_row(report_ws)
    assert report_ws.cell(row=row, column=COL_DATE).value == "2026-06-26"


def test_duration_is_real_elapsed_span_not_sample_count(report_ws):
    row = _find_data_row(report_ws)
    # Real elapsed span is 120 s -> "2m 0s". A sample-count formula would give
    # "0m 5s" (5 samples), so this asserts the span-based fix.
    assert report_ws.cell(row=row, column=COL_DURATION).value == "2m 0s"


def test_battery_temperature_start_end_max(report_ws):
    row = _find_data_row(report_ws)
    assert report_ws.cell(row=row, column=COL_TEMP_START).value == 30.0
    assert report_ws.cell(row=row, column=COL_TEMP_END).value == 33.0
    assert report_ws.cell(row=row, column=COL_TEMP_MAX).value == 35.0


def test_avg_power_is_populated(report_ws):
    row = _find_data_row(report_ws)
    # |4000 mV x 1000 mA| / 1000 = 4000 mW for every sample -> avg 4000.0
    assert report_ws.cell(row=row, column=COL_POWER).value == 4000.0
