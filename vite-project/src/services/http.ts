/**
 * 백엔드(FastAPI) 호출 공통 헬퍼.
 *
 * 개발 중에는 vite.config.ts의 proxy가 /auth/*, /chatbot/* → http://localhost:8008 로 전달한다.
 * 배포 시 다른 오리진을 쓰려면 VITE_API_BASE 환경변수로 override.
 *
 * 인증: 로그인 시 받은 액세스 토큰을 Authorization: Bearer 헤더로 자동 첨부한다.
 * 401이 오면 토큰을 폐기하고 앱 전체에 알려(unauthorized 이벤트) 로그인 화면으로 돌린다.
 */
export const API_BASE = import.meta.env.VITE_API_BASE ?? ''

const TOKEN_KEY = 'klaw.accessToken'
/** 401 발생 시 AuthContext가 구독해 로그아웃 처리하는 이벤트 이름 */
export const UNAUTHORIZED_EVENT = 'klaw:unauthorized'

export function getAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setAccessToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearAccessToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

/** FastAPI의 { detail: ... } 형식을 사람이 읽을 수 있는 문자열로 변환 */
async function extractDetail(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (body?.detail) {
      return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    }
  } catch {
    /* JSON 파싱 실패는 무시하고 기본 메시지 사용 */
  }
  return `요청 실패 (HTTP ${res.status})`
}

export interface ApiFetchInit extends RequestInit {
  /** 로그인 자체처럼 토큰이 아직 없는 요청에서 인증 헤더를 붙이지 않도록 */
  skipAuth?: boolean
}

export async function apiFetch<T>(path: string, init?: ApiFetchInit): Promise<T> {
  const { skipAuth, ...rest } = init ?? {}
  const token = skipAuth ? null : getAccessToken()

  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      ...(rest.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...rest.headers,
    },
  })

  if (res.status === 401 && !skipAuth) {
    clearAccessToken()
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
    throw new Error(await extractDetail(res))
  }

  if (!res.ok) throw new Error(await extractDetail(res))
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}
