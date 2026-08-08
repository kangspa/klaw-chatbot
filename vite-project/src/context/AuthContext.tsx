import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { User } from '../types'
import * as authService from '../services/auth'
import { getAccessToken, UNAUTHORIZED_EVENT } from '../services/http'

interface AuthContextValue {
  user: User | null
  /** 부팅 시 저장된 토큰 검증이 끝나기 전 상태 (깜빡임 방지용) */
  initializing: boolean
  /** Google Identity Services가 준 ID 토큰으로 로그인 (최초면 자동 가입) */
  loginWithGoogle: (credential: string) => Promise<void>
  logout: () => void
  deleteAccount: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  // 캐시된 사용자로 먼저 그리고, 아래 effect에서 서버 검증 결과로 확정한다
  const [user, setUser] = useState<User | null>(() =>
    getAccessToken() ? authService.getCachedUser() : null,
  )
  const [initializing, setInitializing] = useState(() => Boolean(getAccessToken()))

  // 저장된 토큰이 아직 유효한지 서버에 확인 (만료·계정 삭제 시 로그아웃)
  useEffect(() => {
    if (!getAccessToken()) return

    let alive = true
    authService.fetchMe().then(
      (me) => { if (alive) { setUser(me); setInitializing(false) } },
      () => { if (alive) { setUser(null); setInitializing(false) } },
    )
    return () => { alive = false }
  }, [])

  // API 호출 중 401이 나면(토큰 만료 등) 즉시 로그아웃 상태로 전환
  useEffect(() => {
    const onUnauthorized = () => setUser(null)
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  // 다른 탭에서 로그인/로그아웃 시 동기화
  useEffect(() => {
    const sync = () => setUser(getAccessToken() ? authService.getCachedUser() : null)
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      initializing,
      loginWithGoogle: async (credential) => {
        const { user: signedIn } = await authService.loginWithGoogle(credential)
        setUser(signedIn)
      },
      logout: () => {
        authService.logout()
        setUser(null)
      },
      deleteAccount: async () => {
        await authService.deleteAccount()
        setUser(null)
      },
    }),
    [user, initializing],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth는 AuthProvider 안에서만 사용할 수 있습니다.')
  return ctx
}
