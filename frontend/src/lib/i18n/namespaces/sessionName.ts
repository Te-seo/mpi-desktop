// sessionName — names a capture before transferring it to the configured endpoint.
export const ko = {
  title: '세션 이름 지정',
  description: '전송할 측정 세션을 식별할 이름을 지정하세요.',
  placeholder: '세션 이름',
  save: '저장하고 전송',
  skip: '건너뛰기',
  saving: '전송 중…',
}
export type SessionNameMessages = typeof ko
export const en: SessionNameMessages = {
  title: 'Name this session',
  description: 'Give the measurement session a name before sending it.',
  placeholder: 'Session name',
  save: 'Save and send',
  skip: 'Skip',
  saving: 'Sending…',
}
export const zh: SessionNameMessages = {
  title: '命名此会话',
  description: '发送前为测量会话指定一个名称。',
  placeholder: '会话名称',
  save: '保存并发送',
  skip: '跳过',
  saving: '发送中…',
}
