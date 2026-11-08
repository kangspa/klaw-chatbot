/**
 * 백엔드(FastAPI) 호출 공통 헬퍼.
 *
 * 개발 중에는 vite.config.ts의 proxy가 /auth/*, /chatbot/* → http://localhost:8008 로 전달한다.
 * 배포 시 다른 오리진을 쓰려면 VITE_API_BASE 환경변수로 override.
 *
 * 인증 없음: 개인 PC에서 띄워 쓰는 서비스라 백엔드가 모든 요청을 이 PC의 로컬 사용자로 처리한다.
 * (Google 계정은 마이 페이지에서 선택적으로 연결 — 토큰을 주고받지 않는다)
 */
export const API_BASE = import.meta.env.VITE_API_BASE ?? ''

// 예전(로그인 필수 시절) 브라우저에 남은 토큰·사용자 캐시를 정리한다
try {
  localStorage.removeItem('klaw.accessToken')
  localStorage.removeItem('klaw.user')
} catch {
  /* 저장소를 못 쓰는 환경이면 무시 */
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

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!res.ok) throw new Error(await extractDetail(res))
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}
