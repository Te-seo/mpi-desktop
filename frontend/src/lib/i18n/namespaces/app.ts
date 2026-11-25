// App header (App.tsx). The MPI product name stays untranslated; only the
// descriptive subtitle is localized.

export const ko = {
  title: 'MPI',
  subtitle: '모바일 성능 측정 대시보드',
  workspace: '워크스페이스',
  navigationLabel: '주요 화면',
  skipContent: '작업 화면으로 건너뛰기',
}

export type AppMessages = typeof ko

export const en: AppMessages = {
  title: 'MPI',
  subtitle: 'Mobile performance dashboard',
  workspace: 'WORKSPACE',
  navigationLabel: 'Main navigation',
  skipContent: 'Skip to workspace',
}

export const zh: AppMessages = {
  title: 'MPI',
  subtitle: '移动性能测量仪表板',
  workspace: '工作空间',
  navigationLabel: '主导航',
  skipContent: '跳转到工作区',
}
