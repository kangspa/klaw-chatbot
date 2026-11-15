import { useCallback, useEffect, useState } from 'react'
import {
  cancelRestore, deleteBackup, forgetDriveToken, getBackupStatus, getCachedDriveToken, listBackups, restoreBackup, uploadBackup,
  type BackupStatus, type DriveBackup,
} from '../../services/backup'
import { formatWhen } from '../../services/time'

const fmt = (b: number) => (b >= 1e6 ? `${(b / 1e6).toFixed(1)}MB` : `${Math.max(1, Math.round(b / 1e3))}KB`)
const when = (sec: number) => formatWhen(new Date(sec * 1000).toISOString())

/**
 * Google Drive 백업 — 대화·설정·API 키를 연결한 Google 계정 Drive의 '앱 전용 숨김 폴더'에 백업하고 복원한다.
 * 숨김 폴더는 Drive 화면에 보이지 않고 이 앱만 읽고 쓸 수 있다 (사용자의 다른 파일은 볼 수 없다).
 */
export default function BackupCard() {
  const [status, setStatus] = useState<BackupStatus | null>(null)
  const [backups, setBackups] = useState<DriveBackup[] | null>(null)
  const [includeKeys, setIncludeKeys] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [restarting, setRestarting] = useState(false)

  const load = useCallback(async () => {
    try { setStatus(await getBackupStatus()) } catch (e) { setError(e instanceof Error ? e.message : '백업 상태를 불러오지 못했습니다.') }
  }, [])

  useEffect(() => {
    let alive = true
    getBackupStatus().then((s) => { if (alive) setStatus(s) }, (e) => { if (alive) setError(e instanceof Error ? e.message : '백업 상태를 불러오지 못했습니다.') })
    return () => { alive = false }
  }, [])

  // 복원 후 앱이 다시 뜰 때까지 기다렸다가 화면을 새로 고친다
  useEffect(() => {
    if (!restarting) return
    let seenDown = false
    const timer = window.setInterval(async () => {
      try {
        await getBackupStatus()
        if (seenDown) window.location.reload()
      } catch {
        seenDown = true
      }
    }, 2000)
    return () => window.clearInterval(timer)
  }, [restarting])

  const run = async (label: string, fn: (t: string) => Promise<void>) => {
    setBusy(label); setError(null); setNotice(null)
    try {
      await fn(await getCachedDriveToken(status?.email))
    } catch (e) {
      const msg = e instanceof Error ? e.message : '실패했습니다.'
      if (msg.includes('만료') || msg.includes('권한')) forgetDriveToken()
      setError(msg)
    } finally {
      setBusy(null)
    }
  }

  const backupNow = () => run('upload', async (t) => {
    const r = await uploadBackup(t, includeKeys)
    setNotice(`백업했습니다 (${fmt(r.backup.size)}${r.removedOld ? ` · 오래된 백업 ${r.removedOld}개 정리` : ''}).`)
    await load()
    if (backups) setBackups((await listBackups(t)).backups)
  })

  const showList = () => run('list', async (t) => setBackups((await listBackups(t)).backups))

  const restore = (b: DriveBackup) => {
    const msg = `이 백업으로 복원할까요?\n\n${new Date(b.createdTime).toLocaleString()} · ${fmt(b.size)}${b.includesKeys ? ' · API 키 포함' : ''}\n\n`
      + '· 이 PC의 대화 기록·설정' + (b.includesKeys ? '·API 키' : '') + '가 백업 시점으로 바뀝니다.\n'
      + '· 지금 데이터는 한 번 되돌릴 수 있게 앱 폴더(.runtime/restore_previous)에 보관됩니다.\n'
      + (status?.canRestartApp ? '· 앱이 잠시 다시 시작됩니다.' : '· 적용하려면 서버를 다시 시작해야 합니다 (개발 실행).')
    if (!window.confirm(msg)) return
    void run('restore', async (t) => {
      const r = await restoreBackup(t, b.id)
      if (r.restart === 'app') {
        setNotice('복원 파일을 받았습니다. 앱을 다시 시작하는 중… 끝나면 화면이 새로 고쳐집니다.')
        setRestarting(true)
      } else {
        setNotice('복원 파일을 받았습니다. 서버(main.py)를 다시 시작하면 적용됩니다.')
        await load()
      }
    })
  }

  const remove = (b: DriveBackup) => {
    if (!window.confirm(`Drive에서 이 백업을 지울까요?\n${new Date(b.createdTime).toLocaleString()} · ${fmt(b.size)}`)) return
    void run('delete', async (t) => {
      await deleteBackup(t, b.id)
      setBackups((cur) => cur?.filter((x) => x.id !== b.id) ?? null)
    })
  }

  if (!status) {
    return (
      <section className="settings__card">
        <div className="settings__card-head"><h2 className="settings__card-title">Google Drive 백업</h2></div>
        {error ? <p className="settings__error">{error}</p> : <p className="settings__empty">불러오는 중…</p>}
      </section>
    )
  }

  const local = Object.values(status.localBytes).reduce((a, b) => a + b, 0)

  return (
    <section className="settings__card">
      <div className="settings__card-head">
        <h2 className="settings__card-title">Google Drive 백업</h2>
        <p className="settings__card-desc">
          대화 기록·설정·API 키를 연결한 Google 계정 Drive의 <b>앱 전용 숨김 폴더</b>에 백업합니다.
          Drive 화면에는 보이지 않고 이 앱만 읽고 쓸 수 있으며, Drive의 다른 파일은 볼 수 없습니다. 최근 {status.keep}개를 보관합니다.
        </p>
      </div>

      {!status.googleLinked ? (
        <p className="settings__empty">위 '계정' 카드에서 Google 계정을 연결하면 쓸 수 있습니다.</p>
      ) : (
        <>
          <div className="backup-summary">
            <span>계정: <b>{status.email}</b></span>
            <span>이 PC 데이터: {fmt(local)}</span>
            <span>마지막 백업: {status.lastBackup ? `${when(status.lastBackup.at)} (${fmt(status.lastBackup.size)}${status.lastBackup.includesKeys ? ', 키 포함' : ''})` : '없음'}</span>
          </div>
          {status.lastRestore && (
            <p className="model-row__hint">
              {when(status.lastRestore.appliedAt)}에 {when(status.lastRestore.backupCreatedAt)} 백업으로 복원했습니다.
            </p>
          )}
          {status.pendingRestore && (
            <div className="runtime-banner">
              <span>받아 둔 복원이 있습니다 — 다음 시작 때 적용됩니다.</span>
              <button className="btn btn--sm" onClick={() => void cancelRestore().then(load)} disabled={!!busy}>취소</button>
            </div>
          )}

          <label className="uninstall-option">
            <input type="checkbox" checked={includeKeys} onChange={(e) => setIncludeKeys(e.target.checked)} />
            <span>API 키도 함께 백업 <small>(다른 PC에서 복원하면 키를 다시 넣지 않아도 됩니다)</small></span>
          </label>

          <div className="runtime-actions">
            <button className="btn" onClick={() => void showList()} disabled={!!busy || restarting}>
              {busy === 'list' ? '불러오는 중…' : '백업 목록 · 복원'}
            </button>
            <button className="btn btn--primary" onClick={() => void backupNow()} disabled={!!busy || restarting}>
              {busy === 'upload' ? '백업하는 중…' : '지금 백업'}
            </button>
          </div>

          {backups && (
            backups.length === 0 ? <p className="settings__empty">Drive에 백업이 없습니다.</p> : (
              <ul className="uninstall-list">
                {backups.map((b) => (
                  <li key={b.id}>
                    <span>
                      {new Date(b.createdTime).toLocaleString()} · {fmt(b.size)}{b.includesKeys ? ' · 키 포함' : ''}
                    </span>
                    <span className="backup-actions">
                      <button className="btn btn--sm" onClick={() => restore(b)} disabled={!!busy || restarting}>복원</button>
                      <button className="btn btn--sm btn--danger-outline" onClick={() => remove(b)} disabled={!!busy || restarting}>삭제</button>
                    </span>
                  </li>
                ))}
              </ul>
            )
          )}
        </>
      )}
      {notice && <p className="model-row__hint" role="status">{notice}</p>}
      {error && <p className="settings__error" role="alert">{error}</p>}
    </section>
  )
}
