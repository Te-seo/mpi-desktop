// summary — strings for SummaryOverlay.
export const ko = {
  rangeSummary: '구간 요약',
  saveHint: '우클릭하여 구간 데이터 저장',
  saving: '저장 중…',
  saveRange: (n: number) => `구간 데이터 저장 (${n})`,
}
export type SummaryMessages = typeof ko
export const en: SummaryMessages = {
  rangeSummary: 'Range summary',
  saveHint: 'Right-click to save range data',
  saving: 'Saving…',
  saveRange: (n) => `Save range data (${n})`,
}
export const zh: SummaryMessages = {
  rangeSummary: '区间摘要',
  saveHint: '右键单击以保存区间数据',
  saving: '保存中…',
  saveRange: (n) => `保存区间数据 (${n})`,
}
