import { useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/chat'

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await login(email, password)
      navigate(from, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : '로그인에 실패했습니다.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth">
      <div className="auth__card">
        <div className="auth__brand">
          <span className="auth__logo" aria-hidden>⚖️</span>
          <h1 className="auth__title">KLaw</h1>
          <p className="auth__tagline">국내법 상담 AI 챗봇</p>
        </div>

        <form className="auth__form" onSubmit={onSubmit}>
          <label className="field">
            <span className="field__label">이메일</span>
            <input
              type="email"
              className="field__input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="username"
              required
            />
          </label>

          <label className="field">
            <span className="field__label">비밀번호</span>
            <input
              type="password"
              className="field__input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete="current-password"
              required
            />
          </label>

          {error && <p className="auth__error">{error}</p>}

          <button className="btn btn--primary btn--block btn--lg" disabled={busy}>
            {busy ? '로그인 중…' : '로그인'}
          </button>
        </form>

        <p className="auth__switch">
          아직 계정이 없으신가요? <Link to="/signup">회원가입</Link>
        </p>
      </div>
    </div>
  )
}
