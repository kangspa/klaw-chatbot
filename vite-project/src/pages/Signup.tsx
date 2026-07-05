import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function Signup() {
  const { signup } = useAuth()
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (password !== confirm) {
      setError('비밀번호가 일치하지 않습니다.')
      return
    }
    setBusy(true)
    try {
      await signup(name, email, password)
      navigate('/chat', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : '회원가입에 실패했습니다.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth">
      <div className="auth__card">
        <div className="auth__brand">
          <span className="auth__logo" aria-hidden>⚖️</span>
          <h1 className="auth__title">회원가입</h1>
          <p className="auth__tagline">KLaw 법률 상담을 시작해보세요</p>
        </div>

        <form className="auth__form" onSubmit={onSubmit}>
          <label className="field">
            <span className="field__label">이름</span>
            <input
              className="field__input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="홍길동"
              autoComplete="name"
              required
            />
          </label>

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
              placeholder="4자 이상"
              autoComplete="new-password"
              required
            />
          </label>

          <label className="field">
            <span className="field__label">비밀번호 확인</span>
            <input
              type="password"
              className="field__input"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="비밀번호 재입력"
              autoComplete="new-password"
              required
            />
          </label>

          {error && <p className="auth__error">{error}</p>}

          <button className="btn btn--primary btn--block btn--lg" disabled={busy}>
            {busy ? '가입 중…' : '회원가입'}
          </button>
        </form>

        <p className="auth__switch">
          이미 계정이 있으신가요? <Link to="/login">로그인</Link>
        </p>
      </div>
    </div>
  )
}
