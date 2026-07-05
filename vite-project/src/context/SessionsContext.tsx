import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { ChatMessage, Session } from '../types'
import { useAuth } from './AuthContext'
import * as sessionService from '../services/sessions'

interface SessionsContextValue {
  sessions: Session[]              // 최신순 정렬된 세션 목록
  refresh: () => void              // 저장소에서 다시 읽어 상태 갱신
  createNew: () => Session | null  // 새 세션 생성 후 반환
  append: (sessionId: string, message: ChatMessage) => void
  remove: (sessionId: string) => void
  get: (sessionId: string) => Session | null
}

const SessionsContext = createContext<SessionsContextValue | null>(null)

export function SessionsProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const ownerId = user?.id ?? null
  const [sessions, setSessions] = useState<Session[]>([])

  const refresh = useCallback(() => {
    setSessions(ownerId ? sessionService.listSessions(ownerId) : [])
  }, [ownerId])

  // 로그인 사용자 변경 시 목록 로드
  useEffect(() => {
    refresh()
  }, [refresh])

  const value = useMemo<SessionsContextValue>(
    () => ({
      sessions,
      refresh,
      createNew: () => {
        if (!ownerId) return null
        const s = sessionService.createSession(ownerId)
        refresh()
        return s
      },
      append: (sessionId, message) => {
        if (!ownerId) return
        sessionService.appendMessage(ownerId, sessionId, message)
        refresh()
      },
      remove: (sessionId) => {
        if (!ownerId) return
        sessionService.deleteSession(ownerId, sessionId)
        refresh()
      },
      get: (sessionId) => (ownerId ? sessionService.getSession(ownerId, sessionId) : null),
    }),
    [sessions, refresh, ownerId],
  )

  return <SessionsContext.Provider value={value}>{children}</SessionsContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useSessions(): SessionsContextValue {
  const ctx = useContext(SessionsContext)
  if (!ctx) throw new Error('useSessions는 SessionsProvider 안에서만 사용할 수 있습니다.')
  return ctx
}
