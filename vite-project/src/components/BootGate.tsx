import { Outlet } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import AppLogo from './AppLogo'

/**
 * 앱 화면을 그리기 전에 백엔드에서 로컬 사용자 정보를 받아온다.
 * 로그인은 없다 — 백엔드에 연결하지 못했을 때만 안내와 재시도를 보여준다.
 */
export default function BootGate() {
  const { user, initializing, error, refresh } = useAuth()

  if (initializing) {
    return (
      <div className="app-booting">
        <AppLogo size={48} className="app-booting__logo" />
        <p>불러오는 중…</p>
      </div>
    )
  }
  if (!user) {
    return (
      <div className="app-booting">
        <AppLogo size={48} className="app-booting__logo" />
        <p>백엔드 서버에 연결할 수 없습니다.</p>
        {error && <p className="app-booting__detail">{error}</p>}
        <p className="app-booting__detail">KLaw가 실행 중인지(트레이 아이콘) 확인하세요. 개발 중이면 <code>uv run python main.py</code>.</p>
        <button className="btn btn--primary" onClick={() => void refresh()}>다시 시도</button>
      </div>
    )
  }
  return <Outlet />
}
