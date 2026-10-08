import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useSessions } from '../context/SessionsContext'
import UsageChart from '../components/dashboard/UsageChart'
import { OTHER, metricValue, seriesList, seriesOf, type UsageMetric } from '../components/dashboard/series'
import {
  fetchLaws, fetchSessionDigests, fetchUsage, formatCompact, formatInt, formatUsd, lawUrl, presetRange,
  type DateRange, type LawStat, type RangePreset, type SessionDigest, type UsageResponse,
} from '../services/dashboard'
import { formatWhen } from '../services/time'

/**
 * 메인 대시보드 (첫 진입 화면).
 *   - 기간 필터 한 줄이 아래 모든 섹션(사용량·법령·세션 요약)에 똑같이 적용된다.
 *   - 사용량은 그래프/표 두 보기를 제공한다 (값을 hover 없이도 확인할 수 있게).
 */

const EXAMPLES = [
  '전세금을 못 받고 있는데 어떻게 대응해야 하나요?',
  '근로기준법 제60조 연차휴가 규정 알려줘',
  '음주운전 처벌 기준이 어떻게 되나요?',
  '상가 임대차 계약 해지 시 주의할 점은?',
]

const PRESETS: { key: RangePreset | 'custom'; label: string }[] = [
  { key: 'today', label: '오늘' },
  { key: '7d', label: '최근 7일' },
  { key: '30d', label: '최근 30일' },
  { key: 'month', label: '이번 달' },
  { key: 'custom', label: '직접 선택' },
]

interface DashData {
  usage: UsageResponse
  laws: LawStat[]
  sessions: SessionDigest[]
}

export default function Dashboard() {
  const { user } = useAuth()
  const { createNew } = useSessions()
  const navigate = useNavigate()

  const [preset, setPreset] = useState<RangePreset | 'custom'>('30d')
  const [range, setRange] = useState<DateRange>(() => presetRange('30d'))
  const [data, setData] = useState<DashData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [metric, setMetric] = useState<UsageMetric>('cost')
  const [view, setView] = useState<'chart' | 'table'>('chart')

  // 기간이 바뀌면 세 섹션을 함께 다시 불러온다. 불러오는 동안 이전 화면을 흐리게 유지한다.
  useEffect(() => {
    if (range.start > range.end) return
    let alive = true
    Promise.all([fetchUsage(range), fetchLaws(range), fetchSessionDigests(range)]).then(
      ([usage, laws, sessions]) => {
        if (!alive) return
        setData({ usage, laws, sessions })
        setError(null)
        setLoading(false)
      },
      (e: unknown) => {
        if (!alive) return
        setError(e instanceof Error ? e.message : '대시보드를 불러오지 못했습니다.')
        setLoading(false)
      },
    )
    return () => { alive = false }
  }, [range])

  const choosePreset = (p: RangePreset | 'custom') => {
    setPreset(p)
    if (p !== 'custom') {
      setLoading(true)
      setRange(presetRange(p))
    }
  }

  const setCustom = (patch: Partial<DateRange>) => {
    const next = { ...range, ...patch }
    if (next.start && next.end && next.start <= next.end) {
      setLoading(true)
      setRange(next)
    }
  }

  const start = async (seed?: string) => {
    try {
      const s = await createNew()
      if (s) navigate(`/chat/${s.id}`, { state: seed ? { seed } : undefined })
    } catch (e) {
      alert(e instanceof Error ? e.message : '세션을 만들지 못했습니다.')
    }
  }

  const usage = data?.usage
  const series = useMemo(() => (usage ? seriesList(usage.models) : []), [usage])
  const totals = useMemo(() => {
    const t = { calls: 0, input: 0, cache: 0, output: 0, cost: 0 }
    for (const r of usage?.daily ?? []) {
      t.calls += r.calls
      t.input += r.inputTokens
      t.cache += r.cacheReadTokens
      t.output += r.outputTokens
      t.cost += r.costUsd
    }
    return t
  }, [usage])

  return (
    <div className="dash">
      <header className="dash__header">
        <div>
          <h1 className="dash__title">{user?.name}님의 상담 대시보드</h1>
          <p className="dash__desc">모델 사용량과 예상 비용, 자주 찾아본 법령, 세션별 상담 요약을 한눈에 확인하세요.</p>
        </div>
        <button className="btn btn--primary" onClick={() => start()}>
          <span aria-hidden>＋</span> 새 상담 시작
        </button>
      </header>

      <div className="dash__quick" aria-label="예시 질문으로 바로 시작">
        {EXAMPLES.map((q) => (
          <button key={q} className="dash__chip" onClick={() => start(q)}>{q}</button>
        ))}
      </div>

      {/* 기간 필터 — 아래 모든 섹션에 함께 적용 */}
      <div className="dash__filters">
        <div className="segmented" role="group" aria-label="조회 기간">
          {PRESETS.map((p) => (
            <button
              key={p.key}
              className={`segmented__item${preset === p.key ? ' is-active' : ''}`}
              aria-pressed={preset === p.key}
              onClick={() => choosePreset(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
        {preset === 'custom' ? (
          <div className="dash__dates">
            <input type="date" className="dash__date" value={range.start} max={range.end}
              aria-label="시작일" onChange={(e) => setCustom({ start: e.target.value })} />
            <span aria-hidden>~</span>
            <input type="date" className="dash__date" value={range.end} min={range.start}
              aria-label="종료일" onChange={(e) => setCustom({ end: e.target.value })} />
          </div>
        ) : (
          <span className="dash__range">{range.start} ~ {range.end}</span>
        )}
      </div>

      {error && <div className="dash__error" role="alert">{error}</div>}

      {!data && loading && <div className="dash__loading">불러오는 중…</div>}

      {data && usage && (
        <div className={`dash__body${loading ? ' is-loading' : ''}`} aria-busy={loading}>
          {/* 요약 수치 */}
          <section className="dash__stats" aria-label="기간 요약">
            <StatTile label="예상 비용" value={formatUsd(totals.cost)} sub="호출 시점 단가 기준 (USD)" />
            <StatTile
              label="총 토큰"
              value={formatCompact(totals.input + totals.output)}
              sub={`입력 ${formatCompact(totals.input)} · 출력 ${formatCompact(totals.output)} · 캐시 ${formatCompact(totals.cache)}`}
            />
            <StatTile label="질문 수" value={formatInt(usage.turns)} sub="답변을 받은 질문 (재생성 포함)" />
            <StatTile label="LLM 호출" value={formatInt(totals.calls)} sub="검색·요약 등 내부 호출 포함" />
          </section>

          {/* 모델별 사용량 */}
          <section className="dash__card">
            <div className="dash__card-head">
              <div>
                <h2 className="dash__card-title">모델별 {metric === 'cost' ? '예상 비용' : '토큰 사용량'}</h2>
                <p className="dash__card-desc">
                  {usage.range.days.length > 62 ? '주 단위' : '일 단위'} 합계 · 토큰 = 입력(캐시 포함) + 출력
                </p>
              </div>
              <div className="dash__toggles">
                <div className="segmented segmented--sm" role="group" aria-label="지표">
                  <button className={`segmented__item${metric === 'cost' ? ' is-active' : ''}`}
                    aria-pressed={metric === 'cost'} onClick={() => setMetric('cost')}>비용</button>
                  <button className={`segmented__item${metric === 'tokens' ? ' is-active' : ''}`}
                    aria-pressed={metric === 'tokens'} onClick={() => setMetric('tokens')}>토큰</button>
                </div>
                <div className="segmented segmented--sm" role="group" aria-label="보기 방식">
                  <button className={`segmented__item${view === 'chart' ? ' is-active' : ''}`}
                    aria-pressed={view === 'chart'} onClick={() => setView('chart')}>그래프</button>
                  <button className={`segmented__item${view === 'table' ? ' is-active' : ''}`}
                    aria-pressed={view === 'table'} onClick={() => setView('table')}>표</button>
                </div>
              </div>
            </div>

            {usage.daily.length === 0 ? (
              <p className="dash__empty">선택한 기간에 사용 기록이 없습니다.</p>
            ) : view === 'chart' ? (
              <>
                <ul className="legend" aria-label="범례">
                  {series.map((s) => (
                    <li key={s.key} className="legend__item">
                      <span className="legend__swatch" style={{ background: s.color }} aria-hidden />
                      {s.label}
                    </li>
                  ))}
                </ul>
                <UsageChart days={usage.range.days} rows={usage.daily} models={usage.models}
                  metric={metric} series={series} />
              </>
            ) : (
              <UsageTables usage={usage} metric={metric} />
            )}
          </section>

          <div className="dash__grid">
            {/* 자주 조회한 법령 */}
            <section className="dash__card">
              <div className="dash__card-head">
                <div>
                  <h2 className="dash__card-title">자주 조회한 법령</h2>
                  <p className="dash__card-desc">답변 근거로 실제 조회된 법령 · 조회한 질문 수 기준</p>
                </div>
              </div>
              {data.laws.length === 0 ? (
                <p className="dash__empty">선택한 기간에 조회된 법령이 없습니다.</p>
              ) : (
                <ol className="law-rank">
                  {data.laws.map((law, i) => (
                    <li key={law.lawName} className="law-rank__item">
                      <span className="law-rank__no">{i + 1}</span>
                      <div className="law-rank__body">
                        <div className="law-rank__top">
                          <a className="law-rank__name" href={lawUrl(law.lawName)} target="_blank" rel="noreferrer">
                            {law.lawName}
                          </a>
                          <span className="law-rank__count">질문 {law.turns}건</span>
                        </div>
                        <div className="law-rank__bar" aria-hidden>
                          <span style={{ width: `${(law.turns / data.laws[0].turns) * 100}%` }} />
                        </div>
                        <div className="law-rank__meta">
                          {law.articles.map((a) => (
                            <a key={a.article} className="law-rank__article" href={lawUrl(law.lawName, a.article)}
                              target="_blank" rel="noreferrer" title={`${a.count}회 조회`}>
                              {a.article}
                            </a>
                          ))}
                          <span className="law-rank__when">세션 {law.sessions}개 · {formatWhen(law.lastViewedAt)}</span>
                        </div>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            {/* 세션 요약 */}
            <section className="dash__card">
              <div className="dash__card-head">
                <div>
                  <h2 className="dash__card-title">세션 요약</h2>
                  <p className="dash__card-desc">선택한 기간에 활동한 세션 · 최근 활동순</p>
                </div>
                <Link className="dash__more" to="/history">전체 대화 내역 →</Link>
              </div>
              {data.sessions.length === 0 ? (
                <p className="dash__empty">선택한 기간에 활동한 세션이 없습니다.</p>
              ) : (
                <div className="digest-list">
                  {data.sessions.map((s) => <SessionDigestCard key={s.id} s={s} />)}
                </div>
              )}
            </section>
          </div>
        </div>
      )}
    </div>
  )
}

function StatTile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="stat">
      <div className="stat__label">{label}</div>
      <div className="stat__value">{value}</div>
      <div className="stat__sub">{sub}</div>
    </div>
  )
}

function SessionDigestCard({ s }: { s: SessionDigest }) {
  const points = s.summary?.points ?? []
  return (
    <Link to={`/chat/${s.id}`} className="digest">
      <div className="digest__top">
        <h3 className="digest__title">
          {s.isBranch && <span className="summary-card__branch" title="브랜치로 만들어진 세션">⑂ 분기</span>}
          {s.title}
        </h3>
        <span className="digest__when">{formatWhen(s.updatedAt)}</span>
      </div>
      {s.summary?.overall ? (
        <p className="digest__overall">{s.summary.overall}</p>
      ) : (
        <p className="digest__overall digest__overall--pending">
          {s.messageCount > 0 ? '요약을 만드는 중입니다 (잠시 후 새로고침하면 표시됩니다). ' : ''}
          {s.firstQuestion ? `첫 질문: ${s.firstQuestion}` : '아직 대화가 없습니다.'}
        </p>
      )}
      {points.length > 0 && (
        <ul className="digest__points">
          {points.slice(0, 3).map((p) => <li key={p}>{p}</li>)}
        </ul>
      )}
      <div className="digest__foot">메시지 {s.messageCount}개</div>
    </Link>
  )
}

/** 표 보기: 모델별 합계 + 일별 상세 (그래프의 모든 값을 hover 없이 확인) */
function UsageTables({ usage, metric }: { usage: UsageResponse; metric: UsageMetric }) {
  const byModel = useMemo(() => {
    const m = new Map<string, { calls: number; input: number; cache: number; output: number; cost: number }>()
    for (const r of usage.daily) {
      const t = m.get(r.model) ?? { calls: 0, input: 0, cache: 0, output: 0, cost: 0 }
      t.calls += r.calls; t.input += r.inputTokens; t.cache += r.cacheReadTokens
      t.output += r.outputTokens; t.cost += r.costUsd
      m.set(r.model, t)
    }
    return m
  }, [usage])
  const totalCost = [...byModel.values()].reduce((a, t) => a + t.cost, 0)
  const totalTokens = [...byModel.values()].reduce((a, t) => a + t.input + t.output, 0)

  const series = seriesList(usage.models)
  // 표는 정확한 값을 읽는 곳이라 축약하지 않는다. 0은 '–'로 표시해 값이 있는 칸이 눈에 띄게 한다.
  const exact = (v: number) => (v === 0 ? '–' : metric === 'cost' ? formatUsd(v) : formatInt(v))
  const modelSeries = new Map(usage.models.map((m) => [m.id, seriesOf(m)]))
  const days = usage.range.days.filter((d) => usage.daily.some((r) => r.date === d)).reverse()

  return (
    <div className="usage-tables">
      <div className="table-wrap">
        <table className="data-table">
          <caption>모델별 합계</caption>
          <thead>
            <tr><th>모델</th><th>호출</th><th>입력 토큰</th><th>캐시 읽기</th><th>출력 토큰</th><th>예상 비용</th><th>비중</th></tr>
          </thead>
          <tbody>
            {usage.models.map((m) => {
              const t = byModel.get(m.id)
              if (!t) return null
              const share = metric === 'cost'
                ? (totalCost ? t.cost / totalCost : 0)
                : (totalTokens ? (t.input + t.output) / totalTokens : 0)
              return (
                <tr key={m.id}>
                  <th scope="row">
                    <span className="legend__swatch" style={{ background: seriesOf(m).color }} aria-hidden />
                    {m.label}
                  </th>
                  <td>{formatInt(t.calls)}</td>
                  <td>{formatInt(t.input)}</td>
                  <td>{formatInt(t.cache)}</td>
                  <td>{formatInt(t.output)}</td>
                  <td>{formatUsd(t.cost)}</td>
                  <td>{(share * 100).toFixed(1)}%</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="table-wrap">
        <table className="data-table">
          <caption>일별 {metric === 'cost' ? '예상 비용' : '토큰'} (기록이 있는 날만, 최신순)</caption>
          <thead>
            <tr><th>날짜</th>{series.map((s) => <th key={s.key}>{s.label}</th>)}<th>합계</th></tr>
          </thead>
          <tbody>
            {days.map((d) => {
              const rows = usage.daily.filter((r) => r.date === d)
              const val = (key: string) => rows
                .filter((r) => (modelSeries.get(r.model)?.key ?? OTHER.key) === key)
                .reduce((a, r) => a + metricValue(r, metric), 0)
              const total = rows.reduce((a, r) => a + metricValue(r, metric), 0)
              return (
                <tr key={d}>
                  <th scope="row">{d}</th>
                  {series.map((s) => <td key={s.key}>{exact(val(s.key))}</td>)}
                  <td><strong>{exact(total)}</strong></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
