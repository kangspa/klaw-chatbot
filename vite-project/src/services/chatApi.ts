import type { ChatMessage, Session } from '../types'
import { apiFetch } from './http'
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
  /** Postgres에 저장된 사용자 메시지 행 (id·seq 포함) */
  userMessage: ChatMessage | null
  /** Postgres에 저장된 AI 메시지 행 */
  assistantMessage: ChatMessage | null
  /**
   * 갱신된 세션 행. 첫 메시지를 보내면 백엔드가 그 내용으로 제목을 자동 생성하므로,
   * 이 값을 반영해야 사이드바 제목이 '새 대화'에 머무르지 않는다.
   */
  session: Session | null
}

/**
 * POST /chatbot/chat — 사용자 메시지를 보내고 AI 답변을 받는다.
 * 백엔드가 같은 요청 안에서 Postgres 2행 + Qdrant 1 point + LangGraph 상태를 함께 기록한다.
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

export interface ServerHistoryMessage {
  role: string
  content: string
}

/** GET /chatbot/{sessionId}/history — 세션의 대화 기록(Postgres 우선, 없으면 체크포인터). */
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
