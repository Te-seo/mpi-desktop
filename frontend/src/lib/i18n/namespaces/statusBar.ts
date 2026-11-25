// statusBar — status-state labels for the StatusBar footer.
export const ko = {
  stateIdle: '대기',
  stateRefreshing: '디바이스 새로고침',
  stateTracing: '캡처 중',
  stateStopped: '중단됨',
  stateError: '오류',
}

export type StatusBarMessages = typeof ko

export const en: StatusBarMessages = {
  stateIdle: 'Idle',
  stateRefreshing: 'Refreshing devices',
  stateTracing: 'Capturing',
  stateStopped: 'Stopped',
  stateError: 'Error',
}

export const zh: StatusBarMessages = {
  stateIdle: '待机',
  stateRefreshing: '刷新设备',
  stateTracing: '采集中',
  stateStopped: '已停止',
  stateError: '错误',
}
