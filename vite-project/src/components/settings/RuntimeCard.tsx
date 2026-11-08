import { useCallback, useEffect, useRef, useState } from 'react'
import {
  checkLiteLLM,
  getRuntimeJob,
  getRuntimeStatus,
  startRuntimeUpdate,
  type Release,
  type RuntimeJob,
  type RuntimeStatus,
} from '../../services/runtime'

const JOB_POLL_MS = 2000
const SCAN_POLL_MS = 4000

type Compat = { ok: boolean; reason: string } | 'checking'

interface RuntimeCardProps {
  /** 변경이 끝나면 모델 목록·카탈로그를 새로 받는다 (LiteLLM 버전이 바뀌면 고를 수 있는 모델도 바뀐다) */
  onChanged: () => Promise<unknown>
}

/**
 * 런타임 버전 — Ollama(로컬 모델 서버)와 LiteLLM(모델 호출 라이브러리)의 버전을 바꾼다.
 * 두 줄에서 원하는 버전을 고르고 '변경 적용'을 누르면 바뀐 것만 한 작업으로 적용한다.
 * LiteLLM은 백엔드가 쓰는 패키지라, 적용하는 동안 서버가 내려갔다가 새 콘솔 창에서 다시 뜬다.
 */
export default function RuntimeCard({ onChanged }: RuntimeCardProps) {
  const [status, setStatus] = useState<RuntimeStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selOllama, setSelOllama] = useState('')
  const [selLitellm, setSelLitellm] = useState('')
  const [compat, setCompat] = useState<Record<string, Compat>>({})
  const [job, setJob] = useState<RuntimeJob | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [offline, setOffline] = useState(false)   // LiteLLM 적용 중 백엔드가 내려가 있는 동안
  const [now, setNow] = useState(() => Date.now() / 1000)
  const logRef = useRef<HTMLPreElement>(null)

  const applyStatus = useCallback((s: RuntimeStatus) => {
    setStatus(s)
    setJob(s.job)
    setLoadError(null)
    setSelOllama((cur) => cur || s.ollama.current || '')
    setSelLitellm((cur) => cur || s.litellm.current)
  }, [])

  useEffect(() => {
    let alive = true
    getRuntimeStatus().then(
      (s) => { if (alive) applyStatus(s) },
      (e) => { if (alive) setLoadError(e instanceof Error ? e.message : '런타임 상태를 불러오지 못했습니다.') },
    )
    return () => { alive = false }
  }, [applyStatus])

  const active = job?.status === 'running' || job?.status === 'restarting'

  // 진행 중인 작업을 따라간다. 백엔드가 재시작되는 동안의 실패는 '재연결 대기'로 보고 계속 시도한다.
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(async () => {
      setNow(Date.now() / 1000)
      try {
        const next = await getRuntimeJob()
        setOffline(false)
        setJob(next)
        if (next && next.status !== 'running' && next.status !== 'restarting') {
          applyStatus(await getRuntimeStatus())
          setSelOllama(''); setSelLitellm('')
          await onChanged()
        }
      } catch {
        setOffline(true)
      }
    }, JOB_POLL_MS)
    return () => window.clearInterval(timer)
  }, [active, onChanged, applyStatus])

  // 최신 호환 LiteLLM 버전을 서버가 찾는 동안 주기적으로 결과를 받는다
  const scanning = !!status?.litellm.scanning
  useEffect(() => {
    if (!scanning || active) return
    const timer = window.setInterval(async () => {
      try { applyStatus(await getRuntimeStatus()) } catch { /* 다음 주기 */ }
    }, SCAN_POLL_MS)
    return () => window.clearInterval(timer)
  }, [scanning, active, applyStatus])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [job?.log.length])

  const ollama = status?.ollama
  const litellm = status?.litellm
  const litellmCompat = (v: string): Compat | null => {
    if (!litellm || v === litellm.current) return { ok: true, reason: '현재 버전' }
    if (compat[v]) return compat[v]
    const r = litellm.releases.find((x) => x.version === v)
    return r && r.compatible != null ? { ok: r.compatible, reason: r.compatReason ?? '' } : null
  }

  const pickLitellm = async (v: string) => {
    setSelLitellm(v)
    if (!litellm || litellmCompat(v) !== null) return
    setCompat((c) => ({ ...c, [v]: 'checking' }))
    try {
      const r = await checkLiteLLM(v)
      setCompat((c) => ({ ...c, [v]: { ok: r.ok, reason: r.reason } }))
    } catch (e) {
      setCompat((c) => ({ ...c, [v]: { ok: false, reason: e instanceof Error ? e.message : '확인 실패' } }))
    }
  }

  const ollamaTarget = ollama?.current && selOllama && selOllama !== ollama.current ? selOllama : null
  const litellmTarget = litellm && selLitellm && selLitellm !== litellm.current ? selLitellm : null
  const litellmState = litellmTarget ? litellmCompat(litellmTarget) : null
  const litellmBlocked = !!litellmTarget && (litellmState === 'checking' || (litellmState !== null && !litellmState.ok)
    || !litellm?.canRestart)

  const apply = async (targets: { ollama: string | null; litellm: string | null }) => {
    if (!ollama || !litellm || (!targets.ollama && !targets.litellm)) return
    const lines = []
    if (targets.ollama) lines.push(`· Ollama ${ollama.current} → ${targets.ollama}: 이미지를 빌드하고 컨테이너를 다시 시작합니다 (몇 분).`)
    if (targets.litellm) lines.push(`· LiteLLM ${litellm.current} → ${targets.litellm}: 백엔드 서버가 내려갔다가 새 콘솔 창에서 설치 후 다시 뜹니다.`)
    lines.push('· 진행 중에는 채팅을 쓸 수 없습니다. 실패하면 이전 버전으로 자동으로 되돌립니다.')
    if (!window.confirm(`런타임 버전을 바꿀까요?\n\n${lines.join('\n')}`)) return
    setStarting(true)
    setStartError(null)
    try {
      setJob(await startRuntimeUpdate({ ollama: targets.ollama ?? undefined, litellm: targets.litellm ?? undefined }))
      setNow(Date.now() / 1000)
    } catch (e) {
      setStartError(e instanceof Error ? e.message : '버전 변경을 시작하지 못했습니다.')
    } finally {
      setStarting(false)
    }
  }

  const bothUpdates = !!ollama?.updateAvailable && !!litellm?.updateAvailable
  const updateAll = () => {
    if (!ollama || !litellm) return
    const o = ollama.updateAvailable ? ollama.latest : null
    const l = litellm.updateAvailable ? litellm.latestCompatible : null
    if (o) setSelOllama(o)
    if (l) setSelLitellm(l)
    void apply({ ollama: o, litellm: l })
  }

  const elapsed = job ? Math.max(0, Math.round((job.finishedAt ?? now) - job.startedAt)) : 0

  return (
    <section className="settings__card">
      <div className="settings__card-head">
        <h2 className="settings__card-title">런타임 버전</h2>
        <p className="settings__card-desc">
          로컬 모델 서버(Ollama)와 모델 호출 라이브러리(LiteLLM)의 버전을 바꿉니다. 새 모델을 쓰려면 둘 중 하나를 올려야 할 때가 있습니다.
        </p>
      </div>

      {loadError && <p className="settings__error" role="alert">{loadError}</p>}
      {!status && !loadError && <p className="settings__empty">불러오는 중…</p>}

      {status && ollama && litellm && (
        <>
          {bothUpdates && !active && (
            <div className="runtime-banner">
              <span>Ollama와 LiteLLM 모두 새 버전이 있습니다.</span>
              <button className="btn btn--primary btn--sm" onClick={updateAll} disabled={starting}>한 번에 업데이트</button>
            </div>
          )}

          <div className="runtime-rows">
            <RuntimeRow
              name="Ollama"
              desc="로컬 모델 서버"
              current={ollama.current}
              badge={ollama.current
                ? ollama.updateAvailable ? { tone: 'info', text: `업데이트 있음 · ${ollama.latest}` }
                  : ollama.latest ? { tone: 'ok', text: '최신' } : null
                : { tone: 'muted', text: '연결 안 됨' }}
              detail={ollama.acceleration}
              releases={ollama.releases}
              releasesError={ollama.releasesError}
              selected={selOllama}
              onSelect={setSelOllama}
              disabled={active || starting || !ollama.current}
            />
            <RuntimeRow
              name="LiteLLM"
              desc="모델 호출·단가 계산"
              current={litellm.current}
              badge={litellm.updateAvailable ? { tone: 'info', text: `업데이트 있음 · ${litellm.latestCompatible}` }
                : litellm.scanning ? { tone: 'muted', text: '호환 버전 확인 중…' }
                  : { tone: 'ok', text: '최신 호환' }}
              detail={litellm.latest && litellm.latest !== litellm.latestCompatible && litellm.latest !== litellm.current
                ? `최신 ${litellm.latest}은 이 프로젝트와 호환되지 않습니다 (도구 호출 의존성 충돌).` : null}
              releases={litellm.releases}
              releasesError={litellm.releasesError}
              selected={selLitellm}
              onSelect={(v) => void pickLitellm(v)}
              disabled={active || starting}
              compatOf={litellmCompat}
            />
          </div>

          {litellmTarget && litellmState === 'checking' && (
            <p className="model-row__hint">LiteLLM {litellmTarget} 호환성을 확인하는 중입니다 (처음엔 수십 초 걸립니다).</p>
          )}
          {litellmTarget && litellmState && litellmState !== 'checking' && !litellmState.ok && (
            <p className="settings__error">LiteLLM {litellmTarget}은 쓸 수 없습니다: {litellmState.reason}</p>
          )}
          {litellmTarget && !litellm.canRestart && (
            <p className="settings__error">서버를 `uv run python main.py`로 실행해야 LiteLLM을 바꿀 수 있습니다 (자동 재시작 필요).</p>
          )}

          <div className="runtime-actions">
            <button
              className="btn btn--primary"
              onClick={() => void apply({ ollama: ollamaTarget, litellm: litellmTarget })}
              disabled={active || starting || (!ollamaTarget && !litellmTarget) || litellmBlocked}
            >
              {active ? '변경 중…' : starting ? '시작하는 중…'
                : ollamaTarget && litellmTarget ? '두 버전 함께 적용' : '변경 적용'}
            </button>
          </div>
          {startError && <p className="settings__error" role="alert">{startError}</p>}

          {job && (
            <div className={`upgrade-job upgrade-job--${job.status === 'restarting' ? 'running' : job.status}`} aria-live="polite">
              <div className="upgrade-job__head">
                <span className="upgrade-job__title">
                  {active && <span className="upgrade-job__spinner" aria-hidden />}
                  {active ? job.step : job.status === 'done' ? '완료' : '실패'}
                  {(['ollama', 'litellm'] as const).map((k) => job.items[k] && (
                    <span key={k} className={`job-chip job-chip--${job.items[k]!.status}`}>
                      {k === 'ollama' ? 'Ollama' : 'LiteLLM'} {job.items[k]!.from ? `${job.items[k]!.from} → ` : ''}{job.items[k]!.to}
                      {job.items[k]!.rolledBack ? ' (되돌림)' : ''}
                    </span>
                  ))}
                </span>
                <span className="upgrade-job__time">{elapsed}초</span>
              </div>
              {job.status === 'restarting' && (
                <p className="upgrade-job__message">
                  {offline ? '서버가 다시 시작되기를 기다리는 중입니다… ' : ''}
                  새로 열린 콘솔 창에서 LiteLLM을 설치한 뒤 서버를 다시 띄웁니다. 끝나면 이 화면이 자동으로 다시 연결됩니다.
                </p>
              )}
              {!active && job.message && <p className="upgrade-job__message">{job.message}</p>}
              <pre ref={logRef} className="upgrade-job__log">{job.log.join('\n')}</pre>
            </div>
          )}
        </>
      )}
    </section>
  )
}

interface RuntimeRowProps {
  name: string
  desc: string
  current: string | null
  badge: { tone: 'info' | 'ok' | 'muted'; text: string } | null
  detail: string | null
  releases: Release[]
  releasesError: string | null
  selected: string
  onSelect: (v: string) => void
  disabled: boolean
  compatOf?: (v: string) => Compat | null
}

function RuntimeRow({ name, desc, current, badge, detail, releases, releasesError, selected, onSelect, disabled, compatOf }: RuntimeRowProps) {
  const latest = releases[0]?.version
  return (
    <div className="runtime-row">
      <div className="runtime-row__info">
        <div className="runtime-row__name">
          {name} <span className="runtime-row__desc">{desc}</span>
        </div>
        <div className="runtime-row__current">
          {current ? <strong>{current}</strong> : <span className="ollama-info__muted">—</span>}
          {badge && <span className={`badge badge--${badge.tone}`}>{badge.text}</span>}
        </div>
        {detail && <div className="runtime-row__detail">{detail}</div>}
      </div>
      {releasesError ? (
        <p className="model-row__hint">{releasesError}</p>
      ) : (
        <select
          className="runtime-row__select"
          value={selected}
          onChange={(e) => onSelect(e.target.value)}
          disabled={disabled}
          aria-label={`${name} 버전`}
        >
          {current && !releases.some((r) => r.version === current) && <option value={current}>{current} (현재)</option>}
          {releases.map((r) => {
            const c = compatOf?.(r.version)
            const mark = !c || r.version === current ? '' : c === 'checking' ? ' · 확인 중' : c.ok ? ' · 호환' : ' · 비호환'
            return (
              <option key={r.version} value={r.version}>
                {r.version}
                {r.version === latest ? ' (최신)' : ''}
                {r.version === current ? ' (현재)' : ''}
                {mark}
                {r.publishedAt ? ` · ${r.publishedAt}` : ''}
              </option>
            )
          })}
        </select>
      )}
    </div>
  )
}
