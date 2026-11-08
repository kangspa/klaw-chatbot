import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { User } from '../types'
import * as authService from '../services/auth'

interface AuthContextValue {
  /** 이 PC의 로컬 사용자 (Google 계정이 연결돼 있으면 그 이름·사진) */
  user: User | null
  /** 부팅 시 사용자 정보를 받기 전 */
  initializing: boolean
  /** 백엔드에 연결하지 못했을 때의 오류 */
  error: string | null
  /** 사용자 정보 다시 받기 (백엔드 연결 재시도 포함) */
  refresh: () => Promise<void>
  /** Google 계정 연결 — 선택 사항 (GIS가 준 ID 토큰) */
  linkGoogle: (credential: string) => Promise<void>
  /** Google 계정 연결 해제 — 데이터는 그대로 */
  unlinkGoogle: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

/**
 * 로그인 없이 쓰는 개인 PC용 서비스라 '로그인 상태'가 없다.
 * 부팅 때 로컬 사용자 정보를 받아 두고, Google 계정 연결·해제만 다룬다.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [initializing, setInitializing] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const apply = useCallback((me: User) => { setUser(me); setError(null); setInitializing(false) }, [])
  const fail = useCallback((e: unknown) => {
    setError(e instanceof Error ? e.message : '서버에 연결하지 못했습니다.')
    setInitializing(false)
  }, [])

  useEffect(() => {
    let alive = true
    authService.fetchMe().then((me) => { if (alive) apply(me) }, (e) => { if (alive) fail(e) })
    return () => { alive = false }
  }, [apply, fail])

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      initializing,
      error,
      refresh: () => authService.fetchMe().then(apply, fail),
      linkGoogle: async (credential) => setUser(await authService.linkGoogle(credential)),
      unlinkGoogle: async () => setUser(await authService.unlinkGoogle()),
    }),
    [user, initializing, error, apply, fail],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth는 AuthProvider 안에서만 사용할 수 있습니다.')
  return ctx
}
