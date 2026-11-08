import type { User } from '../types'
import { apiFetch } from './http'

/**
 * 사용자·Google 계정 연결.
 *
 * 개인 PC에서 띄워 쓰는 서비스라 로그인 없이 모든 기능을 쓴다 (백엔드가 '로컬 사용자'로 처리).
 * Google 계정 연결은 선택 사항이다 — 개인 Google 드라이브에 대화·설정을 저장할 계정을 정해 두는 용도.
 * 연결해도 사용자는 바뀌지 않으므로 연결 전후로 대화·설정이 그대로 이어진다.
 */

/** GET /auth/config — 백엔드에 설정된 Google 클라이언트 ID (비밀값 아님). 설정이 없으면 null */
export async function fetchGoogleClientId(): Promise<string | null> {
  const data = await apiFetch<{ googleClientId: string | null; configured: boolean }>('/auth/config')
  return data.configured ? data.googleClientId : null
}

/** GET /auth/me — 현재(로컬) 사용자와 Google 연결 여부 */
export async function fetchMe(): Promise<User> {
  return (await apiFetch<{ user: User }>('/auth/me')).user
}

/** POST /auth/google — GIS가 준 ID 토큰으로 Google 계정 연결 */
export async function linkGoogle(credential: string): Promise<User> {
  return (await apiFetch<{ user: User }>('/auth/google', {
    method: 'POST',
    body: JSON.stringify({ credential }),
  })).user
}

/** POST /auth/google/unlink — Google 계정 연결 해제 (데이터는 그대로) */
export async function unlinkGoogle(): Promise<User> {
  const user = (await apiFetch<{ user: User }>('/auth/google/unlink', { method: 'POST' })).user
  // GIS의 자동 선택 기억을 끊어, 다음에 연결할 때 계정 선택 화면이 다시 뜨게 한다
  window.google?.accounts?.id?.disableAutoSelect?.()
  return user
}
