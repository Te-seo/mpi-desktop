// charts — strings for LiveCharts.
export const ko = {
  batteryTemperature: 'Temp (배터리)',
  skinTemperatureHelp: 'Android thermalservice의 현재 TYPE_SKIN 센서 중 최댓값입니다. 기기가 보고하는 표면 온도이며, 값을 제공하지 않으면 측정 불가로 표시합니다. 배터리 온도로 대체하지 않으며, 같은 Skin 기준의 기록만 비교합니다.',
  skinMeasured: 'Android TYPE_SKIN · 측정됨',
  skinUnavailable: 'Android TYPE_SKIN · 측정 불가',
  emptyState: '측정을 시작하면 실시간 차트가 표시됩니다.',
  reorderDragLabel: '차트 순서 변경 드래그',
  reorderDragTitle: '드래그해서 차트 순서 변경',
  cpuUsage: 'CPU 사용률',
  systemCpu: '시스템 CPU',
  appCpuWholeDevice: '앱 CPU (기기 전체 기준)',
  appCpuNormalized: '앱 CPU (정규화)',
  appCpuRaw: '앱 CPU (코어 합산 원본)',
  logicalCpuCount: '논리 CPU 수',
  cpuUsageHelp:
    '시스템 CPU와 앱 CPU를 모두 기기 전체 0–100% 기준으로 표시합니다. 원본 앱 CPU는 코어별 사용률의 합이라 100%를 넘을 수 있습니다.',
  cpuNormalizationHelp:
    '앱 전용 정규화 값입니다. Android는 현재/최대 클럭 데이터를 모두 사용할 수 있는 경우에만 (코어 합산 앱 CPU ÷ 논리 CPU 수) × (현재 전체 CPU 클럭 합 ÷ 최대 전체 CPU 클럭 합)으로 계산하며, 클럭 데이터가 없으면 값을 제공하지 않습니다. iOS는 논리 CPU 수로만 정규화합니다.',
}
export type ChartsMessages = typeof ko
export const en: ChartsMessages = {
  batteryTemperature: 'Temp (Battery)',
  skinTemperatureHelp: 'Maximum current TYPE_SKIN reading from Android thermalservice. This is the device-reported surface temperature. Missing readings remain unavailable, never replaced with battery temperature. Comparisons use recordings with the same Skin source.',
  skinMeasured: 'Android TYPE_SKIN · measured',
  skinUnavailable: 'Android TYPE_SKIN · unavailable',
  emptyState: 'Start a measurement to see live charts.',
  reorderDragLabel: 'Drag to reorder charts',
  reorderDragTitle: 'Drag to reorder charts',
  cpuUsage: 'CPU utilization',
  systemCpu: 'System CPU',
  appCpuWholeDevice: 'App CPU (whole device)',
  appCpuNormalized: 'App CPU (normalized)',
  appCpuRaw: 'App CPU (raw core sum)',
  logicalCpuCount: 'Logical CPUs',
  cpuUsageHelp:
    'System and app CPU use the same whole-device 0–100% scale. Raw app CPU sums per-core use and can exceed 100%.',
  cpuNormalizationHelp:
    'App-only normalized value. On Android, when current and maximum clock data are available: (raw core-summed app CPU ÷ logical CPUs) × (current aggregate CPU clock ÷ maximum aggregate CPU clock); otherwise the value is unavailable. iOS uses logical-CPU normalization only.',
}
export const zh: ChartsMessages = {
  batteryTemperature: '温度（电池）',
  skinTemperatureHelp: 'Android thermalservice 当前 TYPE_SKIN 传感器读数的最大值，表示设备报告的表面温度。缺失时显示不可用，不使用电池温度替代。仅比较采用相同 Skin 来源的记录。',
  skinMeasured: 'Android TYPE_SKIN · 已测量',
  skinUnavailable: 'Android TYPE_SKIN · 不可用',
  emptyState: '开始测量后将显示实时图表。',
  reorderDragLabel: '拖动以重新排列图表',
  reorderDragTitle: '拖动以重新排列图表',
  cpuUsage: 'CPU 使用率',
  systemCpu: '系统 CPU',
  appCpuWholeDevice: '应用 CPU（整机口径）',
  appCpuNormalized: '应用 CPU（归一化）',
  appCpuRaw: '应用 CPU（各核原始合计）',
  logicalCpuCount: '逻辑 CPU 数',
  cpuUsageHelp:
    '系统 CPU 与应用 CPU 均按整机 0–100% 口径显示。原始应用 CPU 会汇总各核心使用率，因此可能超过 100%。',
  cpuNormalizationHelp:
    '仅针对应用的归一化值。Android 仅在当前和最大频率数据均可用时按（各核应用 CPU 原始合计 ÷ 逻辑 CPU 数）×（当前 CPU 总频率 ÷ 最大 CPU 总频率）计算，否则该值不可用。iOS 仅按逻辑 CPU 数归一化。',
}
