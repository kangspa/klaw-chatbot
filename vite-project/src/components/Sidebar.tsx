import { useCallback, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useSessions } from '../context/SessionsContext'
import SessionItem from './SessionItem'
import UserMenu from './UserMenu'

interface SidebarProps {
  onNavigate?: () => void  // 모바일에서 항목 클릭 시 사이드바 닫기
}

export default function Sidebar({ onNavigate }: SidebarProps) {
  const { sessions, error, createNew, remove, rename } = useSessions()
  const navigate = useNavigate()
  const location = useLocation()

  // 한 번에 하나의 세션 메뉴만 열리도록 사이드바에서 관리
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null)

  const handleNewSession = async () => {
    try {
      const s = await createNew()
      if (s) {
        onNavigate?.()
        navigate(`/chat/${s.id}`)
      }
    } catch (e) {
      alert(e instanceof Error ? e.message : '세션을 만들지 못했습니다.')
    }
  }

  const handleDelete = useCallback(
    async (id: string) => {
      await remove(id)
      // 지금 보고 있던 세션을 지웠다면 대시보드로 빠져나온다
      if (location.pathname === `/chat/${id}`) navigate('/dashboard')
    },
    [remove, navigate, location.pathname],
  )

  return (
    <aside className="sidebar">
      <div className="sidebar__brand">
        <span className="sidebar__logo" aria-hidden>⚖️</span>
        <div>
          <div className="sidebar__title">KLaw</div>
          <div className="sidebar__subtitle">국내법 상담 챗봇</div>
        </div>
      </div>

      <div className="sidebar__actions">
        <NavLink
          to="/dashboard"
          className={({ isActive }) => `btn btn--ghost btn--block${isActive ? ' is-active' : ''}`}
          onClick={onNavigate}
        >
          <span aria-hidden>📊</span> 대시보드
        </NavLink>
        <NavLink
          to="/history"
          className={({ isActive }) => `btn btn--ghost btn--block${isActive ? ' is-active' : ''}`}
          onClick={onNavigate}
        >
          <span aria-hidden>🗂️</span> 대화 내역
        </NavLink>
      </div>

      <div className="sidebar__list-head">세션 목록</div>
      <nav className="sidebar__list" aria-label="세션 목록">
        {/* 세션 목록의 첫 줄 — 목록과 함께 스크롤된다 */}
        <button type="button" className="sidebar__new" onClick={handleNewSession}>
          <span className="sidebar__new-icon" aria-hidden>＋</span> 새 세션
        </button>
        {error && <p className="sidebar__empty sidebar__empty--error">{error}</p>}
        {!error && sessions.length === 0 && (
          <p className="sidebar__empty">아직 세션이 없습니다.<br />‘새 세션’으로 시작해보세요.</p>
        )}
        {sessions.map((s) => (
          <SessionItem
            key={s.id}
            session={s}
            menuOpen={menuOpenId === s.id}
            onToggleMenu={(open) => setMenuOpenId(open ? s.id : null)}
            onRename={(title) => rename(s.id, title)}
            onDelete={() => handleDelete(s.id)}
            onNavigate={onNavigate}
          />
        ))}
      </nav>

      <div className="sidebar__footer">
        <UserMenu onNavigate={onNavigate} />
      </div>
    </aside>
  )
}
