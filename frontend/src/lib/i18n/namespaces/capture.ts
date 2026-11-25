// capture — strings used by LiveCaptureProvider endpoint transfer callbacks.
export const ko = {
  pause: '일시중지', resume: '측정 재개', paused: '일시중지됨',
  addCut: '컷 추가', endCut: '컷 종료', cuts: '세션 컷', cutName: '컷 이름',
  cutDefault: '컷', cutStart: '컷 시작', cutEnd: '컷 끝', saveCut: '컷 저장',
  editCut: '컷 편집', deleteCut: '컷 삭제', analyzeCut: '컷 분석', cancel: '취소',
  invalidCut: '이름과 유효한 시작·종료 시간을 입력하세요. 구간은 세션 길이 안에 있어야 합니다.',
  cutHint: '원본 데이터를 유지하며 구간을 저장합니다. 시간 형식: 분:초',
  pauseHint: '중지 구간은 측정 데이터와 자동 종료 시간에서 제외됩니다. 일시정지를 사용한 세션은 재개 후에도 리더보드에서 제외되며 개인 저장·분석은 유지됩니다.',
  endpointSendFailed: (detail: string) => `엔드포인트 전송 실패: ${detail}`,
  dataSendFailed: '데이터 전송에 실패했습니다.',
}
export type CaptureMessages = typeof ko
export const en: CaptureMessages = {
  pause: 'Pause', resume: 'Resume capture', paused: 'Paused',
  addCut: 'Add cut', endCut: 'End cut', cuts: 'Session cuts', cutName: 'Cut name',
  cutDefault: 'Cut', cutStart: 'Cut start', cutEnd: 'Cut end', saveCut: 'Save cut',
  editCut: 'Edit cut', deleteCut: 'Delete cut', analyzeCut: 'Analyze cut', cancel: 'Cancel',
  invalidCut: 'Enter a name and valid start/end times within the session duration.',
  cutHint: 'Save intervals while preserving the original data. Time format: minutes:seconds',
  pauseHint: 'Paused time is excluded from measurement data and the auto-stop limit. Using pause excludes this session from the leaderboard even after resuming; personal saves and analysis remain available.',
  endpointSendFailed: (detail) => `Endpoint send failed: ${detail}`,
  dataSendFailed: 'Failed to send data.',
}
export const zh: CaptureMessages = {
  pause: '暂停', resume: '继续测量', paused: '已暂停',
  addCut: '添加片段', endCut: '结束片段', cuts: '会话片段', cutName: '片段名称',
  cutDefault: '片段', cutStart: '片段开始', cutEnd: '片段结束', saveCut: '保存片段',
  editCut: '编辑片段', deleteCut: '删除片段', analyzeCut: '分析片段', cancel: '取消',
  invalidCut: '请输入名称及会话时长范围内的有效开始和结束时间。',
  cutHint: '保留原始数据并保存区间。时间格式：分:秒',
  pauseHint: '暂停时间不计入测量数据和自动停止时限。使用暂停的会话在恢复后仍不计入排行榜，但可继续保存和分析个人数据。',
  endpointSendFailed: (detail) => `端点发送失败：${detail}`,
  dataSendFailed: '数据发送失败。',
}
