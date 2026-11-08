import { useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  addCustomModel,
  deleteCustomModel,
  formatBytes,
  renameCustomModel,
  type ModelOption,
  type PullState,
} from '../../services/models'
import { LocalBadge, ModelSwitch, PullProgress } from './ModelBits'

interface CustomModelsCardProps {
  /** 커스텀 모델만 */
  models: ModelOption[]
  pulls: Record<string, PullState>
  saving: Set<string>
  /** 켜진 모델 전체 개수 (등록 모델 포함) — 마지막 하나는 끌 수 없다 */
  enabledCount: number
  onToggle: (m: ModelOption) => void
  /** 실패한 다운로드 다시 시도 */
  onRetry: (m: ModelOption) => void
  /** 추가·이름 변경·삭제 뒤 목록을 새로 받는다 */
  onChanged: () => Promise<unknown>
}

/**
 * 커스텀 모델: Ollama 사이트의 모델명(예: lfm3-omni:2b)을 입력해 추가하면 서버가 받아서 바로 쓸 수 있게 한다.
 * 추가한 모델은 채팅 화면 선택 목록의 '커스텀' 그룹에 보인다.
 * 삭제는 서버의 모델 파일까지 지운다 (로컬 모델 삭제와 같음 — 모든 사용자에게서 지워진다).
 */
export default function CustomModelsCard({
  models, pulls, saving, enabledCount, onToggle, onRetry, onChanged,
}: CustomModelsCardProps) {
  const [name, setName] = useState('')
  const [adding, setAdding] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<Set<string>>(new Set())

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const value = name.trim()
    if (!value || adding) return
    setAdding(true)
    setFormError(null)
    try {
      await addCustomModel(value)
      setName('')
      await onChanged()
    } catch (err) {
      setFormError(err instanceof Error ? err.message : '모델을 추가하지 못했습니다.')
    } finally {
      setAdding(false)
    }
  }

  const remove = async (m: ModelOption) => {
    const pulling = pulls[m.id]?.status === 'pulling' || m.localStatus === 'pulling'
    const detail = pulling
      ? '진행 중인 다운로드를 취소합니다.'
      : m.localStatus === 'installed'
        ? `서버의 모델 파일${m.sizeBytes ? `(${formatBytes(m.sizeBytes)})` : ''}도 함께 지워지며, 모든 사용자에게서 삭제됩니다.`
        : '목록에서만 제거됩니다.'
    if (!window.confirm(`커스텀 모델 '${m.label}'을 삭제할까요?\n\n${detail}`)) return
    setDeleting((s) => new Set(s).add(m.id))
    setRowError(null)
    try {
      await deleteCustomModel(m.id)
      await onChanged()
    } catch (err) {
      setRowError(err instanceof Error ? err.message : '모델을 삭제하지 못했습니다.')
    } finally {
      setDeleting((s) => { const n = new Set(s); n.delete(m.id); return n })
    }
  }

  const rename = async (m: ModelOption, label: string) => {
    setRowError(null)
    try {
      await renameCustomModel(m.id, label)
      await onChanged()
    } catch (err) {
      setRowError(err instanceof Error ? err.message : '이름을 바꾸지 못했습니다.')
      throw err
    }
  }

  return (
    <section className="settings__card">
      <div className="settings__card-head">
        <h2 className="settings__card-title">커스텀 모델</h2>
        <p className="settings__card-desc">
          <a href="https://ollama.com/search" target="_blank" rel="noreferrer">Ollama 모델 목록</a>에서
          모델명을 그대로 입력하면 서버에 받아 채팅에서 쓸 수 있습니다. 도구 호출(tools)을 지원하는 모델이어야 제대로 답변합니다.
        </p>
      </div>

      <form className="custom-add" onSubmit={submit}>
        <input
          className="field__input custom-add__input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="모델명 (예: lfm3-omni:2b)"
          aria-label="추가할 Ollama 모델명"
          disabled={adding}
          spellCheck={false}
          autoComplete="off"
        />
        <button className="btn btn--primary" type="submit" disabled={adding || !name.trim()}>
          {adding ? '확인 중…' : '추가'}
        </button>
      </form>
      {adding && <p className="custom-add__hint">Ollama에서 모델을 찾는 중입니다. 찾으면 바로 다운로드를 시작합니다.</p>}
      {formError && <p className="settings__error" role="alert">{formError}</p>}
      {rowError && <p className="settings__error" role="alert">{rowError}</p>}

      {models.length === 0 ? (
        <p className="settings__empty">추가한 커스텀 모델이 없습니다.</p>
      ) : (
        <ul className="model-list">
          {models.map((m) => (
            <CustomModelRow
              key={m.id}
              model={m}
              pull={pulls[m.id] ?? null}
              saving={saving.has(m.id)}
              deleting={deleting.has(m.id)}
              isLastEnabled={m.enabled && enabledCount === 1}
              onToggle={() => onToggle(m)}
              onRetry={() => onRetry(m)}
              onRename={(label) => rename(m, label)}
              onDelete={() => remove(m)}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

interface CustomModelRowProps {
  model: ModelOption
  pull: PullState | null
  saving: boolean
  deleting: boolean
  isLastEnabled: boolean
  onToggle: () => void
  onRetry: () => void
  onRename: (label: string) => Promise<void>
  onDelete: () => void
}

function CustomModelRow({
  model: m, pull, saving, deleting, isLastEnabled, onToggle, onRetry, onRename, onDelete,
}: CustomModelRowProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(m.label)
  const [renaming, setRenaming] = useState(false)

  const pulling = pull?.status === 'pulling' || m.localStatus === 'pulling'
  const pullFailed = !pulling && pull?.status === 'error'

  const startEdit = () => { setDraft(m.label); setEditing(true) }
  const save = async () => {
    const label = draft.trim()
    if (!label || label === m.label) { setEditing(false); return }
    setRenaming(true)
    try {
      await onRename(label)
      setEditing(false)
    } catch {
      /* 오류는 카드 상단에 표시된다 — 편집 상태를 유지해 다시 고칠 수 있게 한다 */
    } finally {
      setRenaming(false)
    }
  }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') { e.preventDefault(); void save() }
    if (e.key === 'Escape') setEditing(false)
  }

  return (
    <li className={`model-row${m.enabled ? '' : ' is-off'}`}>
      <div className="model-row__main">
        {editing ? (
          <div className="model-row__rename">
            <input
              className="field__input model-row__rename-input"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKey}
              maxLength={60}
              autoFocus
              disabled={renaming}
              aria-label="표시 이름"
            />
            <button className="btn btn--primary btn--sm" onClick={() => void save()} disabled={renaming}>저장</button>
            <button className="btn btn--plain btn--sm" onClick={() => setEditing(false)} disabled={renaming}>취소</button>
          </div>
        ) : (
          <div className="model-row__name">
            {m.label}
            <button className="model-row__edit" onClick={startEdit} title="표시 이름 변경" aria-label={`${m.label} 이름 변경`}>✎</button>
            <LocalBadge model={m} pulling={pulling} />
            {m.sizeBytes != null && <span className="badge badge--muted">{formatBytes(m.sizeBytes)}</span>}
            {m.supportsTools === false && <span className="badge badge--warn">도구 호출 미지원</span>}
          </div>
        )}
        <code className="model-row__id">{m.ollamaName ?? m.id}</code>

        {pulling && <PullProgress pull={pull} />}
        {pullFailed && <p className="model-row__error">다운로드 실패: {pull?.message}</p>}
        {m.supportsTools === false && (
          <p className="model-row__hint">
            이 모델은 도구 호출을 지원하지 않습니다. 이 챗봇은 법령 검색·웹 검색에 도구를 쓰므로 답변이 실패할 수 있습니다.
          </p>
        )}
      </div>

      <div className="model-row__actions">
        {m.localStatus === 'missing' && !pulling && (
          <button className="btn btn--outline btn--sm" onClick={onRetry}>{pullFailed ? '다시 시도' : '다운로드'}</button>
        )}
        <button className="btn btn--danger-outline btn--sm" onClick={onDelete} disabled={deleting}>
          {deleting ? '삭제 중…' : '삭제'}
        </button>
        <ModelSwitch model={m} saving={saving} isLastEnabled={isLastEnabled} onToggle={onToggle} />
      </div>
    </li>
  )
}
