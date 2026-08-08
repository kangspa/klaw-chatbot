import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

/** 비로그인 사용자는 /login으로 보내고, 로그인 후 원래 위치로 복귀시킨다. */
export default function ProtectedRoute() {
  const { user, initializing } = useAuth()
  const location = useLocation()

  // 저장된 토큰을 서버에 확인하는 동안에는 리다이렉트를 보류한다.
  // (새로고침 때 잠깐 로그인 화면이 스쳐 지나가는 것을 막는다)
  if (initializing) {
    return (
      <div className="app-booting">
        <span className="app-booting__logo" aria-hidden>⚖️</span>
        <p>로그인 상태를 확인하는 중…</p>
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}
