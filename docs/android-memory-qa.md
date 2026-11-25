# Android memory QA (desktop CLI; local results only)

This tool compares **one authorized attached Android device, one exact package, and one workload** to the reference boundaries. It never launches or stops the app, changes device settings, uses root, or uploads data. The device must already be connected and authorized in ADB. The app must already be running.

From this repository, with Python 3 and Android Platform Tools on PATH:

```powershell
adb devices
python -m mpi.memory_qa --serial SERIAL --package com.mpi.companion --category app --form-factor phone_tablet --state auto --samples 30 --interval 3 --output mpi-home-memory
```

Use `--category game` only for the tested package's genuine Play Store game classification. Repeat separate runs for fixed workloads and states (e.g. foreground landing/gameplay, user-perceived service, background, and cached), across representative phone/tablet RAM tiers. `--state auto` records a per-process state only when two `oom_score_adj` reads agree around the memory read. Unsupported or unstable scores remain unknown. If giving a state manually, label the scenario independent of this tool: a child process can be in a different state from the on-screen app, so prefer auto for multiprocess comparisons. Default `unknown` does not compare thresholds.

- `mpi-home-memory.csv` stores timestamped process rows; JSON also stores separate per-process-instance nearest-rank P90 and the exact versioned rule table. Zero is an actual value; blank fields are not zero. The sampler inspects up to four processes per iteration (exact base name first) and flags how many were omitted. Process identifiers and start ticks are rechecked; failed reads and field omissions are shown, never substituted with total RSS/PSS.
- Measured memory is Linux `RssAnon` + uncompressed `VmSwap`, displayed separately in KiB; `MemTotal` approximates usable RAM, not marketed physical RAM. A DB or local reference framework may use canonical total-memory convention; the local threshold table encodes Google's displayed MB/GB using MiB/GiB. Exactly 18432 MiB is included in the 16 GB tier. Reference comparison requires explicit `--form-factor phone_tablet`, API 33+, a known state, and a threshold for the measured RAM tier. For other or unknown hardware the tool still outputs raw measurements and local P90 without a policy comparison.
- Bitmap is off by default. To obtain a separate, slower local diagnostic, run with `--bitmap`; it writes an additional `.bitmap.csv` and a `bitmap_diagnostics` JSON section. The parser requires the same PID/process before and after and both exact Native Allocations bitmap rows from `dumpsys meminfo -s PID`, with the declared `Total(kB)` column. If only one row appears, show that observed component, but leave the unreported component and combined total null (no inferred zero). Unsupported or denied evidence remains null. It never substitutes RSS, PSS, graphics, DMA-BUF or heap stats, changes system properties, or captures heap dumps. Bitmap diagnostics carry separate timestamps and do not inherit RSS process state or contribute to RSS or Play P90. See [Android bitmap guidance](https://developer.android.com/topic/performance/memory/guide/bitmaps#1-measuring-with-dumpsys-meminfo).
- A local process-instance P90 or an at-or-below reference does **not** reproduce Play's aggregation or 28-day policy evaluation. Check Android vitals in Play Console for the release, device RAM tier and process state. Outputs include local device and process identifiers; review before sharing.

Tests: `python -m unittest discover -s tests -p test_memory_qa.py`.

Sources: [Play technical quality requirements](https://support.google.com/googleplay/android-developer/answer/17492799), [Android memory monitoring](https://developer.android.com/games/optimize/memory-monitoring), and [Manage your app memory](https://developer.android.com/topic/performance/memory/manage-app-memory).

### Existing desktop chart and saved captures

Windows desktop Android measurements now use **Anonymous RSS + Swap** for the
existing Memory chart. This is the selected process's exact `RssAnon` + `VmSwap`
when both are readable and its identity is confirmed around the read. Inaccessible
or late reads remain gaps and never reuse old values. The background ADB read
does not block or queue behind live capture. Legacy PSS/RSS and iOS data remain
separate; Android raw exports retain the legacy columns and add explicit
measurement provenance and nullable new columns.

The chart can show a local, latest-state reference for Android 13+ phones/tablets,
using usable RAM and a stable OOM score. Android package metadata is only a
category hint, not a Play Console classification. Pick App or Game in the chart
when the hint is missing. If the device scope or state is unknown, no line is
shown. The units use 1024 KiB per MiB. This reference is not Play Vitals or a
pass/fail for the historical capture; the 28-day Play decision is independent.
