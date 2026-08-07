import type { ChatMessage, Session, SessionSummary } from '../types'
import { apiFetch } from './http'

/**
 * 세션/메시지 저장소 클라이언트 (백엔드 Postgres 연동).
 *
 * 이전 버전은 localStorage 목업이었지만, 이제 대화 원문의 진실 원본은 Postgres다.
 * 백엔드는 하나의 요청으로 Postgres(원문) · Qdrant(벡터) · LangGraph 상태를
 * 동시에 맞춰주므로, 프론트는 응답으로 받은 최신 목록을 그대로 반영하기만 하면 된다.
 */

const BASE = '/chatbot/sessions'

/** 사용자의 세션 목록 (메시지 포함, 최근 활동순) */
export async function listSessions(ownerId: string): Promise<Session[]> {
  const data = await apiFetch<{ sessions: Session[] }>(
    `${BASE}?userId=${encodeURIComponent(ownerId)}`,
  )
  return data.sessions ?? []
}

export async function getSession(id: string): Promise<Session> {
  return apiFetch<Session>(`${BASE}/${encodeURIComponent(id)}`)
}

export async function createSession(ownerId: string, title?: string): Promise<Session> {
  return apiFetch<Session>(BASE, {
    method: 'POST',
    body: JSON.stringify({ userId: ownerId, title }),
  })
}

export async function renameSession(id: string, title: string): Promise<Session> {
  return apiFetch<Session>(`${BASE}/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

/** 세션 삭제: Postgres 행 + Qdrant의 해당 thread 벡터 + LangGraph 상태를 모두 정리한다. */
export async function deleteSession(id: string): Promise<void> {
  await apiFetch<{ deleted: boolean }>(`${BASE}/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

// ────────────────────── 메시지 단위 조작 (챗봇 답변의 3개 버튼) ──────────────────────

export interface DeleteMessageResult {
  turnId: string | null
  removedMessageIds: string[]
  messages: ChatMessage[]
}

/**
 * ① 삭제 — 선택한 답변과 짝이 되는 질문(= 같은 turn)을 세 저장소에서 모두 제거한다.
 * Qdrant는 질문+답변을 point 1개로 저장하므로 삭제 단위도 턴 단위다.
 */
export async function deleteMessage(sessionId: string, messageId: string): Promise<DeleteMessageResult> {
  return apiFetch<DeleteMessageResult>(
    `${BASE}/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}`,
    { method: 'DELETE' },
  )
}

export interface BranchResult {
  session: Session
  copiedMessages: number
  copiedVectorPoints: number
}

/**
 * ② 브랜치 — 이 메시지까지의 대화를 물려받는 새 세션을 만든다.
 * Qdrant의 대화 벡터도 새 thread_id로 복제되므로, 브랜치에서도 과거 대화 검색이 그대로 동작한다.
 */
export async function branchFromMessage(
  sessionId: string,
  messageId: string,
  ownerId: string,
  title?: string,
): Promise<BranchResult> {
  return apiFetch<BranchResult>(
    `${BASE}/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/branch`,
    { method: 'POST', body: JSON.stringify({ userId: ownerId, title }) },
  )
}

export interface RegenerateResult {
  turnId: string
  assistantMessage: ChatMessage
  messages: ChatMessage[]
}

/**
 * ③ 다시 실행 — 같은 질문으로 답변을 새로 생성한다.
 * 같은 turn_id로 다시 저장되므로 Qdrant의 기존 벡터도 새 답변으로 덮어써진다.
 */
export async function regenerateMessage(
  sessionId: string,
  messageId: string,
  ownerId: string,
  isotime: string,
  model?: string,
): Promise<RegenerateResult> {
  return apiFetch<RegenerateResult>(
    `${BASE}/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/regenerate`,
    { method: 'POST', body: JSON.stringify({ userId: ownerId, isotime, model }) },
  )
}

// ────────────────────────────── 표시용 유틸 ──────────────────────────────

/** 요약 페이지용: 세션을 한눈에 보기 좋은 요약 카드로 변환 */
export function toSummary(session: Session): SessionSummary {
  const messages = session.messages ?? []
  const firstUser = messages.find((m) => m.role === 'user')
  const last = messages[messages.length - 1]
  return {
    id: session.id,
    title: session.title,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    messageCount: messages.length,
    firstUserMessage: firstUser?.content ?? '(아직 질문이 없습니다)',
    lastMessage: last?.content ?? '',
    isBranch: Boolean(session.parentSessionId),
  }
}
