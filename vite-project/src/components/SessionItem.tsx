import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { NavLink } from 'react-router-dom'
import type { Session } from '../types'
import { formatWhen } from '../services/time'

interface SessionItemProps {
  session: Session
  /** 이 항목의 메뉴가 열려 있는지 (사이드바가 한 번에 하나만 열리도록 관리) */
  menuOpen: boolean
  onToggleMenu: (open: boolean) => void
  onRename: (title: string) => Promise<void>
  onDelete: () => Promise<void>
  onNavigate?: () => void
}

/**
 * 사이드바의 세션 한 줄.
 * 우측 햄버거(⋯) 버튼을 누르면 '이름 변경' / '삭제' 메뉴가 열린다.
 *
 * 링크(NavLink)와 메뉴 버튼을 형제로 두는 이유: 링크 안에 버튼을 중첩하면
 * 클릭 처리와 접근성이 모두 깨지기 때문에 래퍼 div로 감싸고 링크는 본문만 담당한다.
 */
export default function SessionItem({
  session, menuOpen, onToggleMenu, onRename, onDelete, onNavigate,
}: SessionItemProps) {
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState(session.title)
  const [saving, setSaving] = useState(false)

  const wrapRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // 메뉴 바깥 클릭 / Esc 로 닫기
  useEffect(() => {
    if (!menuOpen) return
    const onPointerDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) onToggleMenu(false)
    }
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') onToggleMenu(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen, onToggleMenu])

  // 이름 변경 모드로 들어가면 입력창에 포커스 + 전체 선택
  useEffect(() => {
    if (renaming) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [renaming])

  const startRename = () => {
    setDraft(session.title)
    setRenaming(true)
    onToggleMenu(false)
  }

  // Enter로 커밋하면 곧이어 blur도 발생한다. state는 비동기로 반영되므로
  // ref로 진행 여부를 잠가 중복 PATCH를 막는다.
  const committingRef = useRef(false)

  const commitRename = async () => {
    const title = draft.trim()
    if (!renaming || committingRef.current) return
    if (!title || title === session.title) {
      setRenaming(false)
      return
    }
    committingRef.current = true
    setSaving(true)
    try {
      await onRename(title)
      setRenaming(false)
    } catch (e) {
      alert(e instanceof Error ? e.message : '이름을 변경하지 못했습니다.')
    } finally {
      committingRef.current = false
      setSaving(false)
    }
  }

  const onInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      void commitRename()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setRenaming(false)
    }
  }

  const handleDelete = async () => {
    onToggleMenu(false)
    if (!confirm(`'${session.title}' 세션을 삭제할까요?\n대화 원문과 검색용 벡터 기록이 모두 사라집니다.`)) return
    try {
      await onDelete()
    } catch (e) {
      alert(e instanceof Error ? e.message : '세션을 삭제하지 못했습니다.')
    }
  }

  if (renaming) {
    return (
      <div className="session-item is-renaming" ref={wrapRef}>
        <input
          ref={inputRef}
          className="session-item__rename"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onInputKeyDown}
          onBlur={() => void commitRename()}
          disabled={saving}
          maxLength={80}
          aria-label="세션 이름"
        />
      </div>
    )
  }

  return (
    <div className={`session-item-wrap${menuOpen ? ' is-menu-open' : ''}`} ref={wrapRef}>
      <NavLink
        to={`/chat/${session.id}`}
        className={({ isActive }) => `session-item${isActive ? ' is-active' : ''}`}
        onClick={onNavigate}
      >
        <span className="session-item__body">
          <span className="session-item__title">
            {session.parentSessionId && (
              <span className="session-item__branch" title="브랜치로 만들어진 세션">⑂</span>
            )}
            {session.title}
          </span>
          <span className="session-item__meta">{formatWhen(session.updatedAt)}</span>
        </span>
      </NavLink>

      <button
        className="session-item__menu-btn"
        title="세션 메뉴"
        aria-label="세션 메뉴"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={(e) => {
          e.preventDefault()
          e.stopPropagation()
          onToggleMenu(!menuOpen)
        }}
      >
        ⋯
      </button>

      {menuOpen && (
        <div className="session-menu" role="menu">
          <button className="session-menu__item" role="menuitem" onClick={startRename}>
            <span aria-hidden>✏️</span> 이름 변경
          </button>
          <button
            className="session-menu__item session-menu__item--danger"
            role="menuitem"
            onClick={() => void handleDelete()}
          >
            <span aria-hidden>🗑</span> 삭제
          </button>
        </div>
      )}
    </div>
  )
}
