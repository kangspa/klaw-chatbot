import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import GoogleSignInButton from '../components/GoogleSignInButton'

/**
 * 로그인 = 회원가입.
 * Google 계정 연동만 지원하므로 별도의 가입 절차나 비밀번호 입력이 없다.
 * 처음 로그인하는 계정이면 백엔드가 users 테이블에 회원을 자동으로 만든다.
 */
export default function Login() {
  const { loginWithGoogle } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/chat'

  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const handleCredential = async (credential: string) => {
    setError(null)
    setBusy(true)
    try {
      await loginWithGoogle(credential)
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

        <p className="auth__lead">
          Google 계정으로 시작하세요.<br />
          처음이시라면 로그인과 동시에 자동으로 가입됩니다.
        </p>

        {error && <p className="auth__error">{error}</p>}

        <GoogleSignInButton onCredential={handleCredential} busy={busy} />

        <p className="auth__legal">
          로그인하면 Google 계정의 이름·이메일·프로필 사진을 받아 회원 정보로 저장합니다.
          대화 기록은 회원 계정에 연결되어 보관됩니다.
        </p>
      </div>
    </div>
  )
}
