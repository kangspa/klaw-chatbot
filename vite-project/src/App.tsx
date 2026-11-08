import { Navigate, Route, Routes } from 'react-router-dom'
import BootGate from './components/BootGate'
import AppLayout from './components/AppLayout'
import Dashboard from './pages/Dashboard'
import ChatPage from './pages/ChatPage'
import HistoryPage from './pages/HistoryPage'
import SettingsPage from './pages/SettingsPage'
import KeysPage from './pages/KeysPage'

export default function App() {
  return (
    <Routes>
      {/* 로그인 없이 쓰는 개인 PC용 서비스 — 예전 로그인 주소는 마이 페이지(Google 계정 연결)로 보낸다 */}
      <Route path="/login" element={<Navigate to="/settings" replace />} />
      <Route path="/signup" element={<Navigate to="/settings" replace />} />

      {/* 백엔드에서 로컬 사용자 정보를 받은 뒤 그린다 (사이드바 레이아웃 공유) */}
      <Route element={<BootGate />}>
        <Route element={<AppLayout />}>
          {/* 첫 진입 화면 = 대시보드 (사용량·비용, 자주 조회한 법령, 세션 요약) */}
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/chat" element={<Navigate to="/dashboard" replace />} />
          <Route path="/chat/:sessionId" element={<ChatPage />} />
          <Route path="/history" element={<HistoryPage />} />
          {/* 마이 페이지: 계정 정보 + 모델 사용 설정·로컬 모델 다운로드 */}
          <Route path="/settings" element={<SettingsPage />} />
          {/* API 키 관리: 키를 이 PC의 .env에 저장 */}
          <Route path="/keys" element={<KeysPage />} />
        </Route>
      </Route>

      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}
