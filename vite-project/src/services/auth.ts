import type { User } from '../types'
import { apiFetch, clearAccessToken, setAccessToken } from './http'

/**
 * Google 계정 연동 로그인.
 *
 * 로그인 수단은 Google 하나뿐이며, 최초 로그인이 곧 회원가입이다.
 * 브라우저의 Google Identity Services(GIS)가 발급한 ID 토큰을 백엔드로 보내면,
 * 백엔드가 Google 공개키로 검증한 뒤 우리 서버가 서명한 액세스 토큰을 돌려준다.
 * 이후 모든 API 호출은 그 액세스 토큰만 사용한다 (Google 토큰은 보관하지 않는다).
 */

const USER_KEY = 'klaw.user'

export interface LoginResult {
  user: User
  isNewUser: boolean
}

// ────────────────────────── 로컬 캐시 ──────────────────────────

/** 앱 부팅 시 화면 깜빡임을 줄이기 위한 캐시. 진짜 인증 상태는 서버 토큰이 결정한다. */
export function getCachedUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

function cacheUser(user: User | null): void {
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
  else localStorage.removeItem(USER_KEY)
}

// ────────────────────────── 서버 연동 ──────────────────────────

/** GET /auth/config — 백엔드에 설정된 Google 클라이언트 ID (비밀값 아님) */
export async function fetchGoogleClientId(): Promise<string | null> {
  const data = await apiFetch<{ googleClientId: string | null; configured: boolean }>('/auth/config')
  return data.configured ? data.googleClientId : null
}

/** POST /auth/google — GIS가 준 ID 토큰으로 로그인(최초면 자동 가입) */
export async function loginWithGoogle(credential: string): Promise<LoginResult> {
  const data = await apiFetch<{
    accessToken: string
    expiresIn: number
    user: User
    isNewUser: boolean
  }>('/auth/google', {
    method: 'POST',
    body: JSON.stringify({ credential }),
    skipAuth: true,
  })

  setAccessToken(data.accessToken)
  cacheUser(data.user)
  return { user: data.user, isNewUser: data.isNewUser }
}

/** GET /auth/me — 저장된 토큰이 아직 유효한지 확인하고 최신 회원 정보를 받는다 */
export async function fetchMe(): Promise<User | null> {
  try {
    const data = await apiFetch<{ user: User }>('/auth/me')
    cacheUser(data.user)
    return data.user
  } catch {
    // 토큰 만료·삭제된 계정 등 → 로그아웃 상태로 되돌린다
    logout()
    return null
  }
}

/** 로그아웃: 토큰은 서버에 상태가 없으므로 클라이언트에서 폐기하면 끝난다 */
export function logout(): void {
  clearAccessToken()
  cacheUser(null)
  // GIS의 자동 로그인 기억을 끊어, 다음 방문 시 계정 선택 화면이 다시 뜨게 한다
  window.google?.accounts?.id?.disableAutoSelect?.()
}

/** DELETE /auth/me — 회원 탈퇴 (대화 세션·벡터 기록까지 함께 삭제) */
export async function deleteAccount(): Promise<void> {
  await apiFetch<{ deleted: boolean }>('/auth/me', { method: 'DELETE' })
  logout()
}
