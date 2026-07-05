// 앱 전역에서 쓰는 공통 타입 정의

export interface User {
  id: string          // 로그인 식별자 (이메일)
  name: string        // 표시 이름
}

export type ChatRole = 'user' | 'assistant'

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  createdAt: string   // ISO8601
}

export interface Session {
  id: string          // 세션 고유 ID (= URL의 :sessionId, 백엔드 thread_id로 사용)
  ownerId: string     // 소유 사용자 id
  title: string       // 사이드바/요약에 표시할 제목 (첫 사용자 메시지 기반 자동 생성)
  createdAt: string   // 생성 시각 ISO8601
  updatedAt: string   // 마지막 활동 시각 ISO8601 (최신순 정렬 기준)
  messages: ChatMessage[]
}

/** 요약 페이지에서 사용하는 세션 요약 카드 데이터 */
export interface SessionSummary {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  messageCount: number
  firstUserMessage: string
  lastMessage: string
}
