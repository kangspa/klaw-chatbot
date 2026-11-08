import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  addApiModel,
  deleteApiModel,
  getCatalog,
  restoreApiDefaults,
  type ApiProvider,
  type CatalogModel,
  type ModelOption,
  type ProviderCatalog,
} from '../../services/models'
import { ModelSwitch } from './ModelBits'

interface ApiModelsTabProps {
  provider: ApiProvider
  /** 이 제공사의 등록 모델 (기본 제공 + 사용자가 추가한 것) */
  models: ModelOption[]
  saving: Set<string>
  enabledCount: number
  onToggle: (m: ModelOption) => void
  /** 추가·삭제·복원 뒤 모델 목록을 새로 받는다 */
  onChanged: () => Promise<unknown>
}

/**
 * 마이 페이지 > 모델 설정의 회사별 탭 (Google·OpenAI·Anthropic).
 * 기본 제공 모델(seed)에 더해, 지금 설치된 LiteLLM이 아는 모델 중에서 골라 추가·삭제한다.
 * 기본 제공 모델도 지울 수 있고 '복원'으로 되살린다. 기본 모델(요청에 모델이 없을 때 쓰는 것)은 지울 수 없다.
 */
export default function ApiModelsTab({ provider, models, saving, enabledCount, onToggle, onChanged }: ApiModelsTabProps) {
  const [cat, setCat] = useState<ProviderCatalog | null>(null)
  const [catError, setCatError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [adding, setAdding] = useState(false)
  const [query, setQuery] = useState('')

  useEffect(() => {
    let alive = true
    getCatalog(provider).then(
      (c) => { if (alive) { setCat(c); setCatError(null) } },
      (e) => { if (alive) setCatError(e instanceof Error ? e.message : '모델 목록을 불러오지 못했습니다.') },
    )
    return () => { alive = false }
  }, [provider])

  const refresh = useCallback(async () => {
    await onChanged()
    try { setCat(await getCatalog(provider)) } catch { /* 다음에 다시 받는다 */ }
  }, [onChanged, provider])

  const run = async (key: string, action: () => Promise<unknown>, failMsg: string) => {
    setBusy((s) => new Set(s).add(key))
    setError(null)
    try {
      await action()
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : failMsg)
    } finally {
      setBusy((s) => { const n = new Set(s); n.delete(key); return n })
    }
  }

  const remove = (m: ModelOption) => {
    const note = m.custom
      ? '목록에서 제거됩니다. 다시 쓰려면 \'모델 추가\'에서 고르면 됩니다.'
      : '기본 제공 모델입니다. 지운 뒤에도 \'기본 모델 복원\'으로 되살릴 수 있습니다.'
    if (!window.confirm(`'${m.label}'을 목록에서 삭제할까요?\n\n${note}`)) return
    void run(m.id, () => deleteApiModel(m.id), '모델을 삭제하지 못했습니다.')
  }

  const info = useMemo(() => new Map(cat?.models.map((c) => [c.id, c]) ?? []), [cat])
  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (cat?.models ?? []).filter((c) => !c.added && (!q || c.name.toLowerCase().includes(q)))
  }, [cat, query])

  return (
    <div className="api-tab">
      {cat && !cat.apiKeyConfigured && (
        <p className="api-tab__warn">
          <code>.env</code>에 <code>{cat.apiKeyEnv}</code>가 설정되어 있지 않아 이 회사 모델은 호출에 실패합니다.
        </p>
      )}
      {error && <p className="settings__error" role="alert">{error}</p>}

      {models.length === 0 ? (
        <p className="settings__empty">이 회사 모델이 없습니다. 아래 ‘모델 추가’에서 고르세요.</p>
      ) : (
        <ul className="model-list">
          {models.map((m) => {
            const c = info.get(m.id)
            return (
              <li key={m.id} className={`model-row${m.enabled ? '' : ' is-off'}`}>
                <div className="model-row__main">
                  <div className="model-row__name">
                    {m.label}
                    {m.isDefault && <span className="badge badge--primary">기본</span>}
                    <span className={`badge ${m.custom ? 'badge--info' : 'badge--muted'}`}>{m.custom ? '추가함' : '기본 제공'}</span>
                    {c?.deprecationDate && <span className="badge badge--warn">{c.deprecationDate} 종료 예정</span>}
                  </div>
                  <code className="model-row__id">{m.id}</code>
                  {c && <p className="model-row__meta">{specLine(c)}</p>}
                  {cat && !c && (
                    <p className="model-row__hint">LiteLLM {cat.litellmVersion} 가격표에 없는 모델입니다 (비용이 0으로 집계될 수 있음).</p>
                  )}
                </div>
                <div className="model-row__actions">
                  <button
                    className="btn btn--danger-outline btn--sm"
                    onClick={() => remove(m)}
                    disabled={m.isDefault || busy.has(m.id)}
                    title={m.isDefault ? '기본 모델은 삭제할 수 없습니다' : undefined}
                  >
                    {busy.has(m.id) ? '삭제 중…' : '삭제'}
                  </button>
                  <ModelSwitch model={m} saving={saving.has(m.id)} isLastEnabled={m.enabled && enabledCount === 1}
                               onToggle={() => onToggle(m)} />
                </div>
              </li>
            )
          })}
        </ul>
      )}

      <div className="api-tab__footer">
        <button className="btn btn--outline btn--sm" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
          {adding ? '추가 닫기' : '＋ 모델 추가'}
        </button>
        {cat && cat.removedDefaults.length > 0 && (
          <button
            className="btn btn--plain btn--sm"
            onClick={() => void run('restore', () => restoreApiDefaults(provider), '복원하지 못했습니다.')}
            disabled={busy.has('restore')}
            title={cat.removedDefaults.map((r) => r.label).join(', ')}
          >
            기본 모델 복원 ({cat.removedDefaults.length})
          </button>
        )}
        {cat && <span className="api-tab__source">LiteLLM {cat.litellmVersion} 지원 모델 {cat.models.length}개</span>}
      </div>

      {adding && (
        <div className="catalog">
          {catError && <p className="settings__error" role="alert">{catError}</p>}
          <input
            className="field__input catalog__search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="모델 이름으로 찾기 (예: flash, gpt-6, sonnet)"
            aria-label="추가할 모델 찾기"
            autoFocus
          />
          <p className="catalog__note">도구 호출을 지원하고 아직 서비스 중인 채팅 모델만 보여줍니다.</p>
          {cat && candidates.length === 0 && (
            <p className="settings__empty">{query ? '찾는 모델이 없습니다.' : '추가할 수 있는 모델을 모두 추가했습니다.'}</p>
          )}
          <ul className="catalog__list">
            {candidates.map((c) => (
              <li key={c.id} className="catalog__item">
                <div className="catalog__body">
                  <div className="catalog__name">
                    {c.name}
                    {c.deprecationDate && <span className="badge badge--warn">{c.deprecationDate} 종료 예정</span>}
                  </div>
                  <div className="catalog__meta">{specLine(c)}</div>
                </div>
                <button
                  className="btn btn--outline btn--sm"
                  onClick={() => void run(c.id, () => addApiModel(c.id), '모델을 추가하지 못했습니다.')}
                  disabled={busy.has(c.id)}
                >
                  {busy.has(c.id) ? '추가 중…' : '추가'}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** '입력 $2 · 출력 $10 /1M 토큰 · 컨텍스트 922K · 이미지 · 추론' */
function specLine(c: CatalogModel): string {
  const parts: string[] = []
  if (c.inputPer1M != null && c.outputPer1M != null) parts.push(`입력 $${fmt(c.inputPer1M)} · 출력 $${fmt(c.outputPer1M)} /1M 토큰`)
  if (c.contextTokens) parts.push(`컨텍스트 ${c.contextTokens >= 1_000_000 ? `${(c.contextTokens / 1_000_000).toFixed(c.contextTokens % 1_000_000 ? 2 : 0)}M` : `${Math.round(c.contextTokens / 1000)}K`}`)
  if (c.supportsVision) parts.push('이미지 입력')
  if (c.supportsReasoning) parts.push('추론')
  return parts.join(' · ')
}

function fmt(n: number): string {
  return n >= 1 ? String(Number(n.toFixed(2))) : String(Number(n.toFixed(3)))
}
