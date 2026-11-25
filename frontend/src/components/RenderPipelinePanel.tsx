import { useLocale } from '../lib/i18n'
import type { RenderPipeline } from '../lib/types'

export function RenderPipelinePanel({ data }: { data?: RenderPipeline | null }) {
  const { locale } = useLocale()
  if (!data) return null
  const label = (en: string, ko: string, zh: string) => locale === 'ko' ? ko : locale === 'zh' ? zh : en
  const ms = (value?: number | null) => value == null ? '—' : `${value.toFixed(3)} ms`
  const queue = data.buffer_queue
  const surface = data.surface_present
  const items = [
    [label('Buffer submit · avg', '버퍼 제출 · 평균', '提交缓冲区 · 平均'), ms(queue?.submit_ms_avg)],
    [label('Buffer acquire · avg', '버퍼 획득 · 평균', '获取缓冲区 · 平均'), ms(queue?.acquire_ms_avg)],
    [label('Queue depth · peak', '큐 깊이 · 최대', '队列深度 · 峰值'), queue?.queue_depth_max ?? '—'],
    [label('Queue → acquire · avg', '큐 대기 · 평균', '队列等待 · 平均'), ms(queue?.queue_wait_ms_avg)],
    [label('Queue → acquire · max', '큐 대기 · 최대', '队列等待 · 最大'), ms(queue?.queue_wait_ms_max)],
    ...(data.actual_duration_ms_avg != null || !surface ? [
      [label('FrameTimeline · avg', 'FrameTimeline · 평균', 'FrameTimeline · 平均'), ms(data.actual_duration_ms_avg)],
    ] : []),
    ...(surface ? [
      [label('Frame interval · avg', '프레임 표시 간격 · 평균', '帧呈现间隔 · 平均'), ms(surface.surface_interval_ms_avg)],
      [label('Frame interval · max', '프레임 표시 간격 · 최대', '帧呈现间隔 · 最大'), ms(surface.surface_interval_ms_max)],
    ] : []),
  ]
  return (
    <section className="mb-4 rounded-xl border border-line bg-surface p-4">
      <h2 className="font-semibold text-ink">{label('Android render pipeline', 'Android 렌더 파이프라인', 'Android 渲染管线')}</h2>
      <p className="mt-1 text-xs text-muted">
        {label('Whole measurement. Queue wait is measured from enqueue to acquire for matched buffers.',
          '전체 측정 기준입니다. 같은 버퍼가 큐에 들어간 뒤 획득되기까지의 시간을 기록합니다.',
          '覆盖整段测量。等待时长来自同一缓冲区的入队及获取事件。')}
      </p>
      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        {items.map(([name, value]) => (
          <div key={name} className="rounded-lg border border-line p-2.5">
            <dt className="text-xs text-muted">{name}</dt>
            <dd className="mt-1 font-mono text-sm tabular-nums text-ink">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-muted">
        {queue?.queue_wait_samples
          ? `${label('Matched buffers', '매칭된 버퍼', '已匹配缓冲区')} ${queue.queue_wait_samples.toLocaleString(locale)}`
          : label('Waiting for trace data. Some games or devices may not expose these events.',
            '추적 데이터를 기다리는 중입니다. 게임이나 기기에 따라 이벤트가 제공되지 않을 수 있습니다.',
            '正在等待跟踪数据。部分游戏或设备可能不提供这些事件。')}
      </p>
      {surface && (
        <p className="mt-1 text-xs text-muted">
          {label('SurfaceFlinger presentation timestamps', 'SurfaceFlinger 실제 표시 시각', 'SurfaceFlinger 实际呈现时刻')}
          {' · '}{(surface.surface_present_frames ?? 0).toLocaleString(locale)} {label('frames', '프레임', '帧')}
        </p>
      )}
    </section>
  )
}
