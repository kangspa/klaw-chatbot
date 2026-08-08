import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import ProtectedRoute from './components/ProtectedRoute'
import AppLayout from './components/AppLayout'
import Login from './pages/Login'
import ChatHome from './pages/ChatHome'
import ChatPage from './pages/ChatPage'
import HistoryPage from './pages/HistoryPage'

/** 이미 로그인한 사용자가 /login에 오면 챗봇으로 보낸다. */
function GuestOnly({ children }: { children: React.ReactElement }) {
  const { user, initializing } = useAuth()
  if (initializing) return null   // 토큰 검증 중에는 판단을 미룬다
  return user ? <Navigate to="/chat" replace /> : children
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<GuestOnly><Login /></GuestOnly>} />
      {/* 회원가입은 Google 로그인이 겸하므로 별도 페이지가 없다 */}
      <Route path="/signup" element={<Navigate to="/login" replace />} />

      {/* 인증 필요 영역 (사이드바 레이아웃 공유) */}
      <Route element={<ProtectedRoute />}>
        <Route element={<AppLayout />}>
          <Route path="/chat" element={<ChatHome />} />
          <Route path="/chat/:sessionId" element={<ChatPage />} />
          <Route path="/history" element={<HistoryPage />} />
        </Route>
      </Route>

      <Route path="/" element={<Navigate to="/chat" replace />} />
      <Route path="*" element={<Navigate to="/chat" replace />} />
    </Routes>
  )
}
