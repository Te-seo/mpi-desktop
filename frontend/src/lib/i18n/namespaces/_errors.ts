// User-facing guidance for backend, device, engine, and endpoint errors.
export const ko = {
  signInRequired: '측정을 시작하려면 다시 로그인하세요.',
  measurementQuotaExhausted: '계정의 측정 가능 시간을 모두 사용했습니다. 플랜과 남은 시간을 확인하세요.',
  measurementQuotaUnavailable: '계정의 측정 한도를 확인하지 못해 시작하지 않았습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.',
  targetAppTerminationFailed:
    '측정은 종료되었지만 대상 앱을 종료하지 못했습니다. 기기 연결을 확인하고 앱을 직접 종료하세요.',
  iosWifiUnplug:
    'iOS Wi-Fi 연결이 켜졌습니다. Windows에서 무선으로 전환하려면 지금 USB 케이블을 뽑으세요. (iPhone은 잠금 해제 상태로, PC와 같은 Wi-Fi에 두세요.) 백그라운드 iOS 터널이 Wi-Fi로 재연결되어 기기가 무선으로 다시 잡힙니다. 다시 안 잡히면 상단 iOS 준비 배지에서 터널이 실행 중인지, PC와 iPhone이 같은 Wi-Fi인지 확인하세요.',
  noWirelessDevice:
    '무선 기기를 찾지 못했습니다. iPhone이 잠금 해제된 상태로 PC와 같은 Wi-Fi에 연결되어 있는지 확인한 뒤 다시 시도하세요.',
  tunnelRequired:
    'iOS 터널이 필요합니다. 상단 iOS 준비 배지의 "자동 설정"으로 백그라운드 터널을 등록한 뒤 다시 시도하세요.',
  pmd3NotInstalled:
    'pymobiledevice3가 설치되어 있지 않습니다. 상단 iOS 준비 배지의 "자동 설치"를 사용하세요.',
  developerModeOff:
    '기기에서 개발자 모드를 켜야 합니다: 설정 > 개인정보 보호 및 보안 > 개발자 모드를 활성화한 뒤 재부팅하세요.',
  passwordRequired: 'iPhone 잠금을 해제한 뒤 "이 컴퓨터를 신뢰" 하세요.',
  pairingInvalid:
    '기기 페어링이 유효하지 않습니다. iPhone을 USB로 연결하고 잠금 해제 후 "이 컴퓨터를 신뢰"를 다시 승인하세요.',
  pidNotFound: '앱 프로세스를 찾지 못했습니다. 기기에서 측정할 앱을 실행한 상태로 다시 시도하세요.',
  appNotRunning:
    '선택한 앱이 기기에서 실행 중이 아니어서 측정을 시작할 수 없습니다. 기기에서 앱을 실행한 뒤 다시 시작하세요.',
  appExited:
    '측정 중이던 앱이 종료되어 측정을 마쳤습니다. 수집된 데이터는 저장되었습니다. 앱을 다시 실행한 뒤 새 측정을 시작하세요.',
  appBackgrounded:
    '측정 중이던 앱이 백그라운드로 전환되어 측정을 마쳤습니다. 수집된 데이터는 저장되었습니다. 앱을 화면에 띄운 상태로 새 측정을 시작하세요.',
  deviceUnauthorized: 'USB 디버깅 권한이 승인되지 않았습니다. 기기 화면의 허용 대화상자를 승인하세요.',
  adbNotFound: 'adb를 찾을 수 없습니다. Android platform-tools를 설치하고 PATH에 추가하세요.',
  engineNotRunning:
    '성능 측정 엔진을 시작하지 못했습니다. 앱을 재시작하거나 다시 설치해 주세요. 문제가 계속되면 백신·방화벽이 data-engine 실행을 차단하지 않았는지 확인하세요.',
  backendTimeout: '백엔드 응답이 지연되고 있습니다. 기기 연결과 측정 엔진 상태를 확인한 뒤 다시 시도하세요.',
  endpoint4xx: '엔드포인트가 요청을 거부했습니다. 주소와 수신 서버 설정을 확인하세요.',
  endpoint5xx: '엔드포인트 서버 오류로 전송에 실패했습니다. 잠시 후 다시 시도하세요.',
  transportTimeout: '전송 시간이 초과되었습니다. 네트워크 연결을 확인한 뒤 다시 시도하세요.',
  invalidEndpointUrl: '엔드포인트 URL이 올바르지 않습니다. 설정에서 주소를 확인하세요.',
  unsupportedProtocol: '지원하지 않는 URL 프로토콜입니다. http:// 또는 https:// 주소를 사용하세요.',
  networkError: '엔드포인트에 연결할 수 없습니다. 네트워크 연결과 주소를 확인한 뒤 다시 시도하세요.',
  unexpected: '예기치 못한 오류가 발생했습니다. 기기 연결과 앱 상태를 확인한 뒤 다시 시도하세요.',
  engineStartFailed:
    '성능 측정 엔진을 시작하지 못했습니다. 앱을 다시 설치하거나 재시작해 주세요. 문제가 계속되면 백신·방화벽이 data-engine 실행을 차단하지 않았는지 확인하세요.',
  engineStartFailedDev:
    '성능 측정 엔진(Python)을 시작하지 못했습니다. Python이 설치되어 PATH에 있는지, mpi 패키지가 설치되어 있는지 확인하세요.',
  engineExited: '성능 측정 엔진이 예기치 않게 종료되었습니다. 앱을 재시작해 주세요. 문제가 계속되면 앱을 다시 설치하세요.',
  desktopOnly: '데스크톱 앱에서만 사용할 수 있습니다.',
}

export type ErrorMessages = typeof ko

export const en: ErrorMessages = {
  signInRequired: 'Sign in again before starting a capture.',
  measurementQuotaExhausted: 'Your measurement allowance is exhausted. Check your plan and remaining time.',
  measurementQuotaUnavailable: 'Capture did not start because its allowance could not be verified. Check your internet connection and retry.',
  targetAppTerminationFailed:
    'The measurement ended, but the target app could not be terminated. Check the device connection and close the app manually.',
  iosWifiUnplug:
    'iOS Wi-Fi is now on. To switch to wireless on Windows, unplug the USB cable now. (Keep the iPhone unlocked and on the same Wi-Fi as your PC.) The background iOS tunnel reconnects over Wi-Fi so the device is picked up wirelessly again. If it doesn’t reappear, check the iOS readiness badge above to confirm the tunnel is running and that the PC and iPhone are on the same Wi-Fi.',
  noWirelessDevice:
    'No wireless device found. Make sure the iPhone is unlocked and on the same Wi-Fi as your PC, then try again.',
  tunnelRequired:
    'An iOS tunnel is required. Use "Auto setup" on the iOS readiness badge above to register the background tunnel, then try again.',
  pmd3NotInstalled:
    'pymobiledevice3 is not installed. Use "Auto install" on the iOS readiness badge above.',
  developerModeOff:
    'Developer Mode must be enabled on the device: Settings > Privacy & Security > Developer Mode, then reboot.',
  passwordRequired: 'Unlock the iPhone, then tap "Trust This Computer".',
  pairingInvalid:
    'Device pairing is invalid. Connect the iPhone over USB, unlock it, and approve "Trust This Computer" again.',
  pidNotFound: 'Could not find the app process. Launch the app you want to measure on the device, then try again.',
  appNotRunning:
    'The selected app is not running on the device, so the measurement could not start. Launch the app, then start again.',
  appExited:
    'The measured app exited, so the measurement was ended. Collected data has been saved. Relaunch the app to start a new measurement.',
  appBackgrounded:
    'The measured app moved to the background, so the measurement was ended. Collected data has been saved. Keep the app on screen and start a new measurement.',
  deviceUnauthorized: 'USB debugging is not authorized. Approve the allow dialog on the device screen.',
  adbNotFound: 'adb could not be found. Install the Android platform-tools and add them to your PATH.',
  engineNotRunning:
    'Could not start the measurement engine. Restart or reinstall the app. If it persists, make sure antivirus/firewall isn’t blocking data-engine.',
  backendTimeout: 'The backend is slow to respond. Check the device connection and engine status, then try again.',
  endpoint4xx: 'The endpoint rejected the request. Check the address and receiving server configuration.',
  endpoint5xx: 'The endpoint server failed to process the upload. Please try again shortly.',
  transportTimeout: 'The upload timed out. Check your network connection, then try again.',
  invalidEndpointUrl: 'The endpoint URL is invalid. Check the address in settings.',
  unsupportedProtocol: 'Unsupported URL protocol. Use an http:// or https:// address.',
  networkError: 'Could not reach the endpoint. Check your network connection and the address, then try again.',
  unexpected: 'An unexpected error occurred. Check the device connection and app state, then try again.',
  engineStartFailed:
    'Could not start the measurement engine. Reinstall or restart the app. If it persists, make sure antivirus/firewall isn’t blocking data-engine.',
  engineStartFailedDev:
    'Could not start the measurement engine (Python). Check that Python is installed and on PATH, and that the mpi package is installed.',
  engineExited: 'The measurement engine exited unexpectedly. Restart the app. If it persists, reinstall the app.',
  desktopOnly: 'Available only in the desktop app.',
}

export const zh: ErrorMessages = {
  signInRequired: '开始测量前请重新登录。',
  measurementQuotaExhausted: '帐户的测量时间已用尽。请检查套餐和剩余时间。',
  measurementQuotaUnavailable: '无法验证测量限额，因此未开始测量。请检查网络连接后重试。',
  targetAppTerminationFailed:
    '测量已结束，但无法终止目标应用。请检查设备连接并手动关闭该应用。',
  iosWifiUnplug:
    'iOS Wi-Fi 已开启。要在 Windows 上切换到无线模式，请立即拔掉 USB 数据线。（保持 iPhone 解锁，并与电脑连接同一个 Wi-Fi。）后台 iOS 隧道会通过 Wi-Fi 重新连接，从而无线重新识别设备。如果仍未出现，请查看上方的 iOS 就绪徽章，确认隧道正在运行、且电脑与 iPhone 在同一 Wi-Fi。',
  noWirelessDevice: '未找到无线设备。请确认 iPhone 已解锁并与电脑连接同一个 Wi-Fi，然后重试。',
  tunnelRequired: '需要 iOS 隧道。请使用上方 iOS 就绪徽章的“自动设置”注册后台隧道，然后重试。',
  pmd3NotInstalled: '未安装 pymobiledevice3。请使用上方 iOS 就绪徽章的“自动安装”。',
  developerModeOff: '需要在设备上启用开发者模式：设置 > 隐私与安全性 > 开发者模式，然后重启设备。',
  passwordRequired: '请解锁 iPhone，然后点按“信任此电脑”。',
  pairingInvalid: '设备配对无效。请用 USB 连接 iPhone，解锁后重新确认“信任此电脑”。',
  pidNotFound: '未找到应用进程。请在设备上运行要测量的应用后重试。',
  appNotRunning: '所选应用未在设备上运行，无法开始测量。请先在设备上启动该应用，然后重新开始。',
  appExited: '被测量的应用已退出，测量已结束。已收集的数据已保存。请重新启动应用后开始新的测量。',
  appBackgrounded: '被测量的应用已切换到后台，测量已结束。已收集的数据已保存。请保持应用在前台并开始新的测量。',
  deviceUnauthorized: 'USB 调试未授权。请在设备屏幕上确认允许对话框。',
  adbNotFound: '找不到 adb。请安装 Android platform-tools 并将其加入 PATH。',
  engineNotRunning:
    '无法启动性能测量引擎。请重启或重新安装应用。若问题持续，请确认杀毒软件/防火墙未阻止 data-engine 运行。',
  backendTimeout: '后端响应缓慢。请检查设备连接与测量引擎状态后重试。',
  endpoint4xx: '端点拒绝了请求。请检查地址和接收服务器配置。',
  endpoint5xx: '端点服务器处理上传失败。请稍后重试。',
  transportTimeout: '上传超时。请检查网络连接后重试。',
  invalidEndpointUrl: '端点 URL 无效。请在设置中检查地址。',
  unsupportedProtocol: '不支持的 URL 协议。请使用 http:// 或 https:// 地址。',
  networkError: '无法连接到端点。请检查网络连接与地址后重试。',
  unexpected: '发生意外错误。请检查设备连接与应用状态后重试。',
  engineStartFailed:
    '无法启动性能测量引擎。请重新安装或重启应用。若问题持续，请确认杀毒软件/防火墙未阻止 data-engine 运行。',
  engineStartFailedDev:
    '无法启动性能测量引擎（Python）。请确认已安装 Python 且在 PATH 中，并已安装 mpi 包。',
  engineExited: '性能测量引擎意外退出。请重启应用。若问题持续，请重新安装应用。',
  desktopOnly: '仅在桌面应用中可用。',
}
