import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { deleteKey, listKeys, setKey, testKey, type ApiKeyItem } from '../services/keys'

/** LAW_OC 저장 후 법령 검색 서버 재시작·인증 확인 진행 상태 조회 주기 */
const APPLY_POLL_MS = 2000

const APPLY_NOTE: Record<ApiKeyItem['apply'], string> = {
  immediate: '저장하면 바로 적용됩니다.',
  reset_models: '저장하면 바로 적용됩니다 (해당 모델을 새로 연결).',
  restart_law: '저장하면 법령 검색 서버를 다시 시작하고 인증을 확인합니다 (10초 안팎).',
}

/**
 * API 키 관리.
 * 키는 이 PC의 .env에 저장된다(개인 PC용 서비스). 화면에는 원문을 다시 보여주지 않고 앞뒤 몇 글자만 보여준다.
 */
export default function KeysPage() {
  const [keys, setKeys] = useState<ApiKeyItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const apply = useCallback((list: ApiKeyItem[]) => { setKeys(list); setLoading(false); setError(null) }, [])
  const fail = useCallback((e: unknown) => {
    setError(e instanceof Error ? e.message : 'API 키 목록을 불러오지 못했습니다.')
    setLoading(false)
  }, [])
  const reload = useCallback(() => listKeys().then(apply, fail), [apply, fail])

  useEffect(() => {
    let alive = true
    listKeys().then((l) => { if (alive) apply(l) }, (e) => { if (alive) fail(e) })
    return () => { alive = false }
  }, [apply, fail])

  // LAW_OC를 저장한 뒤 법령 검색 서버가 다시 뜨고 인증 확인이 끝날 때까지 상태를 갱신한다
  const lawApplying = keys.some((k) => k.applyStatus?.status === 'running')
  useEffect(() => {
    if (!lawApplying) return
    const timer = window.setInterval(() => { void reload() }, APPLY_POLL_MS)
    return () => window.clearInterval(timer)
  }, [lawApplying, reload])

  const update = (item: ApiKeyItem) => setKeys((list) => list.map((k) => (k.id === item.id ? item : k)))

  return (
    <div className="settings">
      <header className="settings__header">
        <h1 className="settings__title">API 키 관리</h1>
        <p className="settings__desc">모델 호출과 법령 검색에 쓰는 키를 입력하고 관리합니다.</p>
      </header>

      <section className="settings__card">
        <p className="keys-note">
          🔒 키는 이 PC의 <code>.env</code> 파일에만 저장됩니다. 저장한 키는 화면에 다시 보여주지 않고 앞뒤 몇 글자만 표시합니다.
          ‘연결 확인’은 해당 서비스에만 키를 보내 인증만 확인합니다 (토큰·요금을 쓰지 않음).
        </p>
        {error && <p className="settings__error" role="alert">{error}</p>}
        {loading && <p className="settings__empty">불러오는 중…</p>}
        <ul className="key-list">
          {keys.map((k) => <KeyRow key={k.id} item={k} onUpdated={update} />)}
        </ul>
      </section>
    </div>
  )
}

function KeyRow({ item: k, onUpdated }: { item: ApiKeyItem; onUpdated: (k: ApiKeyItem) => void }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState<null | 'save' | 'test' | 'delete'>(null)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!value.trim()) return
    setBusy('save'); setError(null); setResult(null)
    try {
      onUpdated(await setKey(k.id, value.trim()))
      setValue(''); setEditing(false); setShow(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '저장하지 못했습니다.')
    } finally {
      setBusy(null)
    }
  }

  const test = async () => {
    setBusy('test'); setError(null); setResult(null)
    try { setResult(await testKey(k.id)) } catch (err) {
      setError(err instanceof Error ? err.message : '확인하지 못했습니다.')
    } finally { setBusy(null) }
  }

  const remove = async () => {
    if (!window.confirm(`${k.label} 키를 삭제할까요?\n\n.env에서 지워지며, 이 키를 쓰는 모델은 호출에 실패합니다.`)) return
    setBusy('delete'); setError(null); setResult(null)
    try { onUpdated(await deleteKey(k.id)) } catch (err) {
      setError(err instanceof Error ? err.message : '삭제하지 못했습니다.')
    } finally { setBusy(null) }
  }

  const st = k.applyStatus
  return (
    <li className="key-row">
      <div className="key-row__head">
        <div className="key-row__title">
          <span className="key-row__label">{k.label}</span>
          <span className={`badge ${k.configured ? 'badge--ok' : 'badge--muted'}`}>{k.configured ? '설정됨' : '없음'}</span>
          <code className="key-row__env">{k.env}</code>
        </div>
        <div className="key-row__actions">
          {k.configured && !editing && (
            <button className="btn btn--plain btn--sm" onClick={() => void test()} disabled={busy !== null}>
              {busy === 'test' ? '확인 중…' : '연결 확인'}
            </button>
          )}
          {!editing && (
            <button className="btn btn--outline btn--sm" onClick={() => { setEditing(true); setResult(null) }} disabled={busy !== null}>
              {k.configured ? '변경' : '입력'}
            </button>
          )}
          {k.configured && k.deletable && !editing && (
            <button className="btn btn--danger-outline btn--sm" onClick={() => void remove()} disabled={busy !== null}>
              {busy === 'delete' ? '삭제 중…' : '삭제'}
            </button>
          )}
        </div>
      </div>
      <p className="key-row__desc">{k.description}</p>
      {k.masked && !editing && <code className="key-row__masked">{k.masked}</code>}

      {editing && (
        <form className="key-row__form" onSubmit={save}>
          <input
            className="field__input key-row__input"
            type={show ? 'text' : 'password'}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={k.configured ? '새 키를 입력하면 기존 키를 바꿉니다' : `${k.env} 값`}
            aria-label={`${k.label} 키`}
            autoComplete="off"
            spellCheck={false}
            autoFocus
            disabled={busy === 'save'}
          />
          <button type="button" className="btn btn--plain btn--sm" onClick={() => setShow((v) => !v)}>
            {show ? '숨기기' : '보기'}
          </button>
          <button type="submit" className="btn btn--primary btn--sm" disabled={busy === 'save' || !value.trim()}>
            {busy === 'save' ? '저장 중…' : '저장'}
          </button>
          <button type="button" className="btn btn--plain btn--sm" onClick={() => { setEditing(false); setValue('') }}
                  disabled={busy === 'save'}>취소</button>
          <span className="key-row__apply">{APPLY_NOTE[k.apply]}</span>
        </form>
      )}

      {st?.status && (
        <p className={`key-row__status key-row__status--${st.status}`}>
          {st.status === 'running' && <span className="upgrade-job__spinner" aria-hidden />}
          {st.message}
        </p>
      )}
      {result && <p className={`key-row__status key-row__status--${result.ok ? 'done' : 'error'}`}>{result.ok ? '✓ ' : '✗ '}{result.message}</p>}
      {error && <p className="settings__error" role="alert">{error}</p>}
    </li>
  )
}
