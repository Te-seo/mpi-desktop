// iosReadiness — strings for IosReadinessBadge.
export const ko = {
  tunnelSetupTimeout:
    'iOS 터널이 아직 준비되지 않았습니다. UAC 승인을 완료했는지 확인한 뒤 자동 설정을 다시 실행하세요.',
  uacDeclined: '관리자 권한 요청이 거부되었습니다',
  tunnelSetupFailed: '터널 설정에 실패했습니다',
  checkingReadiness: 'iOS 준비 상태 확인 중',
  toolMissingLabel: 'pymobiledevice3 없음',
  toolMissingHint:
    'iOS 측정 도구(pymobiledevice3)가 설치되어 있지 않습니다. 자동 설치를 누르면 앱이 직접 설치합니다(1~2분).',
  installToolAction: '자동 설치',
  pythonMissingHint: 'Python 인터프리터를 찾을 수 없습니다. python.org에서 Python 3.11+를 먼저 설치하세요.',
  tunnelStartFailedLabel: 'iOS 터널 시작 실패',
  tunnelFailedHintWithState: (taskState: string) =>
    `예약 작업 상태: ${taskState}. 터널 준비가 완료되지 않았습니다. 자동 설정을 다시 실행해 주세요.`,
  tunnelFailedHint: '터널 준비가 완료되지 않았습니다. 자동 설정을 다시 실행해 주세요.',
  autoSetupAction: '자동 설정',
  tunnelOffLabel: 'iOS 터널 꺼짐',
  tunnelOffHint:
    'iOS 17 이상 DVT 측정은 USB와 무선 모두 백그라운드 터널이 필요합니다. 자동 설정을 누르면 관리자 승인(UAC) 한 번으로 등록됩니다.',
  tunnelStartingLabel: 'iOS 터널 시작 중',
  tunnelStartingHint:
    '터널 자동 시작 작업을 등록하고 데몬 응답을 확인하는 중입니다. 관리자 승인 후 최대 30초 정도 걸릴 수 있습니다.',
  tunnelStartFailedWithCode: (code: number) => `iOS 터널 시작 실패 (${code})`,
  tunnelStartNeededLabel: 'iOS 터널 시작 필요',
  tunnelReconfigureHintWithState: (taskState: string) =>
    `예약 작업 상태: ${taskState}. 자동 설정을 다시 눌러 터널 작업을 재등록하고 시작하세요.`,
  tunnelReconfigureHint: '자동 설정을 다시 눌러 터널 작업을 재등록하고 시작하세요.',
  reconfigureAction: '다시 설정',
  tunnelRunningLabel: 'iOS 터널 실행 중',
  tunnelRunningHint: '터널 데몬은 실행 중입니다. iPhone을 잠금 해제한 상태로 USB 또는 같은 Wi-Fi에 연결하세요.',
  wirelessReadyLabel: (tunnels: number) => `iOS 무선 준비 완료 (터널 ${tunnels})`,
  usbReadyLabel: (tunnels: number) => `iOS USB 준비 완료 (터널 ${tunnels})`,
  readyHintWithVersion: (version: string) => `pymobiledevice3 ${version} · iOS DVT 측정 준비 완료`,
  readyHint: 'iOS DVT 측정 준비 완료',
  checkingReadinessHint: 'iOS DVT 측정에 필요한 터널 상태를 확인하는 중입니다.',
  installingBusy: '설치 중...',
  configuringBusy: '설정 중...',
}
export type IosReadinessMessages = typeof ko
export const en: IosReadinessMessages = {
  tunnelSetupTimeout:
    'The iOS tunnel is not ready yet. Make sure you approved the UAC prompt, then run Auto Setup again.',
  uacDeclined: 'Administrator permission request was denied',
  tunnelSetupFailed: 'Tunnel setup failed',
  checkingReadiness: 'Checking iOS readiness',
  toolMissingLabel: 'pymobiledevice3 missing',
  toolMissingHint:
    'The iOS measurement tool (pymobiledevice3) is not installed. Click Auto Install and the app installs it for you (1–2 min).',
  installToolAction: 'Auto Install',
  pythonMissingHint: 'No Python interpreter found. Install Python 3.11+ from python.org first.',
  tunnelStartFailedLabel: 'iOS tunnel failed to start',
  tunnelFailedHintWithState: (taskState) =>
    `Scheduled task status: ${taskState}. The tunnel is not ready. Please run Auto Setup again.`,
  tunnelFailedHint: 'The tunnel is not ready. Please run Auto Setup again.',
  autoSetupAction: 'Auto Setup',
  tunnelOffLabel: 'iOS tunnel off',
  tunnelOffHint:
    'iOS 17+ DVT measurement needs a background tunnel for both USB and wireless. Click Auto Setup to register it with a single administrator (UAC) approval.',
  tunnelStartingLabel: 'iOS tunnel starting',
  tunnelStartingHint:
    'Registering the tunnel auto-start task and waiting for the daemon to respond. This can take up to 30 seconds after administrator approval.',
  tunnelStartFailedWithCode: (code) => `iOS tunnel failed to start (${code})`,
  tunnelStartNeededLabel: 'iOS tunnel start required',
  tunnelReconfigureHintWithState: (taskState) =>
    `Scheduled task status: ${taskState}. Click Auto Setup again to re-register and start the tunnel task.`,
  tunnelReconfigureHint: 'Click Auto Setup again to re-register and start the tunnel task.',
  reconfigureAction: 'Reconfigure',
  tunnelRunningLabel: 'iOS tunnel running',
  tunnelRunningHint:
    'The tunnel daemon is running. Unlock your iPhone and connect it over USB or the same Wi-Fi.',
  wirelessReadyLabel: (tunnels) => `iOS wireless ready (${tunnels} tunnel(s))`,
  usbReadyLabel: (tunnels) => `iOS USB ready (${tunnels} tunnel(s))`,
  readyHintWithVersion: (version) => `pymobiledevice3 ${version} · iOS DVT measurement ready`,
  readyHint: 'iOS DVT measurement ready',
  checkingReadinessHint: 'Checking the tunnel status required for iOS DVT measurement.',
  installingBusy: 'Installing...',
  configuringBusy: 'Configuring...',
}
export const zh: IosReadinessMessages = {
  tunnelSetupTimeout: 'iOS 隧道尚未就绪。请确认已完成 UAC 授权，然后重新运行自动设置。',
  uacDeclined: '管理员权限请求被拒绝',
  tunnelSetupFailed: '隧道设置失败',
  checkingReadiness: '正在检查 iOS 就绪状态',
  toolMissingLabel: '缺少 pymobiledevice3',
  toolMissingHint: '未安装 iOS 测量工具 (pymobiledevice3)。点击自动安装，应用会自动为你安装（1~2 分钟）。',
  installToolAction: '自动安装',
  pythonMissingHint: '未找到 Python 解释器。请先从 python.org 安装 Python 3.11+。',
  tunnelStartFailedLabel: 'iOS 隧道启动失败',
  tunnelFailedHintWithState: (taskState) =>
    `计划任务状态：${taskState}。隧道尚未就绪。请重新运行自动设置。`,
  tunnelFailedHint: '隧道尚未就绪。请重新运行自动设置。',
  autoSetupAction: '自动设置',
  tunnelOffLabel: 'iOS 隧道已关闭',
  tunnelOffHint:
    'iOS 17 及以上的 DVT 测量在 USB 和无线下都需要后台隧道。点击自动设置，只需一次管理员授权 (UAC) 即可注册。',
  tunnelStartingLabel: 'iOS 隧道启动中',
  tunnelStartingHint: '正在注册隧道自动启动任务并等待守护进程响应。管理员授权后最多可能需要 30 秒左右。',
  tunnelStartFailedWithCode: (code) => `iOS 隧道启动失败 (${code})`,
  tunnelStartNeededLabel: '需要启动 iOS 隧道',
  tunnelReconfigureHintWithState: (taskState) =>
    `计划任务状态：${taskState}。请再次点击自动设置以重新注册并启动隧道任务。`,
  tunnelReconfigureHint: '请再次点击自动设置以重新注册并启动隧道任务。',
  reconfigureAction: '重新设置',
  tunnelRunningLabel: 'iOS 隧道运行中',
  tunnelRunningHint: '隧道守护进程正在运行。请解锁 iPhone，并通过 USB 或同一 Wi-Fi 连接。',
  wirelessReadyLabel: (tunnels) => `iOS 无线就绪（隧道 ${tunnels}）`,
  usbReadyLabel: (tunnels) => `iOS USB 就绪（隧道 ${tunnels}）`,
  readyHintWithVersion: (version) => `pymobiledevice3 ${version} · iOS DVT 测量就绪`,
  readyHint: 'iOS DVT 测量就绪',
  checkingReadinessHint: '正在检查 iOS DVT 测量所需的隧道状态。',
  installingBusy: '正在安装...',
  configuringBusy: '正在设置...',
}
