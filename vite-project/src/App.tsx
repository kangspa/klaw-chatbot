import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import ProtectedRoute from './components/ProtectedRoute'
import AppLayout from './components/AppLayout'
import Login from './pages/Login'
import Signup from './pages/Signup'
import ChatHome from './pages/ChatHome'
import ChatPage from './pages/ChatPage'
import HistoryPage from './pages/HistoryPage'

/** 이미 로그인한 사용자가 /login·/signup에 오면 챗봇으로 보낸다. */
function GuestOnly({ children }: { children: React.ReactElement }) {
  const { user } = useAuth()
  return user ? <Navigate to="/chat" replace /> : children
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<GuestOnly><Login /></GuestOnly>} />
      <Route path="/signup" element={<GuestOnly><Signup /></GuestOnly>} />

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
