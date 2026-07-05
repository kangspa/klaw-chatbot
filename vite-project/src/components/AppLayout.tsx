import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import Sidebar from './Sidebar'

/** 사이드바 + 메인 영역 셸. 모바일에서는 사이드바를 오버레이로 토글한다. */
export default function AppLayout() {
  const [open, setOpen] = useState(false)

  return (
    <div className={`app-shell${open ? ' is-open' : ''}`}>
      <div className="app-sidebar">
        <Sidebar onNavigate={() => setOpen(false)} />
      </div>

      {open && <div className="app-scrim" onClick={() => setOpen(false)} />}

      <main className="app-main">
        <header className="app-mobilebar">
          <button className="btn btn--icon" aria-label="메뉴 열기" onClick={() => setOpen(true)}>☰</button>
          <span className="app-mobilebar__title">KLaw</span>
        </header>
        <Outlet />
      </main>
    </div>
  )
}
