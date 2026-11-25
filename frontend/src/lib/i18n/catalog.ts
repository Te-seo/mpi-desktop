// Message catalog: assembles per-namespace modules into one dictionary per
// locale. `ko` is the source of truth; `en`/`zh` are typed as `Messages` so any
// missing or mistyped key is a compile error. Each namespace lives in its own
// module under ./namespaces/ so components can be localized independently.
//
// NOTE: keep the three assemblies (ko/en/zh) in the same key order and add every
// new namespace to all three.

import type { Locale } from './config'
import * as common from './namespaces/_common'
import * as errors from './namespaces/_errors'
import * as app from './namespaces/app'
import * as theme from './namespaces/theme'
import * as localeSelector from './namespaces/localeSelector'
import * as combobox from './namespaces/combobox'
import * as cloud from './namespaces/cloud'
import * as deviceSelector from './namespaces/deviceSelector'
import * as deviceInfo from './namespaces/deviceInfo'
import * as hoverOverlay from './namespaces/hoverOverlay'
import * as iosReadiness from './namespaces/iosReadiness'
import * as capture from './namespaces/capture'
import * as sessionName from './namespaces/sessionName'
import * as charts from './namespaces/charts'
import * as metricCard from './namespaces/metricCard'
import * as metricMenu from './namespaces/metricMenu'
import * as packageSelector from './namespaces/packageSelector'
import * as statusBar from './namespaces/statusBar'
import * as summary from './namespaces/summary'
import * as wireless from './namespaces/wireless'
import * as liveTracing from './namespaces/liveTracing'
import * as dashboard from './namespaces/dashboard'

export const ko = {
  common: common.ko,
  errors: errors.ko,
  app: app.ko,
  theme: theme.ko,
  localeSelector: localeSelector.ko,
  combobox: combobox.ko,
  cloud: cloud.ko,
  deviceSelector: deviceSelector.ko,
  deviceInfo: deviceInfo.ko,
  hoverOverlay: hoverOverlay.ko,
  iosReadiness: iosReadiness.ko,
  capture: capture.ko,
  sessionName: sessionName.ko,
  charts: charts.ko,
  metricCard: metricCard.ko,
  metricMenu: metricMenu.ko,
  packageSelector: packageSelector.ko,
  statusBar: statusBar.ko,
  summary: summary.ko,
  wireless: wireless.ko,
  liveTracing: liveTracing.ko,
  dashboard: dashboard.ko,
}

export type Messages = typeof ko

export const en: Messages = {
  common: common.en,
  errors: errors.en,
  app: app.en,
  theme: theme.en,
  localeSelector: localeSelector.en,
  combobox: combobox.en,
  cloud: cloud.en,
  deviceSelector: deviceSelector.en,
  deviceInfo: deviceInfo.en,
  hoverOverlay: hoverOverlay.en,
  iosReadiness: iosReadiness.en,
  capture: capture.en,
  sessionName: sessionName.en,
  charts: charts.en,
  metricCard: metricCard.en,
  metricMenu: metricMenu.en,
  packageSelector: packageSelector.en,
  statusBar: statusBar.en,
  summary: summary.en,
  wireless: wireless.en,
  liveTracing: liveTracing.en,
  dashboard: dashboard.en,
}

export const zh: Messages = {
  common: common.zh,
  errors: errors.zh,
  app: app.zh,
  theme: theme.zh,
  localeSelector: localeSelector.zh,
  combobox: combobox.zh,
  cloud: cloud.zh,
  deviceSelector: deviceSelector.zh,
  deviceInfo: deviceInfo.zh,
  hoverOverlay: hoverOverlay.zh,
  iosReadiness: iosReadiness.zh,
  capture: capture.zh,
  sessionName: sessionName.zh,
  charts: charts.zh,
  metricCard: metricCard.zh,
  metricMenu: metricMenu.zh,
  packageSelector: packageSelector.zh,
  statusBar: statusBar.zh,
  summary: summary.zh,
  wireless: wireless.zh,
  liveTracing: liveTracing.zh,
  dashboard: dashboard.zh,
}

export const DICTS: Record<Locale, Messages> = { ko, en, zh }
