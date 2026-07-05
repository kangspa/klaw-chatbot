import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { User } from '../types'
import * as authService from '../services/auth'

interface AuthContextValue {
  user: User | null
  login: (email: string, password: string) => Promise<void>
  signup: (name: string, email: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(() => authService.getCurrentUser())

  // 다른 탭에서 로그인/로그아웃 시 동기화
  useEffect(() => {
    const sync = () => setUser(authService.getCurrentUser())
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      login: async (email, password) => {
        setUser(await authService.login(email, password))
      },
      signup: async (name, email, password) => {
        setUser(await authService.signup(name, email, password))
      },
      logout: () => {
        authService.logout()
        setUser(null)
      },
    }),
    [user],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth는 AuthProvider 안에서만 사용할 수 있습니다.')
  return ctx
}
