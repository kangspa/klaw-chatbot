import { useEffect, useMemo, useRef, useState } from 'react'
import type { UsageModel, UsageRow } from '../../services/dashboard'
import { OTHER, formatMetric, metricValue, seriesOf, type Series, type UsageMetric } from './series'

/**
 * 기간별 모델 사용량 누적 막대 그래프 (SVG).
 * - 색은 모델의 전체 목록 순서로 고정한다 (기간·필터가 바뀌어도 같은 모델은 같은 색).
 *   검증된 8색 범주 팔레트를 쓰고, 9번째 이후(로컬 모델 등)는 '기타'로 묶는다.
 * - 막대 ≤24px, 맨 위 4px 둥근 끝, 구간 사이 2px 간격, 가는 실선 격자.
 * - 날짜 기둥마다 hover/focus 툴팁 (값이 먼저, 모델 이름이 뒤).
 */

interface Bucket {
  label: string      // 축 라벨 (10/8 또는 10/1~)
  title: string      // 툴팁 제목 (2026-10-08 또는 2026-10-01 ~ 10-07)
  values: Map<string, number>
  total: number
}

/** 일 단위로 묶되, 기간이 길면(62일 초과) 주 단위로 묶는다 */
function buildBuckets(days: string[], rows: UsageRow[], models: UsageModel[], metric: UsageMetric): Bucket[] {
  const modelSeries = new Map(models.map((m) => [m.id, seriesOf(m)]))
  const byDay = new Map<string, Map<string, number>>()
  for (const r of rows) {
    const key = modelSeries.get(r.model)?.key ?? OTHER.key
    const m = byDay.get(r.date) ?? new Map<string, number>()
    m.set(key, (m.get(key) ?? 0) + metricValue(r, metric))
    byDay.set(r.date, m)
  }
  const size = days.length > 62 ? 7 : 1
  const out: Bucket[] = []
  for (let i = 0; i < days.length; i += size) {
    const span = days.slice(i, i + size)
    const values = new Map<string, number>()
    for (const d of span) {
      for (const [k, v] of byDay.get(d) ?? []) values.set(k, (values.get(k) ?? 0) + v)
    }
    const [y, mo, da] = span[0].split('-').map(Number)
    const last = span[span.length - 1]
    out.push({
      label: size === 1 ? `${mo}/${da}` : `${mo}/${da}~`,
      title: size === 1 ? `${y}년 ${mo}월 ${da}일` : `${span[0]} ~ ${last.slice(5)}`,
      values,
      total: [...values.values()].reduce((a, b) => a + b, 0),
    })
  }
  return out
}

/** 1·2·2.5·5 × 10^k 단위의 보기 좋은 눈금 */
function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0]
  const raw = max / count
  const pow = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? raw
  const ticks: number[] = []
  for (let v = 0; v <= max + step * 0.0001; v += step) ticks.push(v)
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step)
  return ticks
}

const HEIGHT = 260
const M = { top: 12, right: 8, bottom: 28, left: 64 }
const GAP = 2
const RADIUS = 4

interface Props {
  days: string[]
  rows: UsageRow[]
  models: UsageModel[]
  metric: UsageMetric
  series: Series[]   // 범례 순서 = 쌓는 순서 (아래부터)
}

export default function UsageChart({ days, rows, models, metric, series }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(640)
  const [hover, setHover] = useState<{ index: number; x: number } | null>(null)

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(320, entry.contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const buckets = useMemo(() => buildBuckets(days, rows, models, metric), [days, rows, models, metric])
  const ticks = niceTicks(Math.max(...buckets.map((b) => b.total), 0))
  const yMax = ticks[ticks.length - 1] || 1

  const plotW = width - M.left - M.right
  const plotH = HEIGHT - M.top - M.bottom
  const band = plotW / Math.max(buckets.length, 1)
  const barW = Math.max(2, Math.min(24, band * 0.62))
  const y = (v: number) => M.top + plotH - (v / yMax) * plotH
  const labelEvery = Math.max(1, Math.ceil(buckets.length / Math.max(1, Math.floor(plotW / 52))))

  const active = hover ? buckets[hover.index] : null

  return (
    <div className="usage-chart" ref={wrapRef}>
      <svg width={width} height={HEIGHT} role="img" aria-label="기간별 모델 사용량 누적 막대 그래프">
        {/* 격자 + y축 눈금 */}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={M.left} x2={width - M.right} y1={y(t)} y2={y(t)} className="usage-chart__grid" />
            <text x={M.left - 8} y={y(t)} className="usage-chart__tick" textAnchor="end" dominantBaseline="middle">
              {formatMetric(t, metric)}
            </text>
          </g>
        ))}

        {buckets.map((b, i) => {
          const x = M.left + i * band + (band - barW) / 2
          let acc = 0
          const present = series.filter((s) => (b.values.get(s.key) ?? 0) > 0)
          return (
            <g key={i}>
              {present.map((s, si) => {
                const v = b.values.get(s.key) ?? 0
                const y0 = y(acc)
                acc += v
                const isTop = si === present.length - 1
                // 위에 다른 구간이 있으면 2px 간격을 남긴다
                const y1 = y(acc) + (isTop ? 0 : GAP)
                const h = y0 - y1
                if (h <= 0.5) return null
                if (!isTop) {
                  return <rect key={s.key} x={x} y={y1} width={barW} height={h} fill={s.color} />
                }
                const r = Math.min(RADIUS, h, barW / 2)
                // 맨 위 구간만 위쪽 모서리를 둥글게, 바닥 쪽은 각지게
                const d = `M${x},${y0} V${y1 + r} Q${x},${y1} ${x + r},${y1} H${x + barW - r} Q${x + barW},${y1} ${x + barW},${y1 + r} V${y0} Z`
                return <path key={s.key} d={d} fill={s.color} />
              })}
              {i % labelEvery === 0 && (
                <text x={M.left + i * band + band / 2} y={HEIGHT - 8} className="usage-chart__tick" textAnchor="middle">
                  {b.label}
                </text>
              )}
              {/* 기둥 전체가 hover/focus 대상 (막대보다 넓게) */}
              <rect
                x={M.left + i * band}
                y={M.top}
                width={band}
                height={plotH}
                className={`usage-chart__hit${hover?.index === i ? ' is-active' : ''}`}
                tabIndex={0}
                aria-label={`${b.title} 합계 ${formatMetric(b.total, metric)}`}
                onPointerEnter={() => setHover({ index: i, x: M.left + i * band + band / 2 })}
                onPointerLeave={() => setHover(null)}
                onFocus={() => setHover({ index: i, x: M.left + i * band + band / 2 })}
                onBlur={() => setHover(null)}
              />
            </g>
          )
        })}

        <line x1={M.left} x2={width - M.right} y1={y(0)} y2={y(0)} className="usage-chart__axis" />
      </svg>

      {active && hover && (
        <div
          className="usage-chart__tooltip"
          style={{ left: Math.min(Math.max(hover.x, 90), width - 90), top: M.top }}
          role="status"
        >
          <div className="usage-chart__tooltip-title">{active.title}</div>
          <div className="usage-chart__tooltip-total">{formatMetric(active.total, metric)}</div>
          {series
            .filter((s) => (active.values.get(s.key) ?? 0) > 0)
            .reverse()
            .map((s) => (
              <div key={s.key} className="usage-chart__tooltip-row">
                <span className="usage-chart__key" style={{ background: s.color }} aria-hidden />
                <strong>{formatMetric(active.values.get(s.key) ?? 0, metric)}</strong>
                <span>{s.label}</span>
              </div>
            ))}
          {active.total === 0 && <div className="usage-chart__tooltip-empty">사용 기록 없음</div>}
        </div>
      )}
    </div>
  )
}
