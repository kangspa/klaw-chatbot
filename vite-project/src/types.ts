// 앱 전역에서 쓰는 공통 타입 정의

export interface User {
  id: string          // 로그인 식별자 (이메일)
  name: string        // 표시 이름
}

export type ChatRole = 'user' | 'assistant'

export interface ChatMessage {
  id: string
  sessionId?: string
  seq?: number
  role: ChatRole
  content: string
  model?: string | null
  /**
   * 대화 1턴(질문 + 답변)의 식별자. Qdrant의 벡터 point id와 동일한 값이며
   * 같은 턴의 user/assistant 메시지가 이 값을 공유한다.
   * 삭제·브랜치·재생성이 Postgres와 Qdrant에서 같은 범위로 적용되는 근거.
   */
  turnId?: string | null
  createdAt: string   // ISO8601
  updatedAt?: string
  /** 아직 서버에 저장되지 않은 낙관적 렌더링용 메시지 */
  pending?: boolean
}

export interface Session {
  id: string          // 세션 고유 ID (= URL의 :sessionId, 백엔드 thread_id로 사용)
  ownerId: string     // 소유 사용자 id
  title: string       // 사이드바/요약에 표시할 제목 (첫 사용자 메시지 기반 자동 생성)
  createdAt: string   // 생성 시각 ISO8601
  updatedAt: string   // 마지막 활동 시각 ISO8601 (최신순 정렬 기준)
  /** 브랜치로 만들어진 세션이면 원본 세션 id */
  parentSessionId?: string | null
  /** 브랜치 기준이 된 원본 메시지 id */
  branchedFromMessageId?: string | null
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
  isBranch: boolean
}
