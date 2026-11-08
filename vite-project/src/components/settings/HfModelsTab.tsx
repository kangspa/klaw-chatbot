import { useEffect, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { listKeys } from '../../services/keys'
import { addHfModel, deleteHfModel, renameCustomModel, type ModelOption } from '../../services/models'
import { ModelSwitch } from './ModelBits'

interface HfModelsTabProps {
  /** Hugging Face 모델 (모두 사용자가 추가한 것) */
  models: ModelOption[]
  saving: Set<string>
  enabledCount: number
  onToggle: (m: ModelOption) => void
  onChanged: () => Promise<unknown>
}

/**
 * 마이 페이지 > 모델 설정 > Hugging Face 탭.
 * 커스텀 모델처럼 모델 ID(org/model)를 입력해 추가한다. 서버가 Hub에서 확인하고, 도구 호출을 지원하는
 * 추론 제공사(Inference Providers)를 골라 고정한다. 다운로드는 없다 (제공사 서버에서 실행).
 */
export default function HfModelsTab({ models, saving, enabledCount, onToggle, onChanged }: HfModelsTabProps) {
  const [name, setName] = useState('')
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [tokenSet, setTokenSet] = useState<boolean | null>(null)

  useEffect(() => {
    let alive = true
    listKeys().then(
      (keys) => { if (alive) setTokenSet(!!keys.find((k) => k.id === 'HUGGINGFACE')?.configured) },
      () => { if (alive) setTokenSet(null) },
    )
    return () => { alive = false }
  }, [])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!name.trim() || adding) return
    setAdding(true)
    setError(null)
    try {
      await addHfModel(name.trim())
      setName('')
      await onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : '모델을 추가하지 못했습니다.')
    } finally {
      setAdding(false)
    }
  }

  const remove = async (m: ModelOption) => {
    if (!window.confirm(`'${m.label}'을 목록에서 삭제할까요?`)) return
    setBusy((s) => new Set(s).add(m.id))
    setError(null)
    try {
      await deleteHfModel(m.id)
      await onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : '모델을 삭제하지 못했습니다.')
    } finally {
      setBusy((s) => { const n = new Set(s); n.delete(m.id); return n })
    }
  }

  const rename = async (m: ModelOption, label: string) => {
    setError(null)
    try {
      await renameCustomModel(m.id, label)
      await onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : '이름을 바꾸지 못했습니다.')
      throw err
    }
  }

  return (
    <div className="api-tab">
      {tokenSet === false && (
        <p className="api-tab__warn">
          Hugging Face 토큰이 없어 호출에 실패합니다. <Link to="/keys">API 키 관리</Link>에서 HUGGINGFACE 키를 입력하세요.
        </p>
      )}
      <p className="catalog__note">
        <a href="https://huggingface.co/models?inference_provider=all&pipeline_tag=text-generation" target="_blank" rel="noreferrer">
          Inference Providers 모델 목록</a>의 모델 ID를 입력하세요. 도구 호출을 지원하는 추론 제공사를 자동으로 골라 고정합니다.
        비용은 Hugging Face 계정에서 청구되며 대시보드에는 0으로 집계됩니다.
      </p>
      <form className="custom-add" onSubmit={submit}>
        <input
          className="field__input custom-add__input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="모델 ID (예: zai-org/GLM-5.3)"
          aria-label="추가할 Hugging Face 모델 ID"
          disabled={adding}
          spellCheck={false}
          autoComplete="off"
        />
        <button className="btn btn--primary" type="submit" disabled={adding || !name.trim()}>
          {adding ? '확인 중…' : '추가'}
        </button>
      </form>
      {error && <p className="settings__error" role="alert">{error}</p>}

      {models.length === 0 ? (
        <p className="settings__empty">추가한 Hugging Face 모델이 없습니다.</p>
      ) : (
        <ul className="model-list">
          {models.map((m) => (
            <HfRow
              key={m.id}
              model={m}
              saving={saving.has(m.id)}
              deleting={busy.has(m.id)}
              isLastEnabled={m.enabled && enabledCount === 1}
              onToggle={() => onToggle(m)}
              onRename={(label) => rename(m, label)}
              onDelete={() => void remove(m)}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

interface HfRowProps {
  model: ModelOption
  saving: boolean
  deleting: boolean
  isLastEnabled: boolean
  onToggle: () => void
  onRename: (label: string) => Promise<void>
  onDelete: () => void
}

function HfRow({ model: m, saving, deleting, isLastEnabled, onToggle, onRename, onDelete }: HfRowProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(m.label)
  const [renaming, setRenaming] = useState(false)
  const meta = m.meta ?? {}

  const save = async () => {
    const label = draft.trim()
    if (!label || label === m.label) { setEditing(false); return }
    setRenaming(true)
    try { await onRename(label); setEditing(false) } catch { /* 오류는 탭 상단에 표시 */ } finally { setRenaming(false) }
  }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') { e.preventDefault(); void save() }
    if (e.key === 'Escape') setEditing(false)
  }
  const price = meta.inputPer1M != null && meta.outputPer1M != null
    ? `입력 $${meta.inputPer1M} · 출력 $${meta.outputPer1M} /1M 토큰` : null
  const ctx = meta.contextTokens ? `컨텍스트 ${Math.round(meta.contextTokens / 1000)}K` : null

  return (
    <li className={`model-row${m.enabled ? '' : ' is-off'}`}>
      <div className="model-row__main">
        {editing ? (
          <div className="model-row__rename">
            <input className="field__input model-row__rename-input" value={draft} onChange={(e) => setDraft(e.target.value)}
                   onKeyDown={onKey} maxLength={60} autoFocus disabled={renaming} aria-label="표시 이름" />
            <button className="btn btn--primary btn--sm" onClick={() => void save()} disabled={renaming}>저장</button>
            <button className="btn btn--plain btn--sm" onClick={() => setEditing(false)} disabled={renaming}>취소</button>
          </div>
        ) : (
          <div className="model-row__name">
            {m.label}
            <button className="model-row__edit" onClick={() => { setDraft(m.label); setEditing(true) }}
                    title="표시 이름 변경" aria-label={`${m.label} 이름 변경`}>✎</button>
            {meta.provider && <span className="badge badge--info" title="고정한 추론 제공사">{meta.provider}</span>}
            {m.supportsTools === false && <span className="badge badge--warn">도구 호출 미지원</span>}
          </div>
        )}
        <code className="model-row__id">{meta.repo ?? m.id}</code>
        {(price || ctx) && <p className="model-row__meta">{[price, ctx].filter(Boolean).join(' · ')}</p>}
        {m.supportsTools === false && (
          <p className="model-row__hint">이 모델을 제공하는 곳 중 도구 호출을 지원하는 제공사가 없어 법령·웹 검색 답변이 실패할 수 있습니다.</p>
        )}
      </div>
      <div className="model-row__actions">
        <button className="btn btn--danger-outline btn--sm" onClick={onDelete} disabled={deleting}>
          {deleting ? '삭제 중…' : '삭제'}
        </button>
        <ModelSwitch model={m} saving={saving} isLastEnabled={isLastEnabled} onToggle={onToggle} />
      </div>
    </li>
  )
}
