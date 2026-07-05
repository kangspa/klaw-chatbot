import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import { AuthProvider } from './context/AuthContext'
import { SessionsProvider } from './context/SessionsContext'

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
