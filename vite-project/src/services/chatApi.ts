import type { ChatMessage, Session } from '../types'
import { API_BASE, apiFetch } from './http'
import { localIsoWithOffset } from './time'

/**
 * 챗봇 대화 턴 전송/조회.
 * 세션·메시지 CRUD와 삭제/브랜치/재생성은 services/sessions.ts에 있다.
 */

export interface ChatRequest {
  sessionId: string
  message: string
  /** 비우면 서버 기본 모델(DB llm_models.is_default)을 쓴다 */
  model?: string
}

export interface ChatResult {
  /** AI 응답 텍스트 */
  response: string
  /** 이번 턴의 turn_id (= Qdrant point id) */
  turnId: string | null
  /** DB에 저장된 사용자 메시지 행 (id·seq 포함) */
  userMessage: ChatMessage | null
  /** DB에 저장된 AI 메시지 행 */
  assistantMessage: ChatMessage | null
  /**
   * 갱신된 세션 행. 첫 메시지를 보내면 백엔드가 그 내용으로 제목을 자동 생성하므로,
   * 이 값을 반영해야 사이드바 제목이 '새 대화'에 머무르지 않는다.
   */
  session: Session | null
}

/**
 * POST /chatbot/chat — 사용자 메시지를 보내고 AI 답변을 받는다.
 * 백엔드가 같은 요청 안에서 DB 2행 + Qdrant 1 point + LangGraph 상태를 함께 기록한다.
 */
export async function sendChat(req: ChatRequest): Promise<ChatResult> {
  const data = await apiFetch<{
    response: string
    turnId?: string | null
    userMessage?: ChatMessage | null
    assistantMessage?: ChatMessage | null
    session?: Session | null
  }>('/chatbot/chat', {
    method: 'POST',
    body: JSON.stringify({
      sessionId: req.sessionId,
      isotime: localIsoWithOffset(),
      message: req.message,
      model: req.model,
    }),
  })

  return {
    response: data.response,
    turnId: data.turnId ?? null,
    userMessage: data.userMessage ?? null,
    assistantMessage: data.assistantMessage ?? null,
    session: data.session ?? null,
  }
}

/* ────────────── 스트리밍 (POST /chatbot/chat/stream, GET /chatbot/chat/live/…) ────────────── */

/** 답변 진행 상황을 받는 콜백. 서버에서 답변은 화면 연결과 무관하게 끝까지 만들어진다 */
export interface StreamHandlers {
  /** 구독을 시작하면 먼저 한 번 — 지금까지 만든 답변 전체 (다른 페이지에 다녀온 경우 이어 붙일 내용) */
  onSnapshot?: (s: { userMessage: { id: string; content: string }; text: string; step: string }) => void
  onStatus?: (step: string) => void
  onToken?: (text: string) => void
  /** 답변 단계가 다시 시작됨 (도구를 쓰고 돌아온 경우) — 지금까지 받은 조각을 지운다 */
  onReset?: () => void
  onDone?: (r: ChatResult) => void
  /** kind: auth(API 키 문제) · quota(사용 한도) · other — AI 말풍선처럼 보여 줄 안내문 */
  onError?: (e: { detail: string; kind: string }) => void
}

export class ChatHttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function readSse(res: Response, h: StreamHandlers): Promise<void> {
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return
    buf += decoder.decode(value, { stream: true })
    let i: number
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i)
      buf = buf.slice(i + 2)
      let event = 'message'
      const data: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event: ')) event = line.slice(7)
        else if (line.startsWith('data: ')) data.push(line.slice(6))
      }
      if (!data.length) continue   // keep-alive 주석
      const d = JSON.parse(data.join('\n'))
      if (event === 'snapshot') h.onSnapshot?.(d)
      else if (event === 'status') h.onStatus?.(d.step)
      else if (event === 'token') h.onToken?.(d.text)
      else if (event === 'reset') h.onReset?.()
      else if (event === 'done') {
        h.onDone?.({ response: d.response, turnId: d.turnId ?? null, userMessage: d.userMessage ?? null,
                     assistantMessage: d.assistantMessage ?? null, session: d.session ?? null })
        return
      } else if (event === 'error') {
        h.onError?.(d)
        return
      }
    }
  }
}

async function failure(res: Response): Promise<ChatHttpError> {
  let detail = `요청 실패 (HTTP ${res.status})`
  try {
    const body = await res.json()
    if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch { /* 기본 문구 */ }
  return new ChatHttpError(res.status, detail)
}

/** 질문을 보내고 답변을 토큰 단위로 받는다. 400(키 없음 등)·409(이미 답변 중)는 ChatHttpError */
export async function streamChat(req: ChatRequest, h: StreamHandlers, signal?: AbortSignal): Promise<void> {
  const res = await fetch(`${API_BASE}/chatbot/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId: req.sessionId, isotime: localIsoWithOffset(), message: req.message, model: req.model }),
    signal,
  })
  if (!res.ok) throw await failure(res)
  await readSse(res, h)
}

export interface LiveTurn {
  active: boolean
  status?: 'running' | 'done' | 'error'
  step?: string
  text?: string
  userMessage?: { id: string; content: string }
  result?: ChatResult | null
  error?: { detail: string; kind: string } | null
}

/** 이 세션에서 지금 만들고 있는(또는 방금 끝난) 답변 */
export async function getLiveTurn(sessionId: string): Promise<LiveTurn> {
  return apiFetch<LiveTurn>(`/chatbot/chat/live/${encodeURIComponent(sessionId)}`)
}

/** 진행 중인 답변에 다시 붙는다 (다른 페이지에 다녀온 경우) */
export async function followLiveTurn(sessionId: string, h: StreamHandlers, signal?: AbortSignal): Promise<void> {
  const res = await fetch(`${API_BASE}/chatbot/chat/live/${encodeURIComponent(sessionId)}/events`, { signal })
  if (!res.ok) throw await failure(res)
  await readSse(res, h)
}

export interface ServerHistoryMessage {
  role: string
  content: string
}

/** GET /chatbot/{sessionId}/history — 세션의 대화 기록(DB 우선, 없으면 체크포인터). */
export async function fetchHistory(sessionId: string): Promise<ServerHistoryMessage[]> {
  try {
    const data = await apiFetch<{ messages?: ServerHistoryMessage[] }>(
      `/chatbot/${encodeURIComponent(sessionId)}/history`,
    )
    return data.messages ?? []
  } catch {
    return []
  }
}
