import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import type { ChatMessage, Session } from '../types'
import { useAuth } from './AuthContext'
import * as sessionService from '../services/sessions'

interface SessionsContextValue {
  sessions: Session[]                                   // 최신 활동순 세션 목록 (메시지 포함)
  loading: boolean
  error: string | null
  refresh: () => Promise<void>                          // 서버에서 다시 읽어 상태 갱신
  createNew: (title?: string) => Promise<Session | null> // 새 세션 생성 후 반환
  remove: (sessionId: string) => Promise<void>
  rename: (sessionId: string, title: string) => Promise<void>
  get: (sessionId: string) => Session | undefined
  /** 목록에 없는 세션을 서버에서 직접 받아와 끼워 넣는다 (직접 URL 진입·탭 복원 대비) */
  upsert: (session: Session) => void
  /**
   * 서버 응답으로 받은 최신 메시지 목록을 특정 세션에 반영 (전체 refresh 없이 즉시 렌더).
   * meta로 제목/갱신시각도 함께 넘기면 사이드바 표시까지 한 번에 맞춰진다.
   */
  applyMessages: (
    sessionId: string,
    messages: ChatMessage[],
    meta?: Pick<Partial<Session>, 'title' | 'updatedAt'>,
  ) => void
}

const SessionsContext = createContext<SessionsContextValue | null>(null)

/** updatedAt 내림차순 — 서버(list_sessions)와 같은 정렬 기준 */
function sortByRecent(list: Session[]): Session[] {
  return [...list].sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
}

export function SessionsProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const ownerId = user?.id ?? null

  const [sessions, setSessions] = useState<Session[]>([])
  const [error, setError] = useState<string | null>(null)
  // 어느 사용자 기준으로 목록을 받아왔는지. loading을 별도 state로 두지 않고 여기서 파생시켜,
  // effect 안에서 동기적으로 setState를 호출하지 않도록 한다(cascading render 방지).
  const [loadedOwner, setLoadedOwner] = useState<string | null>(null)
  const loading = ownerId !== null && loadedOwner !== ownerId

  /**
   * 로컬 변경 횟수. 최초 목록 조회가 도착하기 전에 사용자가 새 세션을 만들면,
   * 뒤늦게 도착한 (그 세션이 없는) 응답이 방금 만든 세션을 지워버리는 경합이 생긴다.
   * 요청 시작 시점의 값과 비교해 그런 경우에는 덮어쓰지 않고 병합한다.
   */
  const mutationRef = useRef(0)
  const bumpMutation = () => { mutationRef.current += 1 }

  const failureMessage = (e: unknown) =>
    e instanceof Error ? e.message : '세션 목록을 불러오지 못했습니다.'

  /** 서버 목록과 로컬 상태를 병합 (로컬에만 있는 세션은 살리고, 로컬 수정본을 우선한다) */
  const mergeWithLocal = (serverList: Session[], prev: Session[]): Session[] => {
    const prevById = new Map(prev.map((s) => [s.id, s]))
    const serverIds = new Set(serverList.map((s) => s.id))
    const merged = serverList.map((s) => prevById.get(s.id) ?? s)
    const localOnly = prev.filter((s) => !serverIds.has(s.id))
    return sortByRecent([...localOnly, ...merged])
  }

  // 로그인 사용자 변경 시 목록 로드 (상태 변경은 전부 promise 콜백 안에서)
  useEffect(() => {
    let alive = true
    const startedAt = mutationRef.current
    const load = ownerId
      ? sessionService.listSessions()
      : Promise.resolve<Session[]>([])

    load.then(
      (list) => {
        if (!alive) return
        setSessions((prev) => (mutationRef.current === startedAt ? list : mergeWithLocal(list, prev)))
        setLoadedOwner(ownerId)
        setError(null)
      },
      (e: unknown) => {
        if (!alive) return
        setSessions([])
        setLoadedOwner(ownerId)  // 실패해도 로딩 상태에서는 벗어나야 한다
        setError(failureMessage(e))
      },
    )

    return () => { alive = false }
  }, [ownerId])

  /** 이벤트 핸들러에서 호출하는 수동 갱신 */
  const refresh = useCallback(async () => {
    if (!ownerId) {
      setSessions([])
      setLoadedOwner(null)
      return
    }
    try {
      const list = await sessionService.listSessions()
      setSessions((prev) => mergeWithLocal(list, prev).filter((s) => list.some((x) => x.id === s.id)))
      setLoadedOwner(ownerId)
      setError(null)
    } catch (e) {
      setError(failureMessage(e))
    }
  }, [ownerId])

  const value = useMemo<SessionsContextValue>(
    () => ({
      sessions,
      loading,
      error,
      refresh,
      createNew: async (title) => {
        if (!ownerId) return null
        const s = await sessionService.createSession(title)
        bumpMutation()
        setSessions((prev) => [{ ...s, messages: s.messages ?? [] }, ...prev])
        return s
      },
      remove: async (sessionId) => {
        await sessionService.deleteSession(sessionId)
        bumpMutation()
        setSessions((prev) => prev.filter((s) => s.id !== sessionId))
      },
      rename: async (sessionId, title) => {
        const updated = await sessionService.renameSession(sessionId, title)
        bumpMutation()
        setSessions((prev) =>
          prev.map((s) => (s.id === sessionId ? { ...s, title: updated.title } : s)),
        )
      },
      get: (sessionId) => sessions.find((s) => s.id === sessionId),
      upsert: (session) => {
        bumpMutation()
        setSessions((prev) =>
          prev.some((s) => s.id === session.id)
            ? prev.map((s) => (s.id === session.id ? { ...s, ...session } : s))
            : sortByRecent([...prev, { ...session, messages: session.messages ?? [] }]),
        )
      },
      applyMessages: (sessionId, messages, meta) => {
        bumpMutation()
        setSessions((prev) =>
          sortByRecent(
            prev.map((s) =>
              s.id === sessionId
                ? {
                    ...s,
                    messages,
                    title: meta?.title ?? s.title,
                    updatedAt: meta?.updatedAt ?? new Date().toISOString(),
                  }
                : s,
            ),
          ),
        )
      },
    }),
    [sessions, loading, error, refresh, ownerId],
  )

  return <SessionsContext.Provider value={value}>{children}</SessionsContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useSessions(): SessionsContextValue {
  const ctx = useContext(SessionsContext)
  if (!ctx) throw new Error('useSessions는 SessionsProvider 안에서만 사용할 수 있습니다.')
  return ctx
}
