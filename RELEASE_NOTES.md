# Release Notes

## 2026.10.23-1 — 2026-10-23

- Cloudflare 기반 Google 로그인만 제공하며, 이전 공급자 복귀 코드와 기존 화면·내부 명칭을 제거했습니다. 자체 회원가입 및 비밀번호 로그인은 제공하지 않습니다.
- Google에 누락된 운영 콜백을 등록해 로그인 시 `redirect_uri_mismatch` 오류를 수정했습니다. 실제 웹 로그인과 PC의 Google→앱 복귀→계정·플랜 확인을 검증했습니다.
- 기존 Cloudflare 인증과 대기 업로드를 보존하며 새 저장 형식으로 이관합니다.
- Windows 설치 파일, WD 포터블, 엔진, Companion을 `2026.10.23-1`로 맞췄습니다. Companion은 code 16이며 기존 APK 서명을 유지합니다.

검증 결과와 범위는 [빌드 기록](docs/releases/2026.10.23-1-build-verification.md)을 참고하세요.

## 2026.10.21-1 — 2026-10-21

- Android 메인 CPU·GPU·배터리 온도 표시와 단말 펌웨어 빌드 정보를 개선했습니다. 지원하지 않는 원천은 미측정으로 유지합니다.
- 실시간 화면의 Parsed·Raw·Hide 및 내부 FPS 문구를 정리하고 앱 아이콘과 그래프 툴팁 동작을 수정했습니다. 모바일 시스템 메모리를 앱 메모리보다 먼저 표시합니다.
- 헤더 설정에서 Google 또는 이메일 계정 로그인을 제공합니다. 로그인된 PC에 연결된 Android Companion의 임시 로그인은 파일 업로드 설정과 독립적으로 동작하며, 로그아웃·연결 종료·lease 만료로 해제됩니다.
- 새 프로토콜이 기존 기기에 적용되도록 Companion을 2026.10.21-1(code 13)로 갱신했습니다.

기존 사용자 OAuth·설정 변경을 보존하여 이번 릴리스에 포함했습니다. 구현 검증은 [요구사항 기록](docs/verification/2026-10-21-requirements.md), 실제 인증 검증은 [후속 인증 기록](docs/verification/2026-10-21-continuation-auth.md)에 있습니다. Windows x64 설치 파일 빌드와 실제 추출 앱의 격리 실행 검증을 완료했습니다. 최신 Companion(code 13)은 10월 16일 전달 APK와 같은 서명으로 빌드했습니다. Windows 설치 파일은 코드 서명 없이 제공하며, 실제 설치/제거 등록은 실행하지 않았습니다. 구성·해시·실행 검증은 [빌드 기록](docs/releases/2026.10.21-1-build-verification.md)에 있습니다.

## 2026.10.16-1 — 2026-10-16

- Android CPU를 읽을 수 없는 구간은 `0%` 대신 미측정으로 표시합니다. 프로세스 재시작이나 카운터 초기화 이후에는 유효한 새 측정 구간부터 사용률을 계산합니다.
- Android Total CPU를 코어별 사용률의 산술평균으로 표시하고 각 코어 사용률도 제공합니다. 일부 코어만 측정되거나 aggregate 원천을 사용한 경우 상태를 함께 표시합니다.
- 기기를 연결하거나 같은 기기가 다시 연결되면 앱 이름과 아이콘을 자동으로 불러옵니다. 선택 앱을 먼저 처리하며, 이미 확보한 정보는 유지하고 일시적 조회 실패는 한 번 재시도합니다.
- iOS 수치에 최신 측정·이전 값 유지·미측정 상태와 경과 시간을 표시합니다. 원천 갱신이 멈추면 유효 시간이 지난 값을 계속 최신 수치처럼 보여 주지 않습니다.
- iOS FPS의 시스템 디스플레이 범위와 Core Animation 원천을 표시합니다. 실험적 kdebug 수치는 별도로 표시하며, 이 값을 선택 앱 자체의 FPS로 해석하지 않도록 구분합니다.
- 수집 가능한 실제 프레임 이벤트를 저장하고 세션 전체 관찰 이벤트의 P50/P90/P95/P99를 제공합니다. 최악 구간 백분위, 원천·범위, 관찰 이벤트 수와 확인된 유실 수를 구분하여 분석 화면과 Excel 측정 근거에 기록합니다.

검증: 프런트 테스트 193개(32개 파일), 프로덕션 빌드와 Electron 컴파일이 통과했습니다. Python 전체 회귀 검증은 551개 통과, Windows 심볼릭 링크 관련 2개 건너뜀입니다. 실제 Android·iOS 기기 검증은 수행하지 않았습니다. Windows 설치 파일용 버전은 `2026.10.16-1`이며 기존 Companion `2026.10.10-2`(code 12)를 포함합니다. 로컬 코드 서명 키가 없어 이번 설치 파일은 서명 없이 빌드합니다.

Windows x64 설치 파일 빌드와 추출 앱의 격리 실행 검증을 완료했습니다. Companion APK는 소스를 변경하지 않고 새 Android debug 키로 빌드했습니다. 기존 단말의 앱과 서명이 다르면 업데이트 설치가 실패할 수 있으며, 이 경우 데이터 보존 여부를 확인한 후 사용자가 재설치해야 합니다. 이번 검증에서는 단말 앱을 설치하거나 제거하지 않았습니다. 설치 파일 SHA-256과 검증 범위는 [빌드 검증 기록](docs/releases/2026.10.16-1-build-verification.md)에 있습니다.

프레임 집계만 제공하는 원천에서는 원본 이벤트를 만들지 않습니다. 전체 세션 데이터는 내보내기를 위해 메모리에 누적되므로 장시간 측정 시 메모리 사용량이 증가합니다. 지원 범위와 이벤트 보관 한계는 [측정 원천 및 데이터 유효성](docs/measurement-provenance.md)에 정리했습니다.

## 2026.7.28-1 - 2026-07-28

- Redesigns the analysis dashboard: a fixed toolbar keeps the A–D session
  legend, partial-load warning, and collapsible range editors above a scrolling
  read surface.
- Reworks the single-session view with a slim header, two-tier KPI cards with
  per-metric color ticks, and a unified diagnosis grid.
- Improves session comparison with left-aligned column headers showing
  device · platform · duration, centered values, and metric-group color ticks.
- Cleans up the session library: overlay toasts for notices, an in-flow delete
  button, and consistent A–D session colors.
- Temperature-triggered auto-start now attaches to an already-running selected
  app without changing its state, and launches it only when it is not running.
- Synchronizes Python, Electron lockfile, Android Companion, and portable
  packaging metadata to `2026.7.28-1`.

## 2026.7.27-1 - 2026-07-27

- Adds `1% Low`, `Stutter`, and `Smooth` to the live FPS card and keeps invalid
  frame windows visibly unavailable instead of turning them into zeroes.
- Shows elapsed measurement time in the Android overlay from the first actual
  sample, excluding setup and temperature waiting.
- Uses separate hour, minute, and second fields for automatic stop, defaulting
  new or damaged settings to `00:40:00`.
- Keeps current and threshold temperatures visible after a read error, marks the
  current value unavailable, and retains inline retry/error guidance.
- Uses a smaller primary capture-control row and true per-window native
  fullscreen that hides both the title bar and Windows taskbar.
- Gives each selected analysis session a stable A–D color across charts,
  toggles, and tooltips; CPU uses solid System and dashed App lines.
- Shows user-assigned Android/iOS device names in selectors and sessions while
  retaining the hardware model in details, and displays iOS market versions
  such as `iOS 26.2.1` instead of build identifiers.
- Renames current product contracts to `MPI`/`mpi`, including Python commands,
  environment variables, app IDs, cache/storage paths, sessions, Companion, and
  iOS tunnel tasks. Existing local data is migrated with verification and
  resumable collision handling.
- Uses `MPI.exe` as the primary desktop entrypoint. The `2026.7.27-1` installer
  and portable package also contain a one-release `PerfInspect.exe` compatibility
  alias, while manifests continue to launch `MPI.exe`.
- Synchronizes all release manifests to `2026.7.27-1`.
- Protects upgrades from concurrent session-file creation, interrupted nested
  moves, rollback-updated settings, overlapping tunnel setup, and failed
  Companion handoff.
- Claims legacy session files before moving them, so a capture writing during
  upgrade is retained for the next pass and source/target junction changes
  cannot copy or delete data outside the MPI folders.
- Holds Windows write-excluding handles through the final migration commit so
  a simultaneously edited MPI session and its differing legacy data both survive.
- Recovers same-value and concurrent-window local setting rollbacks, validates
  setting fields before retiring old keys, and migrates cleared endpoint values.
- Fully stops a failed iOS tunnel task before restoring and verifying the prior
  task, carries custom Python paths across UAC, and verifies that a preserved
  legacy Android Companion actually reclaimed its bridge.
- Clears orphaned legacy tunnel listeners before accepting the replacement and
  serializes Companion installation for each phone across measurement windows.
- Prevents delayed native fullscreen events from undoing the latest toggle and
  records the revision in Windows executable metadata as `2026.7.27.1`.

## 2026.7.27 - 2026-07-27

- Shows the measured metrics on the Android device's own screen while a
  measurement runs. A small floating window displays FPS, CPU, GPU, temperature,
  and network, so the device can be watched without looking at the PC.
- Lets each of the five values be shown or hidden individually, and lets the
  whole overlay be turned on or off during a measurement without interrupting it.
- Adds per-app network throughput on the device overlay, falling back to
  whole-device traffic when a per-app reading is unavailable and marking which
  one is shown.
- Adds device-wide receive/transmit byte counters and KB/s rates to Android raw
  CSV exports, CSV replay, and configured endpoint transfers.
- Lets the Android overlay and its metrics be configured before a device is
  connected, then applies the saved preference when measurement starts.
- Shows device-wide system CPU utilization in the overlay instead of app CPU.
- Removes the companion app from the device's app list and recent-apps view, and
  closes its screen automatically once the measurement bridge is running, so it
  can no longer be opened by hand or appear over the game being measured.
- Explains why the overlay cannot be shown — companion app missing, outdated, or
  unreachable, an unsupported Android version, or a missing "Display over other
  apps" permission — instead of leaving the switch with no visible effect.
- Updates the companion app on devices that already have an older version, which
  previously kept whatever build was installed first.
- The overlay is Android-only. iOS does not allow one app to draw over another,
  and the measurement path installs nothing on an iOS device, so the control is
  hidden for iOS.
- Synchronizes Python, Electron, lockfile, and portable-extension release
  metadata to version `2026.7.27`.

## 2026.7.21-3 - 2026-07-21

- Adds an optional automatic measurement stop with a saved On/Off toggle and a
  configurable `HH:MM:SS` end time from 1 second to 1 hour.
- Starts the countdown with the first actual measurement sample, so device
  preparation and temperature-trigger waiting do not consume the configured time.
- Stops the measurement and closes its running target app together on Android
  and iOS. Manual measurement stops still leave the target app open.
- Keeps automatic teardown single-shot across timer/manual-stop races and saves
  the final collected sample even when the end event follows immediately.
- Shows system CPU and app CPU on the same whole-device 0–100% basis across
  live, replay, range-summary, and analysis views. The raw core-summed app value
  remains available only as a clearly labeled diagnostic, while in-product help
  explains Android frequency adjustment and the iOS core-only fallback.

- Improves live and saved-session metric cards, hover details, chart series,
  comparison colors, and dashboard rendering while preserving unavailable
  values instead of presenting misleading fallbacks.
- Synchronizes Python, Electron, lockfile, and portable-extension release
  metadata to version `2026.7.21-3`.

## 2026.7.21-2 - 2026-07-21

- Shows the live battery temperature before measurement waiting begins when
  temperature-triggered auto-start is enabled.
- Uses read-only Android and iOS preview streams that cannot launch the selected
  app, and waits for preview release before starting the existing guard.
- Adds inline retry, stale-value removal, deterministic cleanup, and per-window
  routing across toggle, target, error, and window transitions.
- Embeds Python dependency license texts from the same project virtual
  environment used to build the packaged data engine.
## 2026.7.21-1 - 2026-07-21

- Fixed an iOS auto-start error at the configured battery-temperature threshold
  by connecting the prepared process-control channel before launching the app.
- Removed dependency source maps and package-owned tests from release ASARs.
- Corrected Python dependency notices to use the exact virtual environment from
  which the standalone data engine is built.

## 2026.7.21 - 2026-07-21

- Refreshed the Windows desktop release artifacts from the current `main`
  source, including a rebuilt standalone data engine and NSIS installer.
- Synchronized Python, Electron, lockfile, and portable-extension release
  metadata to version `2026.7.21`.

## 2026.7.20 - 2026-07-20

- Added an opt-in battery-temperature guard for live measurement. The toggle
  remains off by default and the temperature threshold now defaults to 25 °C;
  when enabled, MPI waits for two consecutive valid readings at or above the
  configured threshold before starting a capture.
- Added app-independent 1 Hz Android battery monitoring with guarded
  `dumpsys battery`/battery thermal-zone fallback, followed by a verified cold
  restart of the selected app and promotion into the existing live stream.
- Added an iOS pymobiledevice3 guard path that prepares diagnostics and DVT
  connections before launch, reads newer-device temperature from
  `AppleSmartBatteryPack.BatteryData` with compatible diagnostics and trusted
  Unified Log fallbacks, and cold-launches on the prepared connection to avoid
  losing the first post-launch samples.
- Added physical-device reservation and deterministic cleanup across waiting,
  trigger, cancellation, disconnect, launch failure, capture failure, and
  window-close races.
- Added per-window Electron routing for guard ownership and atomic guard-to-
  capture ownership transfer, with stale guard/capture events rejected by the
  renderer.
- Added guard state, threshold, and live reading controls while preserving the
  existing manual **Start measurement** behavior when the feature is disabled.
- Updated Python, Electron, lockfile, and portable-extension defaults to
  version `2026.7.20`.

## 2026.7.14-4 - 2026-07-14

- Added guarded session deletion to the analysis library, including explicit
  confirmation, per-file failure reporting, stale selection/range cleanup, and
  refresh-race protection.
- Restored saved app names and launcher icons from platform-specific caches,
  preserved PNG/WebP metadata correctly, and show a clearly marked package ID
  instead of “Unknown app” when no friendly title is recoverable.
- Normalized dashboard app CPU usage against logical CPU capacity and bounded
  all CPU analysis series to 0–100%. Captures without a trustworthy core count
  or normalized metric now remain unavailable instead of showing a misleading
  saturated value.
- Replaced raw-second range inputs with compact minute:second controls, removed
  the “Edit session range” copy, and kept reset controls available after empty
  or failed range analysis.

## 2026.7.14-3 - 2026-07-14

- Corrected Android SurfaceFlinger frame sampling for irregular collection
  cadence so FPS and frame timing remain aligned with the actual sample span.
- Corrected iOS Core Animation aggregation when instruments report coarse
  duration buckets, preventing short captures from overstating frame rates.
- Made the Electron window explicitly visible at creation and restored a
  hidden or minimized existing window before focusing it on a second launch.
- Prepared the `2026.7.14-3` portable external-extension package and isolated
  its artifacts under a version-specific release directory.

## 2026.7.14-1 - 2026-07-14

- Added a portable external-extension packaging workflow for the desktop host,
  producing a versioned ZIP, runtime metadata, release manifest, checksums, and
  release notes from a version-specific Electron `win-unpacked` directory.
- Hardened extension packaging with an explicit runtime-file allowlist, source
  and archive path validation, reparse-point rejection, and refusal to replace
  an already published artifact for the same version.
- Changed the packaged Windows executable name from `MPI.exe` to
  `PerfInspect.exe` so the external-extension entrypoint is stable and matches
  the host manifest contract.

## 2026.7.13-2 - 2026-07-13

- Moved session range editing out of the session-library sidebar and into the
  main analysis flow directly below the KPI/comparison metrics.
- Multi-session analysis now shows compact A-D range editors for every loaded
  session, with independent validation, apply, and reset actions. Single-session
  analysis uses the same main-panel placement for consistency.
- Preserved stable file-path mapping when a selected session fails to load, so
  editing one visible session cannot apply its range to another CSV.

## 2026.7.13-1 - 2026-07-13

- Changed app identity throughout live measurement and analysis to use the
  resolved app title as the primary label rather than the package identifier.
- Replaced sample-count copy in the session library with filtered/total session
  counts, and added cached launcher icons to newly saved session metadata.
- Added per-session start/end range editing. Applying a range re-runs the full
  analysis so KPI cards, comparison deltas, coverage, duration, sample count,
  and charts all use the same cropped interval, rebased from zero seconds.
- Assigned distinct colors to FPS, temperature, CPU, normalized CPU, GPU,
  memory, power, and battery charts while retaining line patterns for A-D
  session identification.

## 2026.7.13 - 2026-07-13

- Added live-to-static CSV comparison on the Live Measurement screen. An
  existing raw CSV can stay loaded while capture continues, with a dedicated
  Compare On/Off control that does not alter captured or exported live data.
- Overlaid each saved CSV headline metric as a labeled dashed line aligned by
  elapsed session time. Comparison lines stop at the saved session boundary
  instead of extending the final value past the source data.
- Added validation for empty comparison files and focused regression coverage
  for exact, nearest-sample, disabled/empty, and unequal-duration alignment.
- Improved the comparison CSV control with hover/focus highlighting, full-name
  tooltips for truncated filenames, and an accessible remove action that clears
  only the loaded comparison and its overlay state.

## 2026.7.11 - 2026-07-11

- Added a top-level **Live Measurement / Analysis Dashboard** navigation. The
  default screen stays on live measurement, and switching screens preserves an
  in-progress capture (the live view is hidden, never unmounted).
- Removed the live screen's single-CSV load and drop-to-replay features,
  replacing them with the dashboard's **persistent session library**:
  auto-saved sessions and imported external CSVs, newest first, searchable by
  app / package / device / date / platform.
- External CSVs are validated, de-duplicated, and copied into
  `Documents/PerfInspect/Raw`. New files de-dupe by a stable `session_id`;
  legacy files by a header-excluded content fingerprint. Imports report
  per-file imported / duplicate / failed so a bad file degrades to partial
  success.
- **Single-session analysis** (metadata, KPI cards, CPU/GPU/memory/power/
  battery/temperature detail, and 8 time-series charts) and **session
  comparison** (one baseline + up to 3, absolute and percent deltas, neutral
  sign-only, with a condition-mismatch warning). Up to 4 sessions use fixed
  colors plus solid/dashed line styles so they never rely on color alone.
- All aggregates come from a single Python `SessionAnalysis` path (finite
  values only; empty cells preserved as null; only a measured `0` counts).
  Coverage below 80% shows a partial-data warning. No target refresh rate is
  assumed, so there is no automatic pass/fail score.
- Extended the CSV to v2 with `schema_version`, a stable `session_id`, session
  name, app label, platform, de-identified device metadata, and
  `missed_vsync` / `slow_ui_thread` / `frame_deadline_missed` / memory
  code+stack fields. The raw device serial is still never stored.
- Fixed loading forcing iOS sessions to Android, and fixed app-label /
  platform / serial-hash restoration on load.
- Added a frontend test suite (Vitest + React Testing Library) for the
  dashboard alongside the existing Python analysis regression tests.

## 2026.7.10 - 2026-07-10

- Improved connected-device recognition with automatic non-overlapping polling,
  parallel Android/iOS discovery, and cached static device metadata.
- Made installed-app lists appear sooner by showing the fast platform inventory
  first, enriching it from the Android companion when available, and loading
  labels/icons only for visible rows instead of sweeping every app up front.
- Reduced measurement startup work by reusing device/package metadata and
  parallelizing independent Android initialization probes.
- Removed the hosted business stack and desktop account/plan/upgrade flows,
  including the Cloudflare website, PAT, entitlement, and quota paths.
- Kept save-time transfer as a local setting: when enabled, saved measurement
  data is POSTed once to the configured HTTP(S) data-transfer endpoint.

## 0.3.7 - 2026-06-23

- iOS device selection now automatically requests the Windows tunnel scheduled
  task registration/start when the tunnel is missing, with a per-device guard
  to avoid repeated UAC prompts.
- Fixed iOS USB live measurement readiness on Windows: USB captures now require
  the same RemoteXPC tunnel as wireless captures, matching the actual
  `pymobiledevice3 developer dvt` behavior on recent iOS versions.
- Reworked the Windows iOS tunnel helper to run `tunneld` as a managed hidden
  child process instead of via pymobiledevice3 `--daemonize`. `--daemonize`
  relies on the unix-only `daemonize`/`fcntl` modules and aborts immediately on
  Windows (the child exits with `0xC0000142` before binding the port), so the
  tunnel never came up. The helper now owns the `tunneld` child directly,
  captures its output to the log, monitors the local tunneld API, and restarts
  the child only when it exits or the API is unavailable.
- Fixed the iOS tunnel readiness badge so a registered-but-not-running tunnel
  task no longer stays in "start waiting" forever; it now times out to a
  retryable setup state and clears that state once tunneld responds.
- Hardened Windows iOS wireless capture by preserving tunnel-specific
  pymobiledevice3 failures, counting active tunneld entries accurately, and
  failing clearly when a DVT sysmon sample cannot find the selected app process.

## 0.3.6 - 2026-06-23

- Fixed intermittent impossible FPS spikes in live capture by rejecting
  queue-submission/burst timestamp readings above the device display ceiling.
- Android live capture now prefers SurfaceFlinger present-to-present timing and
  caps `gfxinfo`/layer FPS outliers to the detected refresh rate.
- iOS live capture now falls back from burst Core Animation timestamps to frame
  duration timing and caps extreme `pymobiledevice3` sysmon FPS samples.

## 0.3.5 - 2026-06-18

- Fixed log archiving so each ZIP is named from the full folder path *relative to
  the chosen folder* instead of just the deepest folder. Logs found under
  `A\B\C\D\E` now produce `B_C_D_E_Log.zip` (components joined by `_`, with a
  `_Log` suffix); logs sitting directly in the chosen folder fall back to that
  folder's own name.

## 0.3.4 - 2026-06-15

- Added a Claude-brand Windows app icon (ink tile with a clay performance-trend
  glyph), generated reproducibly by `scripts/make_icon.py` and embedded into the
  installer and executable via electron-builder `win.icon`.
- Added a self-signed Authenticode code-signing pipeline:
  `scripts/make-signing-cert.ps1` creates the certificate and
  `scripts/build-signed.ps1` produces a signed installer. The installer, app,
  uninstaller, and elevate helper are now signed with SHA-256 and an RFC 3161
  timestamp. (A self-signed certificate does not remove the SmartScreen "unknown
  publisher" warning — a CA-issued cert is needed for that.)
- Signing is now SHA-256 only (dropped the deprecated SHA-1 `/t` timestamp pass
  that public timestamp authorities no longer serve) and the package `author` is
  set for the installer publisher name.

## 0.3.3 - 2026-06-12

- iOS 17+/26 captures on Windows now work without any manual terminal steps:
  the app auto-discovers the device's RemoteXPC tunnel from a running
  `tunneld` (queried at `http://127.0.0.1:49151`) and injects `--rsd HOST PORT`
  into DVT commands itself. Works for both USB and wireless (Wi-Fi) devices.
- New iOS readiness badge on the Live Tracing page: checks both the
  pymobiledevice3 CLI and the tunnel daemon. A missing CLI gets a one-click
  in-app "자동 설치" (pip install via the backend); a missing tunnel gets a
  one-click "자동 설정" that registers a logon scheduled task with a single
  UAC approval — no PowerShell/bash required from the user.
- New `ios_preflight` / `ios_install_tool` backend commands, and frozen
  (packaged) builds now fall back to `python -m pymobiledevice3` when a system
  Python has the module but the standalone exe is not on PATH.
- Added `scripts/ios-tunnel.ps1` (bundled into the packaged app) that runs
  `pymobiledevice3 remote tunneld` as an auto-restarting elevated daemon with
  `-Install`/`-Uninstall`/`-Status` modes; logs to
  `%LOCALAPPDATA%\perfinspect\ios-tunnel.log`.
- `PERFINSPECT_PYMOBILEDEVICE3_RSD` still pins a fixed RSD address and takes
  priority over auto-discovery; `PERFINSPECT_PYMOBILEDEVICE3_TUNNELD`
  overrides the tunneld probe address.
- Reworked the wireless connection flow into a guided single path: pick the
  USB device, press "무선 전환", and end in a compact "무선 연결됨" chip (with a
  "변경" escape hatch). A "무선 기기 직접 선택" mode reconnects to already-wireless
  devices without re-pairing. Replaces the old double-dropdown layout.
- Common capture/wireless errors (missing tunnel, developer mode off, locked
  device, unauthorized USB debugging, stale pairing, missing CLI, timeouts)
  now surface as actionable Korean guidance in the status bar and wireless
  flow; the raw error stays available via tooltip.

## 0.3.2 - 2026-05-29

- Reskinned the desktop app in the Anthropic/Claude brand language: warm cream→ink neutral palette, clay accent, and a Poppins display font.
- Applied clay accents to active navigation, primary buttons, and selection/filter/drag states; warmed the live chart grid and FPS/pinned crosshair colors. Start/stop buttons keep their green/red semantics.
- Tightened spacing across the UI (toolbars, buttons, table cells, side panels, chart gaps) for a more compact, dense layout.
- Hardened the IPC layer to degrade gracefully when the Electron preload bridge is absent.

## 0.3.1 - 2026-05-27

- Added iOS live capture support alongside Android for connected device discovery, app listing, and capture startup.
- Added a Windows-compatible iOS capture path through the external `pymobiledevice3` CLI, including USB and wireless usbmux device discovery.
- Routed iOS wireless preparation through the live tracing UI/API so paired devices can enable Wi-Fi connections before selecting a wireless target.
- Added iOS parser support for devicectl app/device data, Activity Monitor CPU and memory samples, and Core Animation frame metrics.
- Updated the desktop live tracing flow so platform selection, device selection, package loading, and capture state handle Android and iOS targets.
- Documented the iOS setup requirements and CLI examples for macOS/Xcode and Windows/pymobiledevice3 capture workflows.
