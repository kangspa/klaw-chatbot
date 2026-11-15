import { useEffect, useRef, useState } from 'react'
import { fetchGoogleClientId } from '../services/auth'
import { loadGsiScript } from '../services/gsi'

interface GoogleSignInButtonProps {
  /** GIS가 발급한 ID 토큰(credential)을 받아 로그인 처리 */
  onCredential: (credential: string) => void | Promise<void>
  /** 로그인 진행 중이면 버튼을 가린다 */
  busy?: boolean
}

/**
 * Google 계정 연결 버튼 (선택 사항 — 마이 페이지).
 *
 * 클라이언트 ID는 백엔드(/auth/config)에서 받아온다. 프론트 빌드에 값을 심지 않아도 되고,
 * 서버와 프론트가 서로 다른 클라이언트 ID를 보는 상황도 생기지 않는다.
 * (클라이언트 ID는 비밀값이 아니라 브라우저에 노출되는 것이 정상이다)
 */
export default function GoogleSignInButton({ onCredential, busy }: GoogleSignInButtonProps) {
  const holderRef = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [message, setMessage] = useState<string | null>(null)

  // 최신 콜백을 ref로 유지 — GIS initialize는 1회만 하고 싶기 때문에
  // 콜백이 바뀔 때마다 재초기화하지 않고 ref를 통해 최신 함수를 호출한다.
  const callbackRef = useRef(onCredential)
  useEffect(() => {
    callbackRef.current = onCredential
  }, [onCredential])

  useEffect(() => {
    let alive = true

    const setup = async () => {
      const clientId = await fetchGoogleClientId()
      if (!alive) return
      if (!clientId) {
        throw new Error(
          'Google 계정 연결이 설정되지 않았습니다. .env의 GOOGLE_CLIENT_ID를 지정한 뒤 서버를 재시작하세요.',
        )
      }

      await loadGsiScript()
      if (!alive) return

      const gis = window.google?.accounts?.id
      if (!gis || !holderRef.current) throw new Error('Google 로그인 초기화에 실패했습니다.')

      gis.initialize({
        client_id: clientId,
        callback: (res) => { void callbackRef.current(res.credential) },
        cancel_on_tap_outside: true,
        use_fedcm_for_prompt: true,
      })
      gis.renderButton(holderRef.current, {
        type: 'standard',
        theme: 'outline',
        size: 'large',
        text: 'signin_with',
        shape: 'pill',
        logo_alignment: 'left',
        width: 320,
        locale: 'ko',
      })
      setStatus('ready')
    }

    setup().catch((e: unknown) => {
      if (!alive) return
      setMessage(e instanceof Error ? e.message : 'Google 로그인을 준비하지 못했습니다.')
      setStatus('error')
    })

    return () => { alive = false }
  }, [])

  return (
    <div className="gsi">
      {status === 'loading' && <p className="gsi__hint">Google 로그인 준비 중…</p>}
      {status === 'error' && <p className="auth__error">{message}</p>}
      {/* GIS가 이 요소 안에 버튼을 그린다. 로그인 처리 중에는 중복 클릭을 막기 위해 가린다. */}
      <div ref={holderRef} className="gsi__button" hidden={status !== 'ready' || busy} />
      {busy && <p className="gsi__hint">연결 중…</p>}
    </div>
  )
}
