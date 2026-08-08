/**
 * Google Identity Services (accounts.google.com/gsi/client) 전역 타입.
 * 공식 패키지(@types/google.accounts)를 추가하지 않고, 이 앱이 쓰는 API만 최소로 선언한다.
 */

export interface GoogleCredentialResponse {
  /** Google이 서명한 ID 토큰(JWT). 백엔드가 이 값을 검증한다. */
  credential: string
  select_by?: string
}

export interface GoogleIdConfiguration {
  client_id: string
  callback: (response: GoogleCredentialResponse) => void
  auto_select?: boolean
  cancel_on_tap_outside?: boolean
  use_fedcm_for_prompt?: boolean
  ux_mode?: 'popup' | 'redirect'
}

export interface GoogleButtonOptions {
  type?: 'standard' | 'icon'
  theme?: 'outline' | 'filled_blue' | 'filled_black'
  size?: 'small' | 'medium' | 'large'
  text?: 'signin_with' | 'signup_with' | 'continue_with' | 'signin'
  shape?: 'rectangular' | 'pill' | 'circle' | 'square'
  logo_alignment?: 'left' | 'center'
  width?: number | string
  locale?: string
}

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: GoogleIdConfiguration) => void
          renderButton: (parent: HTMLElement, options: GoogleButtonOptions) => void
          prompt: () => void
          disableAutoSelect: () => void
        }
      }
    }
  }
}

export {}
