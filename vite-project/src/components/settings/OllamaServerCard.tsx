import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getOllamaStatus,
  getUpgradeJob,
  startOllamaUpgrade,
  type OllamaStatus,
  type UpgradeJob,
} from '../../services/ollama'

/** 버전 변경 진행 상태 조회 주기 */
const JOB_POLL_MS = 2000

interface OllamaServerCardProps {
  /** 버전 변경이 끝나면 모델 목록(설치 상태)을 새로 받는다 */
  onChanged: () => Promise<unknown>
}

/**
 * 로컬 모델 서버(Ollama) 버전 변경.
 * 최신 모델 중에는 새 Ollama가 필요한 것이 있어서, 서버의 Ollama 이미지를 새 버전으로 빌드하고 컨테이너를 다시 띄운다.
 * 실패하면 서버가 이전 버전으로 되돌린다. 받아 둔 모델은 그대로 남는다.
 */
export default function OllamaServerCard({ onChanged }: OllamaServerCardProps) {
  const [status, setStatus] = useState<OllamaStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selected, setSelected] = useState('')
  const [job, setJob] = useState<UpgradeJob | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  // 경과 시간 표시용 현재 시각(초). 렌더 중에 Date.now()를 부르지 않도록 조회 주기마다 갱신한다
  const [now, setNow] = useState(() => Date.now() / 1000)
  const logRef = useRef<HTMLPreElement>(null)

  const applyStatus = useCallback((s: OllamaStatus) => {
    setStatus(s)
    setJob(s.job)
    setLoadError(null)
    // 기본 선택: 업데이트가 있으면 최신, 아니면 지금 버전
    setSelected((cur) => cur || (s.updateAvailable && s.latestVersion ? s.latestVersion : s.runningVersion ?? ''))
  }, [])

  useEffect(() => {
    let alive = true
    getOllamaStatus().then(
      (s) => { if (alive) applyStatus(s) },
      (e) => { if (alive) setLoadError(e instanceof Error ? e.message : 'Ollama 상태를 불러오지 못했습니다.') },
    )
    return () => { alive = false }
  }, [applyStatus])

  const running = job?.status === 'running'

  // 변경 작업이 진행 중이면 주기적으로 상태를 받고, 끝나면 버전·모델 목록을 새로 받는다
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(async () => {
      try {
        const next = await getUpgradeJob()
        setJob(next)
        setNow(Date.now() / 1000)
        if (next && next.status !== 'running') {
          applyStatus(await getOllamaStatus())
          await onChanged()
        }
      } catch {
        /* 컨테이너 재시작 중 일시 오류는 다음 주기에 다시 시도 */
      }
    }, JOB_POLL_MS)
    return () => window.clearInterval(timer)
  }, [running, onChanged, applyStatus])

  // 새 로그 줄이 오면 맨 아래로 스크롤
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [job?.log.length])

  const upgrade = async () => {
    if (!status || !selected) return
    const from = status.runningVersion ?? status.configuredVersion
    const ok = window.confirm(
      `Ollama를 ${from} → ${selected}로 바꿀까요?\n\n` +
      '· 이미지를 새로 빌드하고 컨테이너를 다시 시작합니다 (몇 분 걸릴 수 있습니다).\n' +
      '· 그동안 로컬 모델로 대화할 수 없습니다.\n' +
      '· 실패하면 이전 버전으로 자동으로 되돌립니다. 받아 둔 모델은 그대로 남습니다.',
    )
    if (!ok) return
    setStarting(true)
    setStartError(null)
    try {
      setJob(await startOllamaUpgrade(selected))
      setNow(Date.now() / 1000)
    } catch (e) {
      setStartError(e instanceof Error ? e.message : '버전 변경을 시작하지 못했습니다.')
    } finally {
      setStarting(false)
    }
  }

  const current = status?.runningVersion
  const sameAsCurrent = !!current && selected === current && status?.configuredVersion === current
  const elapsed = job ? Math.max(0, Math.round((job.finishedAt ?? now) - job.startedAt)) : 0

  return (
    <section className="settings__card">
      <div className="settings__card-head">
        <h2 className="settings__card-title">로컬 모델 서버 (Ollama)</h2>
        <p className="settings__card-desc">
          로컬 모델을 실행하는 Ollama의 버전을 바꿉니다. 최신 모델 중에는 새 버전의 Ollama가 필요한 것이 있습니다.
        </p>
      </div>

      {loadError && <p className="settings__error" role="alert">{loadError}</p>}
      {!status && !loadError && <p className="settings__empty">불러오는 중…</p>}

      {status && (
        <>
          <dl className="ollama-info">
            <div>
              <dt>현재 버전</dt>
              <dd>
                {current ? <strong>{current}</strong> : <span className="ollama-info__muted">연결할 수 없음</span>}
                {current && status.updateAvailable && <span className="badge badge--info">업데이트 있음 · {status.latestVersion}</span>}
                {current && !status.updateAvailable && status.latestVersion && <span className="badge badge--ok">최신</span>}
                {current && status.configuredVersion !== current && (
                  <span className="badge badge--warn" title="컨테이너를 다시 만들면 설정 버전으로 바뀝니다">설정 {status.configuredVersion}</span>
                )}
              </dd>
            </div>
            {status.acceleration && (
              <div>
                <dt>가속</dt>
                <dd className={status.accelerated ? '' : 'ollama-info__muted'}>{status.acceleration}</dd>
              </div>
            )}
          </dl>

          {status.releasesError ? (
            <p className="model-row__hint">{status.releasesError}</p>
          ) : (
            <div className="ollama-upgrade">
              <select
                className="ollama-upgrade__select"
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
                disabled={running || starting}
                aria-label="바꿀 Ollama 버전"
              >
                {current && !status.releases.some((r) => r.version === current) && (
                  <option value={current}>{current} (현재)</option>
                )}
                {status.releases.map((r, i) => (
                  <option key={r.version} value={r.version}>
                    {r.version}
                    {i === 0 ? ' (최신)' : ''}
                    {r.version === current ? ' (현재)' : ''}
                    {r.publishedAt ? ` · ${r.publishedAt}` : ''}
                  </option>
                ))}
              </select>
              <button
                className="btn btn--primary"
                onClick={upgrade}
                disabled={running || starting || !selected || sameAsCurrent || !current}
                title={sameAsCurrent ? '지금 쓰고 있는 버전입니다' : undefined}
              >
                {running ? '변경 중…' : starting ? '시작하는 중…' : '버전 변경'}
              </button>
            </div>
          )}
          {startError && <p className="settings__error" role="alert">{startError}</p>}

          {job && (
            <div className={`upgrade-job upgrade-job--${job.status}`} aria-live="polite">
              <div className="upgrade-job__head">
                <span className="upgrade-job__title">
                  {job.status === 'running' && <span className="upgrade-job__spinner" aria-hidden />}
                  {job.status === 'running'
                    ? `${job.previous} → ${job.target} · ${job.step}`
                    : job.status === 'done'
                      ? `완료 — Ollama ${job.target}`
                      : `실패${job.rolledBack ? ` — ${job.previous}로 되돌렸습니다` : ''}`}
                </span>
                <span className="upgrade-job__time">{elapsed}초</span>
              </div>
              {job.status !== 'running' && job.message && <p className="upgrade-job__message">{job.message}</p>}
              <pre ref={logRef} className="upgrade-job__log">{job.log.join('\n')}</pre>
            </div>
          )}
        </>
      )}
    </section>
  )
}
