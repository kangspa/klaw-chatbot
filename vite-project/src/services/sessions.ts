import type { ChatMessage, Session, SessionSummary } from '../types'

/**
 * localStorage 기반 목업 세션 저장소 (사용자별).
 *
 * 세션 목록/메시지/요약을 브라우저에 보관한다. 챗봇 '응답'만 실제 백엔드(chatApi)를 쓰고,
 * 세션 메타/히스토리는 여기서 관리한다. 실제 API가 준비되면 이 파일만 교체하면 된다.
 *
 * 백엔드 MemorySaver는 서버 재시작 시 휘발되므로, 프론트 표시는 이 로컬 사본을 진실원본으로 삼는다.
 */

const keyFor = (ownerId: string) => `klaw.sessions.${ownerId}`

function readAll(ownerId: string): Session[] {
  try {
    return JSON.parse(localStorage.getItem(keyFor(ownerId)) ?? '[]') as Session[]
  } catch {
    return []
  }
}

function writeAll(ownerId: string, sessions: Session[]): void {
  localStorage.setItem(keyFor(ownerId), JSON.stringify(sessions))
}

function newId(): string {
  // 최신 브라우저는 crypto.randomUUID 지원. 폴백 포함.
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
}

/** 최신 활동순(updatedAt 내림차순) 세션 목록 */
export function listSessions(ownerId: string): Session[] {
  return readAll(ownerId).sort(
    (a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime(),
  )
}

export function getSession(ownerId: string, id: string): Session | null {
  return readAll(ownerId).find((s) => s.id === id) ?? null
}

export function createSession(ownerId: string): Session {
  const now = new Date().toISOString()
  const session: Session = {
    id: newId(),
    ownerId,
    title: '새 대화',
    createdAt: now,
    updatedAt: now,
    messages: [],
  }
  const all = readAll(ownerId)
  all.push(session)
  writeAll(ownerId, all)
  return session
}

/** 메시지 추가 + updatedAt 갱신 + (첫 사용자 메시지면) 제목 자동 생성 */
export function appendMessage(ownerId: string, id: string, message: ChatMessage): Session | null {
  const all = readAll(ownerId)
  const idx = all.findIndex((s) => s.id === id)
  if (idx === -1) return null

  const session = all[idx]
  session.messages.push(message)
  session.updatedAt = message.createdAt

  // 제목이 기본값이고 이번이 사용자 메시지면 제목 생성
  if (session.title === '새 대화' && message.role === 'user') {
    session.title = message.content.trim().slice(0, 40) || '새 대화'
  }

  all[idx] = session
  writeAll(ownerId, all)
  return session
}

export function renameSession(ownerId: string, id: string, title: string): void {
  const all = readAll(ownerId)
  const idx = all.findIndex((s) => s.id === id)
  if (idx === -1) return
  all[idx].title = title.trim() || all[idx].title
  writeAll(ownerId, all)
}

export function deleteSession(ownerId: string, id: string): void {
  writeAll(ownerId, readAll(ownerId).filter((s) => s.id !== id))
}

/** 요약 페이지용: 세션을 한눈에 보기 좋은 요약 카드로 변환 */
export function toSummary(session: Session): SessionSummary {
  const firstUser = session.messages.find((m) => m.role === 'user')
  const last = session.messages[session.messages.length - 1]
  return {
    id: session.id,
    title: session.title,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    messageCount: session.messages.length,
    firstUserMessage: firstUser?.content ?? '(아직 질문이 없습니다)',
    lastMessage: last?.content ?? '',
  }
}

export function summarizeAll(ownerId: string): SessionSummary[] {
  return listSessions(ownerId).map(toSummary)
}
