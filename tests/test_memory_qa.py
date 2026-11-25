import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mpi.memory_qa import AdbError, cli, parse_bitmap_dump, parse_processes, parse_start_ticks, read_bitmap_sample, read_kib, read_sample, summarize, threshold

PACKAGE = "com.mpi.companion"
BASE = dict(timestamp_utc="2026-11-24T00:00:00Z", sample_index=1, device="fixture",
            package=PACKAGE, category="app", state="foreground", state_source="operator_label",
            mem_total_kib=3200 * 1024, sdk_int=33, form_factor="phone_tablet")


BITMAP_ROWS = (
    " Native Allocations\n"
    "                     Count        Total(kB)\n"
    "                    ------          ------\n"
    "     Bitmap (malloced):  2            1024\n"
    "  Bitmap (nonmalloced):  3            2048\n"
)


def bitmap(pid=12, process=PACKAGE, rows=BITMAP_ROWS):
    return f"Applications Memory Usage (in Kilobytes):\n\n** MEMINFO in pid {pid} [{process}] **\n" + rows




class Fixture:
    def __init__(self, *_):
        self.rosters = [{12: PACKAGE, 13: PACKAGE + ":worker"}, {12: PACKAGE, 13: PACKAGE + ":worker"}]
        self.startTimes = {12: [20, 20], 13: [21, 21]}

    def processes(self, _):
        return self.rosters.pop(0)

    def start_ticks(self, pid):
        return self.startTimes[pid].pop(0)

    def status(self, pid):
        return f"Pid:\t{pid}\nRssAnon: 1024 kB\n" + ("VmSwap: 0 kB\n" if pid == 12 else "")

    def bitmap_dump(self, pid):
        return bitmap(pid, PACKAGE if pid == 12 else PACKAGE + ":worker")

    def get_state(self):
        return "device"

    def mem_total(self):
        return 3200 * 1024

    def sdk_int(self):
        return 33


class MemoryQaTest(unittest.TestCase):
    def test_exact_package_matching(self):
        self.assertEqual(parse_processes(["123 "+PACKAGE, "321 "+PACKAGE+":child", "12 "+PACKAGE+"evil",
                                          "33 other.app", "-1 "+PACKAGE, "XX "+PACKAGE], PACKAGE),
                         {123: PACKAGE, 321: PACKAGE+":child"})

    def test_fields_do_not_substitute_rss_pss_or_wrong_unit(self):
        self.assertEqual(read_kib("VmRSS: 1000 kB\nRssAnon: 10 kB", "RssAnon"), 10)
        self.assertIsNone(read_kib("RssAnon: 10 MB", "RssAnon"))
        self.assertIsNone(read_kib("VmSwap: 0 kB\nVmSwap: 1 kB", "VmSwap"))
        self.assertEqual(parse_start_ticks("12 (with ) parens) S " + "0 "*18 + "77", 12), 77)
        self.assertIsNone(parse_start_ticks("13 (with ) parens) S " + "0 "*18 + "77", 12))

    def test_per_process_partial_zero_and_pid_reuse(self):
        records = read_sample(Fixture(), PACKAGE, BASE)
        self.assertEqual([r["sample_status"] for r in records], ["ok", "proc_fields_partial"])
        self.assertEqual([r["anon_rss_plus_swap_kib"] for r in records], [1024, None])
        self.assertEqual([r["bitmap_kib"] for r in records], [None, None])
        changed = Fixture()
        changed.startTimes[12][1] = 47
        changed.rosters[1].pop(13)
        self.assertEqual([r["sample_status"] for r in read_sample(changed, PACKAGE, BASE)],
                         ["process_changed", "process_changed"])

    def test_permission_missing_or_target_not_running(self):
        f = Fixture()
        def no(*_):
            raise AdbError("fixture permission denied")
        f.processes = no
        self.assertEqual(read_sample(f, PACKAGE, BASE)[0]["sample_status"], "process_listing_unavailable")
        f.processes = lambda _: {}
        self.assertEqual(read_sample(f, PACKAGE, BASE)[0]["sample_status"], "process_not_observed")
        f = Fixture()
        f.status = no
        self.assertEqual(read_sample(f, PACKAGE, BASE)[0]["sample_status"], "proc_status_unavailable")

    def test_auto_state_stable_oom_and_bounded_pid_scope(self):
        f = Fixture()
        scores = {12: iter([50, 50]), 13: iter([220, 300])}
        f.oom_score_adj = lambda pid: next(scores[pid])
        self.assertEqual([(r["state"], r["state_source"]) for r in read_sample(f, PACKAGE, dict(BASE,state="auto"))],
                         [("foreground", "measured_oom_score_adj"), ("unknown", "unknown")])
        f = Fixture()
        f.rosters = [dict((i, PACKAGE if i==99 else PACKAGE+":side") for i in [1,2,3,4,5,99]) for _ in range(2)]
        f.startTimes = {i:[20,20] for i in [1,2,3,99]}
        rows=read_sample(f, PACKAGE, BASE)
        self.assertEqual([r["pid"] for r in rows], [99, 1, 2, 3, None])
        self.assertEqual(rows[-1]["omitted_processes"], 2)

    def test_category_and_ram_boundaries(self):
        self.assertIsNone(threshold("app", "foreground", 3200 * 1024 - 1))
        self.assertEqual(threshold("app", "foreground", 3200 * 1024), 2048 * 1024)
        self.assertEqual(threshold("game", "foreground", 4800 * 1024), 2816 * 1024)
        self.assertEqual(threshold("game", "background", 6800 * 1024), 2816 * 1024)
        self.assertEqual(threshold("app", "foreground", 9216 * 1024), 3328 * 1024)
        self.assertEqual(threshold("app", "foreground", 14336 * 1024), 4352 * 1024)
        self.assertEqual(threshold("app", "foreground", 18432 * 1024), 4352 * 1024)
        self.assertIsNone(threshold("app", "foreground", 18432 * 1024 + 1))
        self.assertIsNone(threshold("game", "cached", 9216 * 1024))
        self.assertIsNone(threshold("game", "unknown", 9216 * 1024))

    def test_local_nearest_rank_and_no_aggregation_across_instances(self):
        sample = read_sample(Fixture(), PACKAGE, BASE)[0]
        rows = [dict(sample, anon_rss_plus_swap_kib=i) for i in range(1, 11)]
        rows += [dict(sample, start_ticks=21), dict(sample, sdk_int=32), dict(sample, state="unknown")]
        good, reused, old_sdk, unknown = summarize(rows)
        self.assertEqual(good["local_p90_kib"], 9)
        self.assertEqual(good["n_valid"], 10)
        self.assertEqual(reused["n_valid"], 1)
        self.assertIsNone(old_sdk["threshold_local_kib"])
        self.assertIsNone(unknown["threshold_local_kib"])
        self.assertFalse(good["play_vitals_result"])

    def test_reference_is_restricted_to_explicit_phone_tablet_even_on_new_sdk(self):
        sample = read_sample(Fixture(), PACKAGE, BASE)[0]
        without = {k:v for k,v in sample.items() if k != "form_factor"}
        phone, other, unknown = summarize([sample, dict(sample, form_factor="other"), without,
                                           dict(sample, form_factor="invalid")])
        self.assertIsNotNone(phone["threshold_local_kib"])
        self.assertIsNone(other["threshold_local_kib"])
        self.assertIsNone(unknown["threshold_local_kib"])
        self.assertEqual(unknown["n_valid"], 2)
        self.assertEqual(other["local_p90_kib"], phone["local_p90_kib"])

    def test_bitmap_requires_exact_identity_section_and_complete_unique_pair(self):
        self.assertEqual(parse_bitmap_dump(bitmap(), 12, PACKAGE), (2, 1024, 3, 2048))
        zero = BITMAP_ROWS.replace("2            1024", "0  0").replace("3            2048", "0  0")
        self.assertEqual(parse_bitmap_dump(bitmap(rows=zero), 12, PACKAGE), (0, 0, 0, 0))
        for raw in [
            bitmap(pid=13), bitmap(process=PACKAGE+":worker"),
            bitmap(rows=BITMAP_ROWS.replace("Native Allocations", "Shared Bitmaps")),
            bitmap(rows=BITMAP_ROWS.replace("Total(kB)", "Size(KB)")),
            bitmap(rows=BITMAP_ROWS.split("  Bitmap (nonmalloced)")[0]),
            bitmap(rows=BITMAP_ROWS+"Bitmap (malloced): 1 1\n"),
            bitmap(rows=BITMAP_ROWS.replace("  Bitmap (nonmalloced)", "\nGraphics\n  Bitmap (nonmalloced)")),
            bitmap()+bitmap(pid=13), bitmap(rows=BITMAP_ROWS+"Native Allocations\n"),
            BITMAP_ROWS+bitmap(rows=""),
        ]:
            self.assertIsNone(parse_bitmap_dump(raw, 12, PACKAGE), raw)

    def test_bitmap_sampling_rechecks_identity_and_never_uses_partial_or_denied(self):
        f = Fixture()
        rows = read_bitmap_sample(f, PACKAGE, BASE, limit=1)
        self.assertEqual(rows[0]["bitmap_kib"], 3072)
        self.assertEqual(rows[0]["bitmap_source"], "dumpsys_meminfo_native_allocations")
        self.assertNotIn("state", rows[0])
        self.assertEqual(rows[1]["omitted_processes"], 1)
        f=Fixture()
        f.startTimes[12][1] = 99
        f.rosters[1].pop(13)
        self.assertEqual([r["bitmap_status"] for r in read_bitmap_sample(f, PACKAGE, BASE)],
                         ["process_changed", "process_changed"])
        f=Fixture()
        f.bitmap_dump=lambda pid: bitmap(pid, PACKAGE if pid == 12 else PACKAGE + ":worker",
                                          BITMAP_ROWS.split("  Bitmap (nonmalloced)")[0])
        partial = read_bitmap_sample(f, PACKAGE, BASE)[0]
        self.assertEqual(partial["bitmap_status"], "bitmap_fields_partial")
        self.assertEqual(partial["malloced_kib"], 1024)
        self.assertIsNone(partial["nonmalloced_kib"])
        self.assertIsNone(partial["bitmap_kib"])
        f=Fixture()
        f.bitmap_dump=lambda pid: "Permission Denial"
        self.assertIsNone(read_bitmap_sample(f, PACKAGE, BASE)[0]["bitmap_kib"])
        f=Fixture()
        f.bitmap_dump=lambda pid: (_ for _ in ()).throw(AdbError("denied"))
        self.assertEqual(read_bitmap_sample(f, PACKAGE, BASE)[0]["bitmap_status"], "bitmap_read_unavailable")

    def test_bitmap_cli_opt_in_is_timestamped_sidecar_not_part_of_rss_p90(self):
        class BitmapFixture(Fixture):
            def __init__(self, *_):
                super().__init__()
                self.rosters = self.rosters * 2
                self.startTimes = {pid: ticks * 2 for pid,ticks in self.startTimes.items()}
        with TemporaryDirectory() as tmp:
            output = str(Path(tmp, "fixture"))
            argv = ["--serial","fixture","--package",PACKAGE,"--category","app","--samples","1","--output",output,"--bitmap"]
            self.assertEqual(cli(argv, BitmapFixture), 0)
            data = json.loads(Path(output+".json").read_text(encoding="utf8"))
            self.assertTrue(Path(output+".bitmap.csv").exists())
            self.assertEqual(data["bitmap_diagnostics"][0]["bitmap_kib"], 3072)
            self.assertIsNone(data["sample_rows"][0]["bitmap_kib"])
            self.assertNotIn("bitmap_p90_kib", data["local_summary"][0])

    def test_cli_outputs_non_overwriting_csv_and_json(self):
        with TemporaryDirectory() as tmp:
            output = str(Path(tmp, "fixture"))
            argv = ["--serial", "fixture", "--package", PACKAGE, "--category", "app", "--samples", "1", "--output", output]
            self.assertEqual(cli(argv, Fixture), 0)
            self.assertTrue(Path(output+".csv").is_file())
            result = json.loads(Path(output+".json").read_text(encoding="utf8"))
            self.assertEqual(result["sample_rows"][0]["state"], "unknown")
            self.assertIsNone(result["local_summary"][0]["threshold_local_kib"])
            with self.assertRaises(SystemExit):
                cli(argv, Fixture)


if __name__ == "__main__":
    unittest.main()
