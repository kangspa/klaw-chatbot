import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import { AuthProvider } from './context/AuthContext'
import { SessionsProvider } from './context/SessionsContext'

// 화면 주소는 항상 localhost — Google 로그인은 localhost 주소에서만 되므로 127.0.0.1로 열렸으면 옮긴다
if (window.location.hostname === '127.0.0.1') {
  window.location.replace(window.location.href.replace('//127.0.0.1', '//localhost'))
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <SessionsProvider>
          <App />
        </SessionsProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
)
