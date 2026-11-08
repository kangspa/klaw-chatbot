import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from '../context/AuthContext'
import {
  deleteLocalModel,
  formatBytes,
  getLocalStorage,
  groupModels,
  listModels,
  listPulls,
  setModelEnabled,
  startPull,
  type LocalStorage,
  type ModelOption,
  type PullState,
} from '../services/models'
import { formatWhen } from '../services/time'
import CustomModelsCard from '../components/settings/CustomModelsCard'
import OllamaServerCard from '../components/settings/OllamaServerCard'
import { LocalBadge, ModelSwitch, PullProgress } from '../components/settings/ModelBits'

/** 다운로드 진행률 조회 주기 (받는 중인 모델이 있을 때만 조회한다) */
const PULL_POLL_MS = 1500

/**
 * 마이 페이지.
 *  - 계정 정보
 *  - 모델 설정: 채팅 화면 선택 목록에 보일 모델을 켜고 끈다. 로컬 모델은 여기서 다운로드·삭제한다.
 *  - 커스텀 모델: Ollama 모델명을 입력해 추가·이름 변경·삭제 (components/settings/CustomModelsCard)
 *  - 로컬 저장 공간: 서버에 받아진 모델 용량. 목록에서 빠지고 파일만 남은 모델도 여기서 지운다.
 *  - 로컬 모델 서버: Ollama 버전 변경 (components/settings/OllamaServerCard)
 *
 * 모델을 꺼도 백엔드 호출은 막히지 않는다 — 선택 목록을 정리하는 용도다.
 * 로컬 모델은 서버(Ollama)에 한 번 받으면 모든 사용자에게 설치된 상태로 보이고, 지우면 모두에게서 지워진다.
 */
export default function SettingsPage() {
  const { user } = useAuth()
  const [models, setModels] = useState<ModelOption[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 저장 요청이 진행 중인 모델 (토글 중복 클릭 방지)
  const [saving, setSaving] = useState<Set<string>>(new Set())
  const [pulls, setPulls] = useState<Record<string, PullState>>({})
  // 서버에 받아진 로컬 모델과 용량 (Ollama에 연결할 수 없으면 reachable=false)
  const [storage, setStorage] = useState<LocalStorage | null>(null)
  // 삭제 요청이 진행 중인 모델
  const [deleting, setDeleting] = useState<Set<string>>(new Set())
  // 직전 조회 때 받는 중이던 모델. 여기서 빠진 모델이 생기면 다운로드가 끝난 것이다.
  const prevPullingRef = useRef<Set<string>>(new Set())

  const applyList = useCallback((list: ModelOption[]) => {
    setModels(list)
    setPulls(Object.fromEntries(list.filter((m) => m.pull).map((m) => [m.id, m.pull as PullState])))
    prevPullingRef.current = new Set(list.filter((m) => m.pull?.status === 'pulling').map((m) => m.id))
    setError(null)
    setLoading(false)
  }, [])

  const applyError = useCallback((e: unknown) => {
    setError(e instanceof Error ? e.message : '모델 목록을 불러오지 못했습니다.')
    setLoading(false)
  }, [])

  const reload = useCallback(() => Promise.all([
    listModels().then(applyList, applyError),
    getLocalStorage().then(setStorage, () => setStorage(null)),
  ]), [applyList, applyError])

  useEffect(() => {
    let alive = true
    listModels().then(
      (list) => { if (alive) applyList(list) },
      (e) => { if (alive) applyError(e) },
    )
    getLocalStorage().then(
      (s) => { if (alive) setStorage(s) },
      () => { if (alive) setStorage(null) },
    )
    return () => { alive = false }
  }, [applyList, applyError])

  const anyPulling = Object.values(pulls).some((p) => p.status === 'pulling')

  // 받는 중인 모델이 있으면 진행률을 주기적으로 받고, 끝난 모델이 생기면 설치 상태를 새로 받는다
  useEffect(() => {
    if (!anyPulling) return
    const timer = window.setInterval(async () => {
      try {
        const next = await listPulls()
        setPulls(next)
        const stillPulling = new Set(Object.entries(next).filter(([, p]) => p.status === 'pulling').map(([id]) => id))
        const finished = [...prevPullingRef.current].some((id) => !stillPulling.has(id))
        prevPullingRef.current = stillPulling
        if (finished) void reload()
      } catch {
        /* 일시 오류는 다음 주기에 다시 시도 */
      }
    }, PULL_POLL_MS)
    return () => window.clearInterval(timer)
  }, [anyPulling, reload])

  const enabledCount = models.filter((m) => m.enabled).length

  const toggle = async (m: ModelOption) => {
    const next = !m.enabled
    // 낙관적 반영 → 실패하면 되돌린다
    setModels((list) => list.map((x) => (x.id === m.id ? { ...x, enabled: next } : x)))
    setSaving((s) => new Set(s).add(m.id))
    try {
      await setModelEnabled(m.id, next)
    } catch (e) {
      setModels((list) => list.map((x) => (x.id === m.id ? { ...x, enabled: m.enabled } : x)))
      setError(e instanceof Error ? e.message : '설정을 저장하지 못했습니다.')
    } finally {
      setSaving((s) => { const n = new Set(s); n.delete(m.id); return n })
    }
  }

  const download = async (m: ModelOption) => {
    try {
      const state = await startPull(m.id)
      prevPullingRef.current.add(m.id)
      setPulls((p) => ({ ...p, [m.id]: state }))
      setModels((list) => list.map((x) => (x.id === m.id ? { ...x, localStatus: 'pulling' } : x)))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '다운로드를 시작하지 못했습니다.')
    }
  }

  /** 로컬 모델 파일 삭제. 서버 전체에서 지워지므로 반드시 확인을 받는다. */
  const remove = async (modelId: string, label: string, sizeBytes: number | null) => {
    const size = sizeBytes ? ` (${formatBytes(sizeBytes)})` : ''
    const ok = window.confirm(
      `'${label}' 모델 파일${size}을 서버에서 삭제할까요?\n\n` +
      '모든 사용자에게서 삭제되며, 다시 쓰려면 다시 다운로드해야 합니다.',
    )
    if (!ok) return
    setDeleting((s) => new Set(s).add(modelId))
    try {
      await deleteLocalModel(modelId)
      setError(null)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : '모델을 삭제하지 못했습니다.')
    } finally {
      setDeleting((s) => { const n = new Set(s); n.delete(modelId); return n })
    }
  }

  // 관리자가 등록한 모델과 사용자가 추가한 커스텀 모델은 카드를 나눠 보여준다
  const builtinModels = models.filter((m) => !m.custom)
  const customModels = models.filter((m) => m.custom)

  // 목록에서 빠졌는데 서버에 파일만 남은 모델 (용량만 차지한다)
  const orphans = storage?.models.filter((m) => !m.registered) ?? []
  const hasLocal = models.some((m) => m.local) || (storage?.models.length ?? 0) > 0

  return (
    <div className="settings">
      <header className="settings__header">
        <h1 className="settings__title">마이 페이지</h1>
        <p className="settings__desc">계정 정보와 채팅에서 사용할 AI 모델을 관리합니다.</p>
      </header>

      <section className="settings__card">
        <h2 className="settings__card-title">계정</h2>
        <div className="account">
          {user?.picture ? (
            <img className="account__avatar" src={user.picture} alt="" referrerPolicy="no-referrer" />
          ) : (
            <span className="account__avatar account__avatar--initial" aria-hidden>{user?.name?.[0]?.toUpperCase() ?? '?'}</span>
          )}
          <div className="account__body">
            <div className="account__name">{user?.name}</div>
            <div className="account__email">{user?.email}</div>
            {user?.createdAt && <div className="account__meta">가입 {formatWhen(user.createdAt)}</div>}
          </div>
        </div>
      </section>

      <section className="settings__card">
        <div className="settings__card-head">
          <h2 className="settings__card-title">모델 설정</h2>
          <p className="settings__card-desc">
            켜 둔 모델만 채팅 화면의 모델 선택 목록에 보입니다. 로컬 모델은 처음 한 번 서버에 다운로드해야 쓸 수 있습니다.
          </p>
        </div>

        {error && <p className="settings__error" role="alert">{error}</p>}
        {loading && <p className="settings__empty">불러오는 중…</p>}
        {!loading && builtinModels.length === 0 && !error && <p className="settings__empty">등록된 모델이 없습니다.</p>}

        {groupModels(builtinModels).map((g) => (
          <div key={g.group} className="model-group">
            <h3 className="model-group__title">{g.group}</h3>
            <ul className="model-list">
              {g.models.map((m) => (
                <ModelRow
                  key={m.id}
                  model={m}
                  pull={pulls[m.id] ?? null}
                  saving={saving.has(m.id)}
                  isLastEnabled={m.enabled && enabledCount === 1}
                  onToggle={() => toggle(m)}
                  onDownload={() => download(m)}
                  deleting={deleting.has(m.id)}
                  onDelete={() => remove(m.id, m.label, m.sizeBytes)}
                />
              ))}
            </ul>
          </div>
        ))}
      </section>

      {!loading && (
        <CustomModelsCard
          models={customModels}
          pulls={pulls}
          saving={saving}
          enabledCount={enabledCount}
          onToggle={toggle}
          onRetry={download}
          onChanged={reload}
        />
      )}

      {hasLocal && (
        <section className="settings__card">
          <div className="settings__card-head">
            <h2 className="settings__card-title">로컬 저장 공간</h2>
            <p className="settings__card-desc">
              서버(Ollama)에 받아 둔 로컬 모델이 차지하는 디스크 용량입니다. 쓰지 않는 모델은 삭제해 공간을 정리하세요.
            </p>
          </div>

          {storage === null || !storage.reachable ? (
            <p className="settings__empty">로컬 모델 서버(Ollama)에 연결할 수 없어 용량을 확인하지 못했습니다.</p>
          ) : (
            <>
              <div className="storage-total">
                <span>사용 중</span>
                <strong>{formatBytes(storage.totalBytes)}</strong>
                <span className="storage-total__count">모델 {storage.models.length}개</span>
              </div>
              {orphans.length > 0 && (
                <div className="model-group">
                  <h3 className="model-group__title">목록에 없는 모델 (파일만 남음)</h3>
                  <ul className="model-list">
                    {orphans.map((o) => (
                      <li key={o.id} className="model-row">
                        <div className="model-row__main">
                          <div className="model-row__name">
                            {o.name}
                            <span className="badge badge--muted">{formatBytes(o.sizeBytes)}</span>
                          </div>
                          <p className="model-row__hint">모델 목록에서 빠져 채팅에서 쓸 수 없지만 용량을 차지하고 있습니다.</p>
                        </div>
                        <div className="model-row__actions">
                          <button
                            className="btn btn--danger-outline btn--sm"
                            onClick={() => remove(o.id, o.name, o.sizeBytes)}
                            disabled={deleting.has(o.id)}
                          >
                            {deleting.has(o.id) ? '삭제 중…' : '삭제'}
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </section>
      )}

      {!loading && <OllamaServerCard onChanged={reload} />}
    </div>
  )
}

interface ModelRowProps {
  model: ModelOption
  pull: PullState | null
  saving: boolean
  isLastEnabled: boolean
  onToggle: () => void
  onDownload: () => void
  deleting: boolean
  onDelete: () => void
}

function ModelRow({ model: m, pull, saving, isLastEnabled, onToggle, onDownload, deleting, onDelete }: ModelRowProps) {
  const pulling = pull?.status === 'pulling' || m.localStatus === 'pulling'
  const pullFailed = pull?.status === 'error' && m.localStatus === 'missing'

  return (
    <li className={`model-row${m.enabled ? '' : ' is-off'}`}>
      <div className="model-row__main">
        <div className="model-row__name">
          {m.label}
          {m.isDefault && <span className="badge badge--primary">기본</span>}
          {m.local && <LocalBadge model={m} pulling={pulling} />}
          {m.sizeBytes != null && <span className="badge badge--muted">{formatBytes(m.sizeBytes)}</span>}
        </div>
        <code className="model-row__id">{m.id}</code>

        {pulling && <PullProgress pull={pull} />}
        {pullFailed && <p className="model-row__error">다운로드 실패: {pull?.message}</p>}
        {m.localStatus === 'unreachable' && (
          <p className="model-row__hint">로컬 모델 서버(Ollama)에 연결할 수 없어 설치 상태를 확인하지 못했습니다.</p>
        )}
      </div>

      <div className="model-row__actions">
        {m.localStatus === 'missing' && !pulling && (
          <button className="btn btn--outline btn--sm" onClick={onDownload}>
            {pullFailed ? '다시 시도' : '다운로드'}
          </button>
        )}
        {m.localStatus === 'installed' && (
          <button className="btn btn--danger-outline btn--sm" onClick={onDelete} disabled={deleting}
                  title="서버에서 모델 파일을 지워 디스크 공간을 확보합니다">
            {deleting ? '삭제 중…' : '삭제'}
          </button>
        )}
        <ModelSwitch model={m} saving={saving} isLastEnabled={isLastEnabled} onToggle={onToggle} />
      </div>
    </li>
  )
}
