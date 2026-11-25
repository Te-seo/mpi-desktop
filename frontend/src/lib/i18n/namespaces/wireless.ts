// wireless — strings for WirelessSetup.
export const ko = {
  connectedLabel: (name: string) => `무선 연결됨: ${name}`,
  changeDeviceTitle: '다른 기기로 변경',
  change: '변경',
  prepareHintIos:
    'iPhone을 USB로 연결해 페어링하면 Wi-Fi 연결을 켜고 무선 기기로 전환합니다. 기기는 잠금 해제 상태로 PC와 같은 Wi-Fi에 있어야 합니다.',
  prepareHintAndroid:
    'USB로 연결된 기기를 TCP/IP 모드(포트 5555)로 전환한 뒤 무선으로 다시 연결합니다. 기기와 PC가 같은 네트워크에 있어야 합니다.',
  usbDeviceLabel: 'USB 기기',
  switching: '무선 전환 중...',
  retrySwitch: '무선 전환 다시 시도',
  autoSwitchHint: 'USB 기기를 연결하면 자동으로 무선 전환합니다',
  directSelectTitle: '이미 무선으로 보이는 기기를 USB 준비 없이 바로 선택합니다',
  directSelect: '무선 기기 직접 선택',
  wirelessDeviceLabel: '무선 기기',
  prepareUsbSwitch: 'USB로 전환 준비',
}
export type WirelessMessages = typeof ko
export const en: WirelessMessages = {
  connectedLabel: (name) => `Wireless connected: ${name}`,
  changeDeviceTitle: 'Change to another device',
  change: 'Change',
  prepareHintIos:
    'Connect the iPhone via USB and pair to enable Wi-Fi and switch it to a wireless device. The device must be unlocked and on the same Wi-Fi as this PC.',
  prepareHintAndroid:
    'Switches the USB-connected device to TCP/IP mode (port 5555), then reconnects over wireless. The device and PC must be on the same network.',
  usbDeviceLabel: 'USB device',
  switching: 'Switching to wireless...',
  retrySwitch: 'Retry wireless switch',
  autoSwitchHint: 'Connect a USB device to switch to wireless automatically',
  directSelectTitle: 'Select a device that already appears wireless, without USB prep',
  directSelect: 'Select wireless device directly',
  wirelessDeviceLabel: 'Wireless device',
  prepareUsbSwitch: 'Prepare USB switch',
}
export const zh: WirelessMessages = {
  connectedLabel: (name) => `已连接无线：${name}`,
  changeDeviceTitle: '更换其他设备',
  change: '更换',
  prepareHintIos:
    '通过 USB 连接 iPhone 并配对后，将开启 Wi-Fi 连接并切换为无线设备。设备须处于解锁状态并与本机连接同一 Wi-Fi。',
  prepareHintAndroid:
    '将 USB 连接的设备切换为 TCP/IP 模式（端口 5555），然后通过无线重新连接。设备与本机须处于同一网络。',
  usbDeviceLabel: 'USB 设备',
  switching: '正在切换为无线...',
  retrySwitch: '重试无线切换',
  autoSwitchHint: '连接 USB 设备后将自动切换为无线',
  directSelectTitle: '直接选择已显示为无线的设备，无需 USB 准备',
  directSelect: '直接选择无线设备',
  wirelessDeviceLabel: '无线设备',
  prepareUsbSwitch: '准备切换到 USB',
}
