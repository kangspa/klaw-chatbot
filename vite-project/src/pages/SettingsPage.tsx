import { useCallback, useEffect, useRef, useState } from 'react'
import {
  deleteLocalModel,
  API_PROVIDERS,
  apiProviderOf,
  formatBytes,
  getLocalStorage,
  listModels,
  listPulls,
  setModelEnabled,
  startPull,
  type ApiProvider,
  type LocalStorage,
  type ModelOption,
  type PullState,
} from '../services/models'
import AccountCard from '../components/settings/AccountCard'
import BackupCard from '../components/settings/BackupCard'
import ApiModelsTab from '../components/settings/ApiModelsTab'
import CustomModelsCard from '../components/settings/CustomModelsCard'
import RuntimeCard from '../components/settings/RuntimeCard'
import EmbeddingCard from '../components/settings/EmbeddingCard'
import UninstallCard from '../components/settings/UninstallCard'
import { LocalBadge, ModelSwitch, PullProgress } from '../components/settings/ModelBits'

type ModelTab = ApiProvider | 'local'

/** 다운로드 진행률 조회 주기 (받는 중인 모델이 있을 때만 조회한다) */
const PULL_POLL_MS = 1500

/**
 * 마이 페이지.
 *  - 계정: 로컬 사용자 + Google 계정 연결(선택 — 개인 드라이브 저장용, components/settings/AccountCard)
 *  - 모델 설정: 회사별 탭(Google·OpenAI·Anthropic)에서 API 모델을 추가·삭제하고 켜고 끈다
 *    (components/settings/ApiModelsTab). 'Ollama' 탭은 등록된 로컬 모델의 다운로드·삭제.
 *  - 커스텀 모델: Ollama 모델명을 입력해 추가·이름 변경·삭제 (components/settings/CustomModelsCard)
 *  - 로컬 저장 공간: 서버에 받아진 모델 용량. 목록에서 빠지고 파일만 남은 모델도 여기서 지운다.
 *  - 대화 기록 검색(임베딩): 내장·Ollama·Gemini 중 선택, 바꾸면 검색 색인 재생성 (components/settings/EmbeddingCard)
 *  - 런타임 버전: Ollama·LiteLLM 버전 변경, 둘 다 한 번에도 가능 (components/settings/RuntimeCard)
 *
 * 모델을 꺼도 백엔드 호출은 막히지 않는다 — 선택 목록을 정리하는 용도다.
 * 로컬 모델은 서버(Ollama)에 한 번 받으면 모든 사용자에게 설치된 상태로 보이고, 지우면 모두에게서 지워진다.
 */
export default function SettingsPage() {
  const [models, setModels] = useState<ModelOption[]>([])
  const [tab, setTab] = useState<ModelTab>('gemini')
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

  // API 모델은 회사별 탭, 등록된 Ollama 모델은 'Ollama' 탭, 사용자가 추가한 Ollama 모델은 커스텀 모델 카드
  const apiModelsOf = (p: ApiProvider) => models.filter((m) => apiProviderOf(m.id) === p)
  const localModels = models.filter((m) => m.local && !m.custom)
  const customModels = models.filter((m) => m.local && m.custom)
  const tabs: { id: ModelTab; label: string; count: number }[] = [
    ...API_PROVIDERS.map((p) => ({ id: p.id as ModelTab, label: p.label, count: apiModelsOf(p.id).length })),
    { id: 'local', label: 'Ollama', count: localModels.length },
  ]

  // 목록에서 빠졌는데 서버에 파일만 남은 모델 (용량만 차지한다)
  const orphans = storage?.models.filter((m) => !m.registered) ?? []
  const hasLocal = models.some((m) => m.local) || (storage?.models.length ?? 0) > 0

  return (
    <div className="settings">
      <header className="settings__header">
        <h1 className="settings__title">마이 페이지</h1>
        <p className="settings__desc">계정 정보와 채팅에서 사용할 AI 모델을 관리합니다.</p>
      </header>

      <AccountCard />
      <BackupCard />

      <section className="settings__card">
        <div className="settings__card-head">
          <h2 className="settings__card-title">모델 설정</h2>
          <p className="settings__card-desc">
            켜 둔 모델만 채팅 화면의 모델 선택 목록에 보입니다. 회사별 탭에서 모델을 추가·삭제할 수 있고,
            로컬 모델은 처음 한 번 서버에 다운로드해야 쓸 수 있습니다.
          </p>
        </div>

        {error && <p className="settings__error" role="alert">{error}</p>}
        {loading && <p className="settings__empty">불러오는 중…</p>}

        {!loading && (
          <>
            <div className="tabs" role="tablist" aria-label="모델 제공사">
              {tabs.map((t) => (
                <button
                  key={t.id}
                  role="tab"
                  aria-selected={tab === t.id}
                  className={`tabs__tab${tab === t.id ? ' is-active' : ''}`}
                  onClick={() => setTab(t.id)}
                >
                  {t.label} <span className="tabs__count">{t.count}</span>
                </button>
              ))}
            </div>

            <div className="tabs__panel" role="tabpanel">
              {tab === 'local' ? (
                localModels.length === 0 ? (
                  <p className="settings__empty">등록된 Ollama 모델이 없습니다. 아래 ‘커스텀 모델’에서 추가하세요.</p>
                ) : (
                  <ul className="model-list">
                    {localModels.map((m) => (
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
                )
              ) : (
                <ApiModelsTab
                  key={tab}
                  provider={tab}
                  models={apiModelsOf(tab)}
                  saving={saving}
                  enabledCount={enabledCount}
                  onToggle={toggle}
                  onChanged={reload}
                />
              )}
            </div>
          </>
        )}
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

      {!loading && <EmbeddingCard />}
      {!loading && <RuntimeCard onChanged={reload} />}
      {!loading && <UninstallCard />}
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
