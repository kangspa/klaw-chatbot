import { loadGsiScript } from './gsi'
import { fetchGoogleClientId } from './auth'
import { apiFetch } from './http'

/**
 * Google Drive 백업 — Drive의 '앱 전용 숨김 폴더'(drive.appdata)에 대화·설정·API 키를 백업·복원한다.
 * 액세스 토큰은 버튼을 누를 때마다 Google에서 받아(1시간 유효) 요청에 실어 보낸다. 서버는 저장하지 않는다.
 */

export const DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.appdata'

export interface BackupInfo {
  at: number
  fileId: string
  name: string
  size: number
  includesKeys: boolean
  account: string
}

export interface BackupStatus {
  googleLinked: boolean
  email: string | null
  localBytes: Record<string, number>
  lastBackup: BackupInfo | null
  pendingRestore: { createdAt: number; includesKeys: boolean } | null
  lastRestore: { appliedAt: number; backupCreatedAt: number; replaced: string[]; includesKeys: boolean } | null
  canRestartApp: boolean
  keep: number
}

export interface DriveBackup {
  id: string
  name: string
  size: number
  createdTime: string
  includesKeys: boolean
  appVersion: string
}

/** Google에서 Drive(앱 전용 폴더) 액세스 토큰을 받는다 — 팝업이 뜰 수 있다 */
export async function getDriveToken(loginHint?: string | null): Promise<string> {
  const clientId = await fetchGoogleClientId()
  if (!clientId) throw new Error('Google 계정 연결 설정(GOOGLE_CLIENT_ID)이 없습니다.')
  await loadGsiScript()
  const oauth2 = window.google?.accounts.oauth2
  if (!oauth2) throw new Error('Google 로그인 스크립트를 불러오지 못했습니다.')
  return new Promise<string>((resolve, reject) => {
    const client = oauth2.initTokenClient({
      client_id: clientId,
      scope: DRIVE_SCOPE,
      login_hint: loginHint ?? undefined,
      callback: (r) => {
        if (r.error || !r.access_token) {
          reject(new Error(r.error === 'access_denied' ? 'Drive 권한에 동의하지 않았습니다.' : `Google 인증 실패: ${r.error_description || r.error}`))
        } else if (!(r.scope ?? '').split(' ').includes(DRIVE_SCOPE)) {
          reject(new Error('Drive 권한(앱 전용 폴더)이 허용되지 않았습니다. 다시 시도해 체크박스에 동의하세요.'))
        } else {
          resolve(r.access_token)
        }
      },
      error_callback: (e) => reject(new Error(e.type === 'popup_closed' ? 'Google 창이 닫혀 취소되었습니다.' : `Google 인증 창 오류: ${e.message || e.type}`)),
    })
    client.requestAccessToken()
  })
}

const TOKEN_TTL_MS = 50 * 60 * 1000   // Google 토큰은 1시간 — 조금 일찍 새로 받는다
let cached: { value: string; at: number } | null = null

/** 받아 둔 토큰이 있으면 다시 쓰고(팝업 없이), 없거나 오래됐으면 새로 받는다 */
export async function getCachedDriveToken(loginHint?: string | null): Promise<string> {
  if (cached && Date.now() - cached.at < TOKEN_TTL_MS) return cached.value
  const value = await getDriveToken(loginHint)
  cached = { value, at: Date.now() }
  return value
}

export function forgetDriveToken(): void {
  cached = null
}

export async function getBackupStatus(): Promise<BackupStatus> {
  return apiFetch<BackupStatus>('/chatbot/backup')
}

export async function uploadBackup(accessToken: string, includeKeys: boolean): Promise<{ backup: BackupInfo; removedOld: number }> {
  return apiFetch('/chatbot/backup/drive/upload', { method: 'POST', body: JSON.stringify({ accessToken, includeKeys }) })
}

export async function listBackups(accessToken: string): Promise<{ account: string; backups: DriveBackup[] }> {
  return apiFetch('/chatbot/backup/drive/list', { method: 'POST', body: JSON.stringify({ accessToken }) })
}

export async function deleteBackup(accessToken: string, fileId: string): Promise<void> {
  await apiFetch('/chatbot/backup/drive/delete', { method: 'POST', body: JSON.stringify({ accessToken, fileId }) })
}

export async function restoreBackup(accessToken: string, fileId: string): Promise<{ restart: 'app' | 'manual' }> {
  return apiFetch('/chatbot/backup/drive/restore', { method: 'POST', body: JSON.stringify({ accessToken, fileId }) })
}

export async function cancelRestore(): Promise<void> {
  await apiFetch('/chatbot/backup/restore/cancel', { method: 'POST' })
}
