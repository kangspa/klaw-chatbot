import { NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useSessions } from '../context/SessionsContext'
import { formatWhen } from '../services/time'

interface SidebarProps {
  onNavigate?: () => void  // 모바일에서 항목 클릭 시 사이드바 닫기
}

export default function Sidebar({ onNavigate }: SidebarProps) {
  const { user, logout } = useAuth()
  const { sessions, createNew, remove } = useSessions()
  const navigate = useNavigate()

  const handleNewSession = () => {
    const s = createNew()
    if (s) {
      onNavigate?.()
      navigate(`/chat/${s.id}`)
    }
  }

  const handleDelete = (e: React.MouseEvent, id: string) => {
    e.preventDefault()
    e.stopPropagation()
    if (confirm('이 세션을 삭제할까요? 대화 내용이 사라집니다.')) {
      remove(id)
      navigate('/chat')
    }
  }

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
        <button className="btn btn--primary btn--block" onClick={handleNewSession}>
          <span aria-hidden>＋</span> 새 세션
        </button>
        <NavLink
          to="/history"
          className={({ isActive }) => `btn btn--ghost btn--block${isActive ? ' is-active' : ''}`}
          onClick={onNavigate}
        >
          <span aria-hidden>🗂️</span> 대화 내역
        </NavLink>
      </div>

      <div className="sidebar__list-head">세션 목록</div>
      <nav className="sidebar__list">
        {sessions.length === 0 && (
          <p className="sidebar__empty">아직 세션이 없습니다.<br />‘새 세션’으로 시작해보세요.</p>
        )}
        {sessions.map((s) => (
          <NavLink
            key={s.id}
            to={`/chat/${s.id}`}
            className={({ isActive }) => `session-item${isActive ? ' is-active' : ''}`}
            onClick={onNavigate}
          >
            <span className="session-item__body">
              <span className="session-item__title">{s.title}</span>
              <span className="session-item__meta">{formatWhen(s.updatedAt)}</span>
            </span>
            <button
              className="session-item__delete"
              title="세션 삭제"
              aria-label="세션 삭제"
              onClick={(e) => handleDelete(e, s.id)}
            >
              ×
            </button>
          </NavLink>
        ))}
      </nav>

      <div className="sidebar__footer">
        <div className="sidebar__user">
          <span className="sidebar__avatar" aria-hidden>{user?.name?.[0]?.toUpperCase() ?? '?'}</span>
          <span className="sidebar__username" title={user?.id}>{user?.name}</span>
        </div>
        <button className="btn btn--text" onClick={logout} title="로그아웃">로그아웃</button>
      </div>
    </aside>
  )
}
