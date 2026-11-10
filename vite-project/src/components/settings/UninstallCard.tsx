import { useEffect, useState } from 'react'
import { getStoragePlan, uninstallApp, type StoragePlan } from '../../services/appData'

const CONFIRM_WORD = '삭제'

const fmt = (bytes: number) =>
  bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)}GB` : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(0)}MB` : `${Math.max(1, Math.round(bytes / 1e3))}KB`

/**
 * 앱 데이터 모두 삭제 — 무설치 앱이 실행하면서 받아 둔 모든 것(대화·설정·API 키·Ollama 엔진과 모델·임베딩·OCR 모델·
 * 로그·임시 폴더)을 지우고 앱을 끈다. 설치형 프로그램의 '제거'와 같다.
 */
export default function UninstallCard() {
  const [plan, setPlan] = useState<StoragePlan | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [word, setWord] = useState('')
  const [includeShared, setIncludeShared] = useState(false)
  const [deleteExe, setDeleteExe] = useState(false)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)

  useEffect(() => {
    if (!open) return
    let alive = true
    getStoragePlan().then((p) => { if (alive) setPlan(p) }, (e) => { if (alive) setError(e instanceof Error ? e.message : '목록을 불러오지 못했습니다.') })
    return () => { alive = false }
  }, [open])

  const sharedWithData = plan?.shared.filter((s) => s.kind === 'shared') ?? []
  const total = (plan?.totalBytes ?? 0) + (includeShared ? sharedWithData.reduce((a, s) => a + s.bytes, 0) : 0)

  const run = async () => {
    if (word.trim() !== CONFIRM_WORD) return
    setBusy(true)
    setError(null)
    try {
      await uninstallApp({ confirm: word.trim(), includeShared, deleteExe })
      try { localStorage.clear(); sessionStorage.clear() } catch { /* 브라우저 저장소를 못 지워도 진행 */ }
      setDone(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : '삭제를 시작하지 못했습니다.')
      setBusy(false)
    }
  }

  if (done) {
    return (
      <div className="uninstall-done" role="alert">
        <div className="uninstall-done__box">
          <h2>KLaw를 제거하는 중입니다</h2>
          <p>앱이 종료되고 잠시 뒤 데이터가 지워집니다. 끝나면 알림 창이 뜹니다.</p>
          <p>이 브라우저 창은 닫아도 됩니다.</p>
        </div>
      </div>
    )
  }

  return (
    <section className="settings__card settings__card--danger">
      <div className="settings__card-head">
        <h2 className="settings__card-title">앱 데이터 모두 삭제</h2>
        <p className="settings__card-desc">
          KLaw가 이 PC에 받아 두거나 만든 모든 것을 지우고 앱을 종료합니다 (설치형 프로그램의 '제거'와 같습니다).
          대화 기록·설정·API 키도 함께 지워지며 되돌릴 수 없습니다.
        </p>
      </div>

      {!open ? (
        <div className="runtime-actions">
          <button className="btn btn--danger-outline" onClick={() => setOpen(true)}>삭제할 항목 보기</button>
        </div>
      ) : (
        <>
          {error && <p className="settings__error" role="alert">{error}</p>}
          {!plan && !error && <p className="settings__empty">불러오는 중…</p>}
          {plan && (
            <>
              <ul className="uninstall-list">
                {plan.items.map((i) => (
                  <li key={i.path} title={i.path}>
                    <span>{i.label}</span>
                    <span className="uninstall-list__size">{fmt(i.bytes)}</span>
                  </li>
                ))}
              </ul>
              <p className="model-row__hint">앱 폴더: <code>{plan.home}</code>{!plan.frozen && ' (개발 실행 — data/·logs/·.runtime/만 지웁니다)'}</p>

              {sharedWithData.length > 0 && (
                <label className="uninstall-option">
                  <input type="checkbox" checked={includeShared} onChange={(e) => setIncludeShared(e.target.checked)} />
                  <span>
                    사용자 폴더의 라이브러리 캐시도 지우기 ({sharedWithData.map((s) => `${s.label} ${fmt(s.bytes)}`).join(', ')})
                    <br /><small>예전 버전이 만든 것으로, 다른 프로그램도 쓸 수 있는 공용 폴더입니다. 비어 있는 것은 자동으로 지웁니다.</small>
                  </span>
                </label>
              )}
              {plan.exePath && (
                <label className="uninstall-option">
                  <input type="checkbox" checked={deleteExe} onChange={(e) => setDeleteExe(e.target.checked)} />
                  <span>KLaw.exe 파일도 지우기 <small>({plan.exePath})</small></span>
                </label>
              )}

              <p className="uninstall-total">지울 용량: <strong>{fmt(total)}</strong></p>
              <div className="uninstall-confirm">
                <input
                  className="uninstall-confirm__input"
                  value={word}
                  onChange={(e) => setWord(e.target.value)}
                  placeholder={`확인하려면 '${CONFIRM_WORD}' 입력`}
                  aria-label="확인 문구"
                  disabled={busy}
                />
                <button className="btn btn--danger-outline" onClick={() => void run()} disabled={busy || word.trim() !== CONFIRM_WORD}>
                  {busy ? '시작하는 중…' : '모두 삭제하고 종료'}
                </button>
                <button className="btn" onClick={() => { setOpen(false); setWord('') }} disabled={busy}>취소</button>
              </div>
            </>
          )}
        </>
      )}
    </section>
  )
}
