/** Google Identity Services 스크립트 로더 — 로그인 버튼(GoogleSignInButton)과 Drive 백업 토큰(services/backup)이 같이 쓴다. */
const GSI_SRC = 'https://accounts.google.com/gsi/client'

/** GIS 스크립트를 한 번만 로드하고, 이후 호출은 같은 Promise를 재사용한다. */
let gsiLoader: Promise<void> | null = null

export function loadGsiScript(): Promise<void> {
  if (window.google?.accounts?.id) return Promise.resolve()
  if (gsiLoader) return gsiLoader

  gsiLoader = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${GSI_SRC}"]`)
    const script = existing ?? document.createElement('script')
    script.src = GSI_SRC
    script.async = true
    script.defer = true
    script.addEventListener('load', () => resolve())
    script.addEventListener('error', () =>
      reject(new Error('Google 로그인 스크립트를 불러오지 못했습니다. 네트워크 연결을 확인해주세요.')),
    )
    if (!existing) document.head.appendChild(script)
  })
  return gsiLoader
}
