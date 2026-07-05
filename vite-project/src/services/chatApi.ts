import { localIsoWithOffset } from './time'

/**
 * 실제 FastAPI 백엔드(korean-law 챗봇) 연동.
 * 개발 중에는 vite.config.ts의 proxy가 /chatbot/* → http://localhost:8008 로 전달한다.
 * 배포 시 다른 오리진을 쓰려면 VITE_API_BASE 환경변수로 override.
 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export const DEFAULT_MODEL = 'gemini/gemini-2.5-flash-lite'

export interface ChatRequest {
  userId: string
  sessionId: string
  message: string
  model?: string
}

interface ChatResponsePayload {
  response: string
}

/** POST /chatbot/chat — 사용자 메시지를 보내고 AI 답변 문자열을 받는다. */
export async function sendChat(req: ChatRequest): Promise<string> {
  const res = await fetch(`${API_BASE}/chatbot/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      userId: req.userId,
      sessionId: req.sessionId,
      isotime: localIsoWithOffset(),
      message: req.message,
      model: req.model ?? DEFAULT_MODEL,
    }),
  })

  if (!res.ok) {
    let detail = `요청 실패 (HTTP ${res.status})`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* JSON 파싱 실패는 무시하고 기본 메시지 사용 */
    }
    throw new Error(detail)
  }

  const data = (await res.json()) as ChatResponsePayload
  return data.response
}

export interface ServerHistoryMessage {
  role: string
  content: string
}

/** GET /chatbot/{sessionId}/history — 서버 체크포인터에 남은 대화 기록(있으면). */
export async function fetchHistory(sessionId: string): Promise<ServerHistoryMessage[]> {
  const res = await fetch(`${API_BASE}/chatbot/${encodeURIComponent(sessionId)}/history`)
  if (!res.ok) return []
  try {
    const data = (await res.json()) as { messages?: ServerHistoryMessage[] }
    return data.messages ?? []
  } catch {
    return []
  }
}
