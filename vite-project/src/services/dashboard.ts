import { apiFetch } from './http'

/**
 * 대시보드 API (GET /chatbot/dashboard/*).
 * 기간은 사용자의 로컬 날짜(YYYY-MM-DD, 양 끝 포함)와 브라우저 시간대로 보낸다.
 */

export interface DateRange {
  start: string   // YYYY-MM-DD
  end: string     // YYYY-MM-DD
}

export interface UsageModel {
  id: string
  label: string
  group: string
  /** 전체 모델 목록에서의 순서 — 차트 색을 모델마다 고정하는 데 쓴다 (null = 목록에 없는 모델) */
  order: number | null
}

export interface UsageRow {
  date: string
  model: string
  calls: number
  inputTokens: number
  cacheReadTokens: number
  outputTokens: number
  costUsd: number
}

export interface UsageResponse {
  range: { start: string; end: string; tz: string; days: string[] }
  models: UsageModel[]
  turns: number
  daily: UsageRow[]
}

export interface LawStat {
  lawName: string
  turns: number
  sessions: number
  lastViewedAt: string
  articles: { article: string; count: number }[]
}

export interface SessionDigest {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  messageCount: number
  isBranch: boolean
  firstQuestion: string
  summary: { overall: string | null; points: string[] } | null
  summaryUpdatedAt: string | null
}

const BASE = '/chatbot/dashboard'

function query(range: DateRange, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams({ ...range, tz: browserTimeZone(), ...extra })
  return params.toString()
}

export const fetchUsage = (range: DateRange) => apiFetch<UsageResponse>(`${BASE}/usage?${query(range)}`)
export const fetchLaws = (range: DateRange) => apiFetch<LawStat[]>(`${BASE}/laws?${query(range, { limit: '10' })}`)
export const fetchSessionDigests = (range: DateRange) =>
  apiFetch<SessionDigest[]>(`${BASE}/sessions?${query(range, { limit: '12' })}`)

// ────────────────────────── 날짜 ──────────────────────────
export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Seoul'
}

/** 로컬 날짜를 YYYY-MM-DD로 (toISOString은 UTC라 날짜가 하루 어긋날 수 있다) */
export function toDateString(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export type RangePreset = 'today' | '7d' | '30d' | 'month'

export function presetRange(preset: RangePreset, now: Date = new Date()): DateRange {
  const end = toDateString(now)
  const start = new Date(now)
  if (preset === '7d') start.setDate(now.getDate() - 6)
  else if (preset === '30d') start.setDate(now.getDate() - 29)
  else if (preset === 'month') start.setDate(1)
  return { start: toDateString(start), end }
}

// ────────────────────────── 숫자 ──────────────────────────
const compactFmt = new Intl.NumberFormat('ko-KR', { notation: 'compact', maximumFractionDigits: 1 })
const intFmt = new Intl.NumberFormat('ko-KR')

/** 큰 수는 축약 (1,284 → 1,284 / 12,900 → 1.3만) */
export const formatCompact = (n: number) => (Math.abs(n) < 10_000 ? intFmt.format(n) : compactFmt.format(n))
export const formatInt = (n: number) => intFmt.format(n)

/** 예상 비용 (USD). 1센트 미만도 0으로 보이지 않게 유효 자릿수를 늘린다. */
export function formatUsd(n: number): string {
  if (n === 0) return '$0'
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`
  if (Math.abs(n) < 1) return `$${n.toFixed(3)}`
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 법제처 국가법령정보센터 링크 (법령명 / 조문 경로) */
export function lawUrl(lawName: string, article?: string): string {
  const path = article ? `${lawName}/${article}` : lawName
  return `https://www.law.go.kr/법령/${path.split('/').map(encodeURIComponent).join('/')}`
}
