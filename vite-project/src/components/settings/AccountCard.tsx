import { useEffect, useState } from 'react'
import { useAuth } from '../../context/AuthContext'
import { isGoogleOriginExpected } from '../../services/auth'
import { formatWhen } from '../../services/time'
import GoogleSignInButton from '../GoogleSignInButton'

/**
 * 마이 페이지 > 계정.
 * 로그인 없이 쓰는 개인 PC용 서비스라 이 PC의 로컬 사용자 하나로 동작한다.
 * Google 계정 연결은 선택 사항 — 개인 Google 드라이브에 대화 내역·API 키·설정을 저장할 계정을 정해 둔다.
 * 연결·해제해도 사용자는 그대로라 대화·설정이 사라지지 않는다.
 */
export default function AccountCard() {
  const { user, linkGoogle, unlinkGoogle } = useAuth()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [originOk, setOriginOk] = useState(true)

  useEffect(() => {
    isGoogleOriginExpected().then((r) => setOriginOk(r.ok), () => { /* 확인 실패는 무시 */ })
  }, [])

  const link = async (credential: string) => {
    setBusy(true); setError(null)
    try { await linkGoogle(credential) } catch (e) {
      setError(e instanceof Error ? e.message : 'Google 계정을 연결하지 못했습니다.')
    } finally { setBusy(false) }
  }

  const unlink = async () => {
    if (!window.confirm('Google 계정 연결을 해제할까요?\n\n대화 내역·API 키·설정은 이 PC에 그대로 남습니다.')) return
    setBusy(true); setError(null)
    try { await unlinkGoogle() } catch (e) {
      setError(e instanceof Error ? e.message : '연결을 해제하지 못했습니다.')
    } finally { setBusy(false) }
  }

  return (
    <section className="settings__card">
      <h2 className="settings__card-title">계정</h2>
      <div className="account">
        {user?.picture ? (
          <img className="account__avatar" src={user.picture} alt="" referrerPolicy="no-referrer" />
        ) : (
          <span className="account__avatar account__avatar--initial" aria-hidden>{user?.name?.[0]?.toUpperCase() ?? '?'}</span>
        )}
        <div className="account__body">
          <div className="account__name">{user?.name}</div>
          <div className="account__email">{user?.email ?? '이 PC의 로컬 사용자 · 로그인 없이 모든 기능을 쓸 수 있습니다'}</div>
          {user?.createdAt && <div className="account__meta">처음 실행 {formatWhen(user.createdAt)}</div>}
        </div>
      </div>

      <div className="google-link">
        <div className="google-link__head">
          <span className="google-link__title">Google 계정 연결</span>
          <span className={`badge ${user?.googleLinked ? 'badge--ok' : 'badge--muted'}`}>
            {user?.googleLinked ? '연결됨' : '선택 사항'}
          </span>
        </div>
        <p className="google-link__desc">
          연결한 Google 계정의 개인 드라이브에 대화 내역·API 키·설정을 저장하는 데 씁니다.
          연결하지 않아도 채팅과 모든 설정을 그대로 쓸 수 있습니다.
        </p>
        {error && <p className="settings__error" role="alert">{error}</p>}
        {!originOk && !user?.googleLinked && (
          <p className="settings__error" role="alert">
            다른 프로그램이 KLaw 기본 포트(8008~8012)를 모두 쓰고 있어 임시 주소({window.location.host})로 열렸습니다.
            이 주소는 Google에 등록되지 않아 연결 버튼이 동작하지 않습니다. 그 프로그램을 끄고 KLaw를 다시 시작하세요.
          </p>
        )}
        {originOk && !user?.googleLinked && window.location.hostname === '127.0.0.1' && (
          <p className="model-row__hint">
            Google 로그인 버튼은 localhost 주소에서만 동작합니다.{' '}
            <a href={window.location.href.replace('//127.0.0.1', '//localhost')}>localhost 주소로 다시 열기</a>
          </p>
        )}
        {user?.googleLinked ? (
          <div className="google-link__linked">
            <span>{user.email}{user.lastLoginAt ? ` · ${formatWhen(user.lastLoginAt)} 연결` : ''}</span>
            <button className="btn btn--plain btn--sm" onClick={() => void unlink()} disabled={busy}>
              {busy ? '해제 중…' : '연결 해제'}
            </button>
          </div>
        ) : (
          <GoogleSignInButton onCredential={link} busy={busy} />
        )}
      </div>
    </section>
  )
}
