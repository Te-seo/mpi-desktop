# 변경 이력 (Changelog)

버전 표기: 데스크톱 앱·엔진·컴패니언은 CalVer(YYYY.M.D-revision)를 사용합니다.

## 2026.10.23-1 — 2026-10-23

- 웹·데스크톱·Android에서 이전 인증 공급자의 복귀 분기, SDK, 키·URL, 클래스·IPC·화면 명칭 및 일회성 이전 도구를 제거하고 Cloudflare와 Google 로그인만 유지했습니다.
- 기존 Cloudflare 로컬 인증과 업로드 대기열은 검증 후 새 저장명으로 한 번 복사하며, 로그아웃 이후 이전 인증이 다시 복원되지 않도록 했습니다.
- Google OAuth 클라이언트에 누락된 운영 콜백 `https://mpinspect.com/cf-auth/google/callback`을 등록하여 `redirect_uri_mismatch`를 해결했습니다. 실제 웹 및 PC PKCE 로그인, 계정·플랜 확인, Companion 위임과 로그아웃 후 토큰 회수를 검증했습니다.
- 데스크톱·엔진·Companion 버전을 맞추고, WD 패키징 버전을 현재 앱 메타데이터에서 읽도록 수정했습니다. 과거 이전·검증 문서는 이력으로 보존합니다.

## 2026.10.21-1 — 2026-10-21

- Android CPU·RAM 부분 읽기를 보존하고, `/proc/stat`이 없는 경우 검증된 `dumpsys cpuinfo` TOTAL 값을 사용합니다. 지원하는 KGSL/Mali/SGPU GPU 원천을 보완하고 SGPU kHz 주파수를 구분하되 미지원·잘못된 값은 미측정으로 유지합니다.
- 배터리 온도의 중복 `thermal:` 접두사를 정리하고 기존 CSV·차트 키를 호환합니다. 다른 센서 온도를 배터리 값으로 대신 표시하지 않습니다.
- 실시간 Parsed·Raw·Hide 표시를 제거하고 좌상단 아이콘을 앱 아이콘과 맞췄습니다. 실시간·분석 화면의 내부 FPS source/scope 문자열을 정리했습니다.
- 단말 펌웨어/OS 빌드 버전과 이름을 기기 정보·분석 화면에 표시하고 CSV·재생·분석·전송 메타데이터에 보존합니다. 모바일에서는 시스템 메모리를 앱 메모리 앞에 표시합니다.
- 차트 바깥 이동, 스크롤, 포커스·창 상태 변경 시 툴팁과 예약된 hover 업데이트를 정리합니다.
- PC 로그인 상태를 연결된 Android Companion에 한시적으로 전달하는 내부 브리지를 추가했습니다. refresh token을 전달하지 않고, 로그아웃·연결 종료·만료 시 해제합니다. 데스크톱 파일 업로드 설정은 자동으로 변경하지 않습니다.

프런트 검증은 37개 파일·243개 테스트, 앱·Vite 설정 타입 검사, 변경 구현 파일 린트, Electron 컴파일, Vite production build가 통과했습니다. Python 선별 회귀 418개가 통과했고, 추가 SGPU 원천 수정 후 관련 81개 회귀를 재검증했습니다. 최초 Android 의존성 다운로드 실패 뒤 허가된 실행에서 6개 클래스·39개 단위 테스트와 debug APK 빌드가 통과했습니다. Android 두 대의 원천 probe에 이어 SM-S938N 실제 API 캡처에서 CPU 7.5%·GPU idle 0%·클록 443 MHz·배터리 31.4°C의 전달을 확인했습니다. 전체 화면·인증 E2E와 구체적 한계는 [요구사항 반영·검증 기록](docs/verification/2026-10-21-requirements.md) 및 [추가 계측 검증](docs/verification/2026-10-21-continuation-metrics.md)에 기록합니다.

기존 사용자 OAuth·설정 미커밋 변경을 보존해 원본 소스에 반영하고 바이트 일치를 확인했습니다. 파일별 결과는 위 검증 기록의 원본 동기화 증거에서 확인합니다. 실제 PC→Android 자동 로그인·lease 만료·복구·로그아웃에 이어, 사용자의 정상 잠금 해제 후 모바일 CPU 4.0%·GPU 0.0% 표시까지 확인해 남은 요구사항의 검증을 완료했습니다. 증거 보존 후 별도 검증 앱을 정리하고 기존 앱·계정·Raw CSV는 유지했습니다. 신규 프로토콜 APK의 자동 업데이트 생략을 막기 위해 Companion만 `2026.10.21-1`(code 13)과 Python bundled 상수를 함께 갱신했고 이전 실제 버전 업데이트를 포함한 31개 검사 및 최종 APK 빌드가 통과했습니다. 데스크톱·엔진도 `2026.10.21-1`(Windows 파일 버전 `2026.10.21.1`)로 맞추어 NSIS 설치 파일을 빌드했습니다. 추출한 82개 파일의 해시 일치, frozen 엔진 및 패키지 앱·설정 UI 실행을 확인했습니다. Windows 파일은 서명 없이 제공하고 Companion은 이전 전달 APK 서명을 유지합니다. 구현 단계와 릴리스 단계의 검증 결과는 [빌드 기록](docs/releases/2026.10.21-1-build-verification.md)에서 구분합니다.

## 2026.10.16-1 — 2026-10-16

- Android CPU 원천을 읽지 못하거나 기준 표본만 있는 경우 사용률을 `0%`로 만들지 않고 미측정으로 유지합니다. PID 재사용, 카운터 역행, 코어 재연결 시 기준점을 다시 설정하고, 확인하지 못한 CLK_TCK 값도 추정하지 않습니다.
- Android Total CPU를 측정 가능한 코어별 사용률의 산술평균으로 계산하고 코어별 사용률을 표시합니다. guest 카운터 중복 합산을 제거했으며, 부분 측정과 aggregate-only 대체 원천을 구분합니다.
- 기기 연결·동일 serial 재연결 시 앱 이름과 아이콘을 자동으로 갱신합니다. 선택 앱을 우선하여 8개씩 처리하고 성공 캐시는 보존하며, 실패한 메타데이터는 한 번 자동 재시도합니다. Companion 이름이 없으면 APK 이름 조회로 보완합니다.
- iOS 지표별 원천 갱신 시각과 `fresh`·`held`·`unavailable` 상태를 보존합니다. 유효 시간이 지난 CPU·메모리·FPS·GPU·배터리 값은 미측정으로 전환하며, 온도만 갱신되어도 다른 배터리 값의 유효 시간이 연장되지 않습니다.
- iOS Graphics/Core Animation FPS를 시스템 디스플레이 범위로 표시하고 선택 앱 FPS와 구분합니다. 선택적 kdebug 원천은 실험 기능임을 표시하며, 원천이 바뀔 때 서로 다른 FPS 값을 섞어 평균내지 않습니다.
- 지원 원천에서 실제 프레임 타임스탬프와 duration을 수집하여 CSV·재생·세션 분석에 보존합니다. 중복 이벤트와 확인된 유실 수를 처리하고, 원본 이벤트의 세션 P50/P90/P95/P99를 기존 최악 구간 백분위와 구분합니다. Excel에는 측정 원천·범위·관찰 및 유실 수를 기록합니다.

검증: 프런트 테스트 193개(32개 파일), 프로덕션 빌드와 Electron 컴파일이 통과했습니다. Python 전체 회귀 검증은 551개 통과, Windows 심볼릭 링크 관련 2개 건너뜀입니다. 실제 Android·iOS 기기 검증은 수행하지 않았습니다. 이번 설치 배포 버전은 `2026.10.16-1`이며 Windows 파일 버전은 `2026.10.16.1`입니다. Companion은 변경 없는 `2026.10.10-2`(code 12)를 포함합니다.

Windows x64 NSIS 설치 파일 빌드를 완료했습니다. 설치 파일에서 추출한 82개 파일의 해시, ASAR 구성, 별도 사용자 프로필에서 앱 화면과 Python 엔진 통신을 검증했습니다. Windows 설치 파일은 서명되지 않았으며, Companion은 이번 빌드용 Android debug 키로 서명했습니다. 기존 단말의 Companion과 서명이 다르면 수동 재설치가 필요할 수 있습니다. 자세한 결과는 [설치 파일 검증 기록](docs/releases/2026.10.16-1-build-verification.md)을 참고하세요.

원본 이벤트가 없는 집계값에서 프레임을 합성하지 않습니다. 표본·큐의 이벤트 수는 제한하지만 전체 세션의 내보내기 데이터는 메모리에 누적되므로 장시간 측정의 메모리 사용량에 고정 상한은 없습니다. 원천별 의미와 보관 한계는 [측정 원천 및 데이터 유효성](docs/measurement-provenance.md)을 참고하세요.

## 2026.7.28-1

- Redesigns the analysis dashboard around a fixed toolbar — A–D session legend,
  partial-load warning, and a collapsible per-session range strip — above a
  scrolling read surface, so selection and range controls stay put while
  results reload.
- Compresses the single-session header into a slim identity strip with a
  coverage/partial-data chip, ranks KPIs into two tiers with per-metric color
  ticks, and unifies the detail cards (aligned GPU columns, merged
  Power · Battery card, temperature zones as a grid card).
- Rebuilds comparison columns with left-aligned session headers that show
  device · platform · duration, centered values, metric-group color ticks, and
  a carded table with the CPU normalization note as its footer.
- Moves session-library notices into overlay toasts, places the delete button
  in-flow, and carries each selected session's A–D color from the sidebar
  through the range strip, comparison table, and charts.
- Adds theme warn tokens (light/dark) in place of inline amber styles and fixes
  the missing red-soft token that silently dropped delete-error styling.
- Temperature-triggered auto-start now attaches to an already-running selected
  app without changing its state, and launches it only when it is not running.
- Synchronizes Python, Electron lockfile, Android Companion, and portable
  packaging metadata to `2026.7.28-1`.

## 2026.7.27-1

- Added live FPS quality series for `1% Low`, `Stutter`, and `Smooth`, with
  unavailable values preserved when the frame window or p99 data is invalid.
- Added elapsed measurement time to the Android overlay and made it enabled by
  default; the clock begins at the first real sample and excludes preparation.
- Reworked the automatic-stop setting into separate hour, minute, and second
  inputs with a safe `00:40:00` default and `00:00:01–01:00:00` validation.
- Kept current/threshold temperature fields visible after a measurement error,
  explicitly showing the current value as unavailable without reusing stale data.
- Compacted the primary live controls and replaced renderer-only fullscreen with
  independent native Electron fullscreen for each measurement window.
- Changed multi-session charts to stable A–D session colors, including matching
  toggles/tooltips and solid System versus dashed App CPU lines.
- Added user-assigned device display names while preserving hardware models, and
  fixed iOS market-version parsing to prefer `ProductVersion`/`OSVersion`.
- Migrated the product, Python, Electron, Android, environment-variable, cache,
  storage, session, and iOS-tunnel namespaces to `MPI`/`mpi`, with restart-safe
  legacy data migration and guarded Companion/task replacement.
- Changed the primary desktop executable to `MPI.exe`; this release alone keeps
  a `PerfInspect.exe` compatibility alias while all manifests target `MPI.exe`.
- Synchronized Python, Electron, lockfile, Android Companion, and portable
  packaging metadata to `2026.7.27-1`.
- Hardened namespace migrations against concurrent file creation, nested
  interrupted temporaries, rollback-written local settings, overlapping iOS
  tunnel elevation, and failed Companion protocol handoff.
- Made session migration claim each legacy file before copying so concurrent
  capture writes, source/target junction swaps, and interrupted runs cannot
  delete newer or out-of-root data; escaped hard links are removed on failure.
- Locks the verified target and legacy claim against Windows writes through the
  source-removal commit, preserving both current and legacy bytes across races.
- Added field-level validation and cross-window recovery to localStorage
  migration, including same-byte rollback recreation and cleared endpoint keys.
- Strengthened iOS tunnel rollback to terminate failed task processes, restore
  and verify prior task state, preserve a custom Python path through UAC, and
  keep Electron single-flight active for the full elevated transaction.
- Clears legacy tunnel listeners before probing the replacement task, verifies
  its running state, and serializes Companion installation per Android serial.
- Verifies that the preserved legacy Android Companion process is running and
  responsive after a failed new-package handoff.
- Correlated native fullscreen requests so delayed enter/leave events cannot
  restore an obsolete state, and mapped the release revision to Windows PE
  version `2026.7.27.1`.

## 2026.7.27

- Added an on-device metrics overlay: while an Android capture runs, the measured
  device shows a small floating HUD with FPS, CPU, GPU, temperature and network,
  toggleable per metric from the live measurement sidebar and switchable on or off
  without interrupting the running capture.
- Added on-device per-app network throughput to the companion HUD
  (NetworkStatsManager over the existing usage-access grant), falling back to
  device-wide totals and marking which scope is shown.
- Added device-wide non-loopback RX/TX byte counters and KB/s rates to Android
  capture snapshots, raw CSV exports, CSV replay, and endpoint payloads.
- The Android overlay preference can now be enabled, disabled, and configured
  before a device is connected; the saved setting is applied when capture starts.
- The overlay CPU value now uses device-wide system CPU utilization rather than
  app/process CPU.
- The companion app no longer appears in the device app drawer or recents: its
  launcher entry was removed and its activity now closes itself once the bridge is
  running, so it cannot be launched or foregrounded by hand during a measurement.
- The desktop now reports why the overlay is unavailable — companion missing,
  outdated, unreachable, unsupported OS, or the "Display over other apps"
  permission denied — instead of leaving the switch silently doing nothing.
- Companion installs now upgrade a device that already has an older build, rather
  than skipping the reinstall whenever the package is merely present.
- Added `scripts/build-companion.ps1` to build and stage the companion APK in one
  step, with the JDK and build-variant pitfalls handled.
- The overlay is Android-only: iOS has no on-device agent and no cross-app
  draw-over API, so the control is hidden for iOS devices.
- Synchronized Python, Electron, lockfile, and portable-extension release
  metadata to `2026.7.27`.

## 2026.7.21-3

- Added a persisted On/Off control and `HH:MM:SS` end-time setting for live
  measurements (1 second to 1 hour).
- Added backend-owned monotonic auto-stop timing that begins with the first real
  sample, excluding temperature-guard wait and iOS connection preparation.
- Automatic stops now terminate the exact captured Android/iOS target app,
  while manual stops continue to leave it running.
- Added race-safe one-shot teardown, last-snapshot preservation, and explicit
  reporting when measurement ends but target-app termination fails.
- Unified visible system/app CPU usage on a whole-device 0–100% scale, kept the
  raw per-core app sum diagnostic-only, and documented platform-specific app
  CPU normalization.

- Improved live and saved-session metric cards, hover details, chart series,
  comparison colors, and dashboard rendering while preserving unavailable
  values instead of presenting misleading fallbacks.
- Synchronized Python, Electron, lockfile, and portable-extension release
  metadata to `2026.7.21-3`.

## 2026.7.21-2

- Added a read-only live battery-temperature preview while temperature-gated
  measurement is enabled and idle.
- Added isolated preview reservations, inline retry, and deterministic handoff
  to the guard only after preview cleanup completes.
- Ensured iOS preview mode cannot accumulate trigger hits or launch the selected
  app, with stale-response and per-window cleanup protections.
- Bound generated Python license text notices to the packaged engine's virtual
  environment.
## 2026.7.21-1

- Fixed the iOS temperature-guard handoff so the prepared DVT process-control
  channel is connected before the threshold-triggered cold launch.
- Restored release-wide ASAR exclusions for dependency source maps and tests.
- Bound Python license generation to the same project virtual environment used
  by the packaged data engine.

## 2026.7.21

- Rebuilt the Windows data engine and desktop installer from the current
  `main` release source.
- Synchronized Python, Electron, lockfile, and portable-extension release
  metadata to `2026.7.21`.

## 2026.7.20

- Added opt-in battery-temperature-gated measurement with a persisted 25 °C
  default threshold, two-hit trigger rule, locked preparation snapshot, and
  unchanged manual flow when disabled.
- Added Android 1 Hz battery monitoring, exact battery-zone fallback, verified
  cold app restart, and live-capture promotion.
- Added iOS prepared-DVT/diagnostics monitoring, live
  `AppleSmartBatteryPack.BatteryData` temperature extraction with Unified Log
  fallback, and same-connection cold launch through pymobiledevice3 9.x.
- Added physical-device reservations, cancellation/failure cleanup, per-window
  guard routing, atomic guard-to-capture ownership transfer, and stale-event
  rejection.
- Synchronized release and portable-extension defaults to `2026.7.20`.

## 2026.7.14-4

- Added safe analysis-session deletion with confirmation, path-scoped state
  cleanup, partial-failure feedback, and refresh-race handling.
- Recovered cached app labels/icons for saved sessions, preserved PNG/WebP icon
  metadata, and added an explicit package-ID fallback.
- Normalized dashboard CPU metrics to a 0–100% whole-device scale and suppress
  unnormalizable legacy raw CPU values instead of saturating them.
- Switched range editing to compact minute:second inputs and kept reset/recovery
  available when a selected interval contains no samples.

## 2026.7.14-3

- Corrected Android SurfaceFlinger sampling for irregular capture cadence and
  iOS Core Animation FPS aggregation for coarse duration buckets.
- Explicitly show the Electron window on creation and restore hidden or
  minimized windows before focusing them after a second launch.
- Prepared portable extension packaging for `2026.7.14-3`, with default
  outputs isolated in a version-specific release directory.

## 2026.7.14-1

- Added versioned portable extension packaging with runtime/release manifests,
  SHA-256 checksums, and release notes.
- Added strict packaging allowlists, path and reparse-point validation, and
  same-version artifact overwrite protection.
- Standardized the packaged Windows executable name as `PerfInspect.exe`.

## 2026.7.13-2

- Moved single and multi-session range editors below the main analysis metrics.
- Added independent A-D range controls and guarded file-path mapping for partial
  multi-session loads.

## 2026.7.13-1

- Changed primary app labels to resolved app titles and added saved launcher
  icons to the session library.
- Replaced sample counts with session counts in the library summary.
- Added per-session analysis range editing with complete metric recalculation.
- Added metric-specific dashboard chart colors while preserving session line
  patterns.

## 2026.7.13

- Added live measurement comparison against a selected static CSV with a
  persistent Compare On/Off control.
- Added elapsed-time-aligned dashed comparison overlays that stop at the saved
  session boundary without modifying live capture or export data.
- Added empty-file validation and regression tests for unequal-duration and
  nearest-sample comparison behavior.
- Added hover/focus feedback and full-filename tooltips to the comparison CSV
  control, plus an explicit action to remove the loaded comparison safely.

## 2026.7.11

- 상단에 **실시간 측정 / 분석 대시보드** 내비게이션을 추가했습니다. 기본 화면은 기존 실시간 측정이며, 화면을 전환해도 진행 중인 측정 상태는 그대로 유지됩니다.
- 실시간 화면의 단일 CSV 불러오기·드롭 재생 기능을 제거하고, 대시보드의 **영구 세션 라이브러리**로 대체했습니다. 측정 종료 시 자동 저장된 세션과 가져온 외부 CSV가 최신순으로 나열됩니다.
- 외부 CSV는 검증·중복 제거 후 `Documents/PerfInspect/Raw`로 복사합니다. 새 파일은 안정적인 `session_id`, 구형 파일은 헤더를 제외한 콘텐츠 지문으로 중복을 판단합니다.
- **단일 세션 분석**(메타데이터·KPI·CPU/GPU/메모리/전력/배터리/온도 상세·시계열 8차트)과 **세션 비교**(기준 1개 + 비교 최대 3개, 절대·퍼센트 delta, 조건 불일치 경고)를 제공합니다.
- 모든 집계는 Python의 단일 `SessionAnalysis` 계산 경로에서 수행합니다. 유효 표본만 집계하고, 빈 셀은 null로 보존하며 실제 측정된 0만 유효값으로 취급합니다. 커버리지 80% 미만이면 부분 데이터 경고를 표시합니다.
- CSV 포맷을 v2로 확장해 `schema_version`, 안정적 `session_id`, 세션명, 앱 라벨, 플랫폼·비식별 기기 메타데이터와 `missed_vsync`·`slow_ui_thread`·`frame_deadline_missed`·메모리 code/stack 필드를 기록합니다. 원본 기기 serial은 계속 저장하지 않습니다.
- 불러오기 시 iOS를 Android로 강제하던 문제와 앱 라벨·플랫폼·serial hash 복원 오류를 수정했습니다.

## 2026.7.10

- 계정 로그인, 구독·쿼터·업그레이드, 별도 웹사이트와 Cloudflare 연동을 제거했습니다.
- **저장 시 클라우드로 전송**은 사용자가 입력한 데이터 전송 엔드포인트로 JSON을 보내는 로컬 설정 기능으로 유지합니다.
- 데스크톱 앱은 계정이나 엔타이틀먼트 확인 없이 로컬 측정을 시작할 수 있습니다.
- Android와 iOS 디바이스를 병렬로 검색하고 자동 폴링해 새 연결을 더 빠르게 인식합니다.
- 설치 앱 목록은 빠른 기본 목록을 먼저 표시하고 필요한 행의 라벨·아이콘만 지연 로드합니다.
- 측정 시작 시 기존 디바이스·앱 메타데이터를 재사용하고 독립적인 Android 초기화 작업을 병렬화합니다.

---

## 이전
- 2026.6.26 이전: 로컬 전용 데스크톱 측정 도구(Android/iOS 실시간 캡처, CSV 저장). 자세한 내역은 git 히스토리 참고.
