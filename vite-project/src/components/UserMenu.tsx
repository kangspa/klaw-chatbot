import { useEffect, useId, useRef, useState, type FocusEvent, type KeyboardEvent } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import UserAvatar from './UserAvatar'

interface UserMenuProps {
  onNavigate?: () => void  // 모바일에서 항목 선택 시 사이드바 닫기
}

const ITEMS = [
  { to: '/settings', icon: '⚙️', label: '마이 페이지' },
  { to: '/keys', icon: '🔑', label: 'API 키' },
] as const

/**
 * 사이드바 하단의 사용자 프로필 버튼.
 * 누르면 위쪽으로 작은 메뉴(마이 페이지 / API 키)가 열린다.
 * 바깥 클릭·Esc·항목 선택·포커스가 메뉴 밖으로 나가면 닫힌다.
 * 키보드: ↑/↓로 열기·이동, Home/End, Esc는 닫고 버튼으로 포커스를 돌려준다.
 */
export default function UserMenu({ onNavigate }: UserMenuProps) {
  const { user } = useAuth()
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const menuId = useId()

  const wrapRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const itemRefs = useRef<(HTMLAnchorElement | null)[]>([])
  // 메뉴가 열린 직후 포커스할 항목 (↑로 열면 마지막 항목)
  const focusOnOpen = useRef(0)

  const linked = !!user?.googleLinked
  const status = linked ? 'Google 연결됨' : 'Google 연결 (선택)'
  const onMenuPage = ITEMS.some((it) => location.pathname.startsWith(it.to))

  // 열리면 항목에 포커스
  useEffect(() => {
    if (open) itemRefs.current[focusOnOpen.current]?.focus()
  }, [open])

  // 바깥 클릭으로 닫기 (포커스를 받지 않는 영역을 눌러도 닫히도록)
  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  const openMenu = (focusIndex: number) => {
    focusOnOpen.current = focusIndex
    setOpen(true)
  }

  const close = (returnFocus: boolean) => {
    setOpen(false)
    if (returnFocus) buttonRef.current?.focus()
  }

  const onButtonKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const index = e.key === 'ArrowUp' ? ITEMS.length - 1 : 0
      if (open) itemRefs.current[index]?.focus()
      else openMenu(index)
    }
  }

  const onMenuKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = itemRefs.current.filter((el): el is HTMLAnchorElement => el !== null)
    const current = items.indexOf(document.activeElement as HTMLAnchorElement)
    let next: number | null = null
    if (e.key === 'ArrowDown') next = (current + 1) % items.length
    else if (e.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = items.length - 1
    if (next !== null) {
      e.preventDefault()
      items[next]?.focus()
    }
  }

  // Esc는 메뉴 안·버튼 어디서 눌러도 닫는다
  const onWrapKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (open && e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close(true)
    }
  }

  // Tab 등으로 포커스가 메뉴 밖으로 나가면 닫기
  const onWrapBlur = (e: FocusEvent<HTMLDivElement>) => {
    if (open && e.relatedTarget && !wrapRef.current?.contains(e.relatedTarget as Node)) setOpen(false)
  }

  return (
    <div className="user-menu" ref={wrapRef} onKeyDown={onWrapKeyDown} onBlur={onWrapBlur}>
      <button
        ref={buttonRef}
        type="button"
        className={`user-menu__button${open ? ' is-open' : ''}${onMenuPage ? ' is-active' : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        title={linked && user?.email ? `Google 연결됨 (${user.email})` : '계정 메뉴'}
        onClick={() => (open ? close(false) : openMenu(0))}
        onKeyDown={onButtonKeyDown}
      >
        <UserAvatar size={30} />
        <span className="user-menu__text">
          <span className="user-menu__name">{user?.name}</span>
          <span className={`user-menu__status${linked ? ' is-linked' : ''}`}>{status}</span>
        </span>
        <svg className="user-menu__chevron" width="14" height="14" viewBox="0 0 16 16" aria-hidden>
          <path d="M4 10l4-4 4 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div id={menuId} className="user-menu__popover" role="menu" aria-label="계정 메뉴" onKeyDown={onMenuKeyDown}>
          {linked && user?.email && <div className="user-menu__email" title={user.email}>{user.email}</div>}
          {ITEMS.map((it, i) => (
            <NavLink
              key={it.to}
              ref={(el) => { itemRefs.current[i] = el }}
              to={it.to}
              role="menuitem"
              className={({ isActive }) => `user-menu__item${isActive ? ' is-active' : ''}`}
              onClick={() => {
                setOpen(false)
                onNavigate?.()
              }}
            >
              <span aria-hidden>{it.icon}</span> {it.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}
