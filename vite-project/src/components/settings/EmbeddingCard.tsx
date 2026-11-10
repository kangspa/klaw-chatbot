import { useCallback, useEffect, useState } from 'react'
import { formatBytes } from '../../services/models'
import {
  deleteUnusedEmbeddings,
  getEmbeddingStatus,
  prepareEmbeddingModel,
  reindexEmbeddings,
  selectEmbeddingProvider,
  type EmbeddingProvider,
  type EmbeddingProviderId,
  type EmbeddingStatus,
} from '../../services/embeddings'

const POLL_MS = 2000

/**
 * 대화 기록 검색(임베딩) 방식 — 내장(CPU) · Ollama Qwen3 Embedding · Gemini API 중 하나를 고른다.
 * 바꾸면 서버가 저장된 대화 원문으로 새 벡터를 백그라운드에서 다시 만든다. 그동안 채팅은 그대로 쓸 수 있다.
 * 이전 방식의 벡터는 자동으로 지우지 않는다 — '안 쓰는 벡터 삭제'로 비운다.
 */
export default function EmbeddingCard() {
  const [status, setStatus] = useState<EmbeddingStatus | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setStatus(await getEmbeddingStatus())
      setLoadError(null)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : '임베딩 상태를 불러오지 못했습니다.')
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const job = status?.reindex ?? null
  const jobActive = job?.status === 'preparing' || job?.status === 'indexing'
  const anyPreparing = !!status?.providers.some((p) => p.preparing)

  // 재인덱싱·모델 받기가 진행 중이면 상태를 계속 받는다
  useEffect(() => {
    if (!jobActive && !anyPreparing) return
    const timer = window.setInterval(() => { void refresh() }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [jobActive, anyPreparing, refresh])

  const run = async (fn: () => Promise<EmbeddingStatus>, done?: string) => {
    setBusy(true)
    setActionError(null)
    setNotice(null)
    try {
      setStatus(await fn())
      if (done) setNotice(done)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : '요청을 처리하지 못했습니다.')
    } finally {
      setBusy(false)
    }
  }

  const choose = (p: EmbeddingProvider) => {
    if (!status || p.id === status.active || p.problem) return
    const turns = status.turns ?? 0
    const lines = [
      `대화 기록 검색 방식을 '${p.label}'(으)로 바꿀까요?`,
      '',
      `· 저장된 대화 ${turns}턴을 새 방식으로 다시 임베딩합니다 (백그라운드, 그동안 채팅은 그대로 됩니다).`,
      !p.downloaded && p.approxBytes ? `· 먼저 모델을 받습니다 (약 ${formatBytes(p.approxBytes)}, 앱 데이터 폴더에만 저장).` : null,
      p.id === 'gemini' ? '· 주의: 질문·답변 원문이 Google로 전송되고, 임베딩 API 요금이 청구될 수 있습니다.' : null,
      '· 이전 방식의 벡터는 남겨 둡니다. 다시 돌아오면 빠진 대화만 채웁니다.',
    ].filter((x) => x !== null)
    if (!window.confirm(lines.join('\n'))) return
    void run(() => selectEmbeddingProvider(p.id))
  }

  const active = status?.providers.find((p) => p.id === status.active) ?? null
  const unused = status?.providers.filter((p) => p.id !== status.active && p.collection.exists) ?? []
  const unusedBytes = unused.reduce((s, p) => s + p.collection.bytes, 0)

  const removeUnused = () => {
    if (!unused.length) return
    const what = unused.map((p) => `· ${p.label}: ${p.collection.points}개 (${formatBytes(p.collection.bytes)})`).join('\n')
    if (!window.confirm(`지금 쓰지 않는 방식의 벡터를 지울까요?\n\n${what}\n\n대화 원문은 지워지지 않습니다. 나중에 그 방식으로 돌아가면 다시 만듭니다.`)) return
    void run(async () => {
      const r = await deleteUnusedEmbeddings()
      setNotice(`안 쓰는 벡터 ${r.deleted.length}개 컬렉션을 지웠습니다 (${formatBytes(r.freedBytes)}).`)
      return r
    })
  }

  return (
    <section className="settings__card">
      <div className="settings__card-head">
        <h2 className="settings__card-title">대화 기록 검색 (임베딩)</h2>
        <p className="settings__card-desc">
          AI가 예전 대화를 찾아볼 때 쓰는 임베딩 모델을 고릅니다. 바꾸면 저장된 대화로 검색 색인을 다시 만듭니다.
        </p>
      </div>

      {loadError && <p className="settings__error" role="alert">{loadError}</p>}
      {!status && !loadError && <p className="settings__empty">불러오는 중…</p>}

      {status && (
        <>
          <div className="embed-options" role="radiogroup" aria-label="임베딩 방식">
            {status.providers.map((p) => (
              <ProviderOption
                key={p.id}
                provider={p}
                checked={p.id === status.active}
                disabled={busy || (!!p.problem && p.id !== status.active)}
                onChoose={() => choose(p)}
                onPrepare={() => void run(() => prepareEmbeddingModel(p.id as EmbeddingProviderId))}
              />
            ))}
          </div>

          {job && (
            <div className={`upgrade-job upgrade-job--${jobActive ? 'running' : job.status === 'done' ? 'done' : 'error'}`} aria-live="polite">
              <div className="upgrade-job__head">
                <span className="upgrade-job__title">
                  {jobActive && <span className="upgrade-job__spinner" aria-hidden />}
                  {jobActive ? (job.status === 'preparing' ? '임베딩 모델 준비 중' : '검색 색인 만드는 중')
                    : job.status === 'done' ? '검색 색인 최신' : job.status === 'cancelled' ? '멈춤' : '실패'}
                  <span className="job-chip">{status.providers.find((p) => p.id === job.provider)?.label ?? job.provider}</span>
                </span>
                {job.total > 0 && <span className="upgrade-job__time">{job.done} / {job.total}턴</span>}
              </div>
              {job.status === 'indexing' && job.total > 0 && (
                <div className="pull-progress">
                  <div className="pull-progress__bar"><span style={{ width: `${job.percent ?? 0}%` }} /></div>
                </div>
              )}
              {job.message && <p className="upgrade-job__message">{job.message}</p>}
            </div>
          )}

          <div className="runtime-actions">
            <button className="btn btn--sm" onClick={() => void run(() => reindexEmbeddings(false))}
              disabled={busy || jobActive || !!active?.problem}
              title="빠졌거나 내용이 바뀐 대화만 다시 임베딩합니다">
              색인 다시 맞추기
            </button>
            <button className="btn btn--sm" onClick={() => {
              if (window.confirm(`저장된 대화 ${status.turns ?? 0}턴을 모두 다시 임베딩할까요?`)) void run(() => reindexEmbeddings(true))
            }} disabled={busy || jobActive || !!active?.problem}>
              전부 다시 만들기
            </button>
            <button className="btn btn--sm btn--danger-outline" onClick={removeUnused} disabled={busy || !unused.length}>
              안 쓰는 벡터 삭제{unused.length ? ` (${formatBytes(unusedBytes)})` : ''}
            </button>
          </div>
          {actionError && <p className="settings__error" role="alert">{actionError}</p>}
          {notice && <p className="model-row__hint">{notice}</p>}
        </>
      )}
    </section>
  )
}

interface ProviderOptionProps {
  provider: EmbeddingProvider
  checked: boolean
  disabled: boolean
  onChoose: () => void
  onPrepare: () => void
}

function ProviderOption({ provider: p, checked, disabled, onChoose, onPrepare }: ProviderOptionProps) {
  const needsDownload = p.approxBytes > 0
  let state: { tone: 'ok' | 'warn' | 'muted' | 'info'; text: string }
  if (p.problem) state = { tone: 'warn', text: '사용 불가' }
  else if (p.preparing) state = { tone: 'info', text: p.downloaded ? '여는 중…' : '받는 중…' }
  else if (p.ready) state = { tone: 'ok', text: needsDownload ? `준비됨 · ${formatBytes(p.sizeBytes)}` : '사용 가능' }
  else if (needsDownload && !p.downloaded) state = { tone: 'muted', text: '모델 미다운로드' }
  else state = { tone: 'muted', text: '대기' }

  return (
    <label className={`embed-option${checked ? ' embed-option--active' : ''}${disabled && !checked ? ' embed-option--disabled' : ''}`}>
      <input type="radio" name="embedding-provider" checked={checked} disabled={disabled} onChange={onChoose} />
      <div className="embed-option__body">
        <div className="embed-option__name">
          {p.label}
          {p.id === 'local' && <span className="badge badge--primary">기본</span>}
          <span className={`badge badge--${state.tone}`}>{state.text}</span>
        </div>
        <div className="embed-option__model">
          {p.model} · {p.dims}차원{needsDownload ? ` · 약 ${formatBytes(p.approxBytes)}` : ''}
        </div>
        <div className="embed-option__line">{p.device}</div>
        <div className={`embed-option__line${p.local ? '' : ' embed-option__line--warn'}`}>{p.privacy}</div>
        {p.collection.exists && (
          <div className="embed-option__line">저장된 벡터 {p.collection.points}개 · {formatBytes(p.collection.bytes)}</div>
        )}
        {p.problem && <p className="settings__error">{p.problem}</p>}
        {!p.problem && p.preparing && (
          <div className="pull-progress" aria-live="polite">
            <div className="pull-progress__bar"><span style={{ width: `${p.percent ?? 0}%` }} /></div>
            <div className="pull-progress__meta">
              {p.percent != null ? `${Math.floor(p.percent)}%` : '준비 중'}{p.message ? ` · ${p.message}` : ''}
            </div>
          </div>
        )}
        {!p.problem && !p.preparing && p.message && <p className="model-row__hint">{p.message}</p>}
        {!p.problem && !p.preparing && needsDownload && !p.downloaded && (
          <button type="button" className="btn btn--sm embed-option__action"
            onClick={(e) => { e.preventDefault(); onPrepare() }} disabled={disabled}>
            모델 미리 받기
          </button>
        )}
      </div>
    </label>
  )
}
