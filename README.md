# MPI Desktop

Desktop client and Python engine for Android/iOS performance measurement and analysis. This repository does not contain the independently managed MPI Web or Android app source.

- **Live capture** via `adb`, `xcrun`, or `pymobiledevice3` — frames, CPU,
  memory, GPU, thermal, battery where the platform exposes them — into a
  `MetricsSnapshot` time-series, viewed live in the dashboard.
- **Replay** of a previously saved capture from an MPI raw `.csv` export.

The app uses a single Python backend (`mpi/`) and an Electron + React
frontend (`frontend/`). The backend speaks newline-delimited JSON over
stdin/stdout (`mpi.api`), so the same engine can serve the desktop app,
scripts, and CI.

## Architecture

```
+------------------------+         +-----------------------------+
| Electron + React UI    |  IPC    |  mpi.api            |
| (frontend/)            | <-----> |  (newline-delimited JSON)   |
| - Live Tracing page    |  stdin  |   - start/stop_capture      |
|   (live + CSV replay)  |  stdout |   - load_file (MPI raw CSV) |
+------------------------+         |   - list_devices / packages |
                                   +--------------+--------------+
                                                  |
                          +-----------------------+-----------------------+
                          |                                               |
              +-----------v-----------+                       +-----------v-----------+
              | mpi/android   |                       | mpi/ios       |
              | (live adb capture)    |                       | (live iOS capture)    |
              | StreamingCapture-     |                       | devicectl / xctrace   |
              | Session + parsers     |                       | xcrun / pmd3 parsers  |
              +-----------------------+                       +-----------------------+

  mpi/reports contains the report helpers shared with live capture:
  adapter (summary dict -> snapshots) and excel_export (live xlsx writer).
```

## Install

### Backend (Python)

```bash
pip install -e .[dev]
```

Requires Python 3.11+. For Android live captures you need the Android
platform-tools (`adb`) on `PATH`.

For iOS live captures:

- macOS uses Xcode command line tools (`xcrun`, `devicectl`, `xctrace`) with a
  paired, developer-mode-enabled physical iOS device.
- Windows/Linux uses the external `pymobiledevice3` CLI. Install the optional
  extra with `pip install -e .[ios-windows]`, or install `pymobiledevice3`
  separately and make sure the `pymobiledevice3` command is on `PATH`. Set
  `MPI_PYMOBILEDEVICE3` if the executable lives outside `PATH`. Set
  `MPI_PYMOBILEDEVICE3_HOME` to a writable folder if the default
  `~/.pymobiledevice3` pairing cache cannot be created. MPI supports the
  `pymobiledevice3` 9.x line (`>=9.13,<10`).
- iOS wireless measurement requires the device to be USB-paired first, Wi-Fi
  connections enabled from the live tracing page, the same Wi-Fi network, and
  an unlocked device while it appears through usbmux as a wireless target.
  DVT-based CPU/memory sampling on recent iOS versions (iOS 17+, incl. iOS 26)
  also requires a `pymobiledevice3` RemoteServiceDiscovery tunnel, which on
  Windows must run elevated (it creates a WinTun virtual adapter).

  **Recommended (in-app, zero terminal):** on the Live Tracing page with iOS
  selected, the tunnel badge shows the daemon state. If no tunnel is set up,
  click **자동 설정** and approve the single UAC prompt — this registers a logon
  scheduled task that keeps the tunnel running in the background from then on.

  **No env vars are needed:** the app auto-discovers the device's tunnel from a
  running `tunneld` (queried at `http://127.0.0.1:49151`) and passes
  `--rsd HOST PORT` to the DVT commands itself, so iOS 17+/26 captures just
  work. The daemon logs to `%LOCALAPPDATA%\MPI\ios-tunnel.log`.

  **CLI alternative:** the same setup is scriptable via the bundled helper:

  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\ios-tunnel.ps1 -Install
  ```

  Check it with `scripts\ios-tunnel.ps1 -Status`; remove it with `-Uninstall`.

  **Manual alternative:** start `pymobiledevice3 remote tunneld` yourself from an
  elevated terminal — auto-discovery still applies. Override the tunneld address
  with `MPI_PYMOBILEDEVICE3_TUNNELD` (`HOST:PORT`) if you ran it on a
  non-default port. To pin a fixed RSD explicitly (e.g. from
  `remote start-tunnel --script-mode`, which prints a `HOST PORT`), set
  `MPI_PYMOBILEDEVICE3_RSD` — it takes priority over auto-discovery.

### Frontend (Electron + React)

```bash
cd frontend
npm install
```

Requires Node 18+ and npm.

## Run

```bash
# Backend-only smoke test (echo a request via stdin, get JSON on stdout)
echo '{"id":"1","command":"ping"}' | python -m mpi.api

# Original Click CLI for live capture
mpi devices
mpi packages --platform android --serial <SERIAL>
mpi check --platform android --serial <SERIAL> --package <BUNDLE_ID>
mpi packages --platform ios --serial <UDID>
mpi check --platform ios --serial <UDID> --package <BUNDLE_ID>

# Electron app (dev — Vite + Electron + Python sidecar all hot-reloading)
cd frontend
npm run electron:dev

# Electron app (prod build → dist/, dist-electron/, build_output/)
npm run electron:build
```

### Battery-temperature-gated measurement

The Live Measurement screen has an optional battery-temperature guard. It is
off by default. When enabled, **Prepare measurement** reserves the selected
physical device, waits for two consecutive valid readings at or above the
configured threshold, attaches to an already-running selected app without
changing its state, or starts it only when it is not running, and promotes the
same operation into the normal live capture. Waiting samples and waiting time
are not included in charts, exports, or measured duration.

The enabled setting and 25 °C default threshold are remembered locally, but an
in-progress wait is never resumed after MPI restarts. Android reads the battery
service with an exact battery thermal-zone fallback. The iOS guard uses
pymobiledevice3 9.x and resolves live temperature through RemoteXPC diagnostics
(`IOPMPowerSource`, then `AppleSmartBatteryPack`/`AppleSmartBattery`/`GasGauge`),
with trusted Apple Unified Log events as a last fallback. It works on every
desktop platform, including macOS; ordinary macOS manual capture keeps using
the existing xcrun path.

## Tests

```bash
pytest -v
```

Covers the live capture parsers, the report/summary adapter, and end-to-end
JSON-IPC round-trips against the `mpi.api` server (no device required).

## Layout

```
mpi/                  Python backend (single package)
  schema.py                   data classes (MetricsSnapshot, ReportSummary, ...)
  backend.py                  abstract DeviceBackend
  cli.py                      click CLI — devices / packages / check
  report.py                   3-section CSV writer for live captures
  api.py                      JSON-IPC server (stdin/stdout)
  android/                    live adb capture + parsers (gfxinfo / meminfo / top / SF / battery)
  ios/                        live xcrun/devicectl/xctrace or pymobiledevice3 capture + parsers
  reports/                    report helpers shared with live capture
    adapter.py                summary dict -> MetricsSnapshot[] + ReportSummary
    excel_export.py           styled Excel report writer (live xlsx export)

frontend/                     Electron + React UI
  electron/                   main process + preload (TypeScript)
  src/
    pages/                    LiveTracing (live capture + CSV replay)
    components/               LiveCharts, DeviceSelector, StatusBar, etc.
    lib/                      typed IPC wrapper + TS mirrors of the schema

tests/                        pytest suite (live parsers, report, adapter, api)
```

## Packaging

Use a clean Windows checkout and the pinned Python/npm toolchains in
[the Windows build guide](docs/windows-engine-build-proof.md). Install the
frozen-engine dependencies into a dedicated virtual environment and run:

```powershell
python scripts/build-windows-engine.py build --python .venv/Scripts/python.exe --out-dir dist
cd frontend
npm ci
npm run electron:build
```

The Windows packager verifies the engine, the matching source commit and
notices, and the absence of bundled APKs. Release signing and publishing
are separate operations. The Android Play app is installed via the desktop
app's Google Play QR code; its source and artifact are not included here.

## License and source

MPI Desktop and its Python engine are distributed under [GNU GPL v3](LICENSE).
Individual third-party components retain their own terms. See
[the reviewed dependency inventory](licenses/third-party.json) and the
THIRD-PARTY-NOTICES.txt and ENGINE-RUNTIME-NOTICES.txt shipped with the
installer.

For exactly corresponding source of the shipped Windows 2026.11.24-1
installer, use the [immutable source ZIP](https://dl.mpinspect.com/mpi-source-2026.11.24-1-core-1e3f384204efb519fac800d1629f27efdf07c33af357098b69c3b731484d4de4.zip).
[Release provenance](SOURCE_PROVENANCE.md) records the source and installer
hashes. This repository also includes standalone setup adjustments and
is intended for future Desktop changes.
