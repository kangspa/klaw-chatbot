import { useState, type CSSProperties } from 'react'
import { useAuth } from '../context/AuthContext'

interface UserAvatarProps {
  /** 지름(px). 기본 30 */
  size?: number
  className?: string
}

/**
 * 현재 사용자의 아바타.
 * Google 계정이 연결돼 있고 프로필 사진이 있으면 사진을, 아니면 이름 첫 글자를 보여준다.
 * 사진을 불러오지 못하면(만료·차단 등) 첫 글자로 되돌아간다.
 */
export default function UserAvatar({ size = 30, className }: UserAvatarProps) {
  const { user } = useAuth()
  // 불러오기에 실패한 사진 주소 — 주소가 바뀌면(다시 연결 등) 자동으로 다시 시도한다
  const [failedSrc, setFailedSrc] = useState<string | null>(null)

  const picture = user?.googleLinked && user.picture ? user.picture : null
  const showPhoto = picture !== null && picture !== failedSrc
  const initial = user?.name?.trim()?.[0]?.toUpperCase() ?? '?'

  const style: CSSProperties = {
    width: size,
    height: size,
    fontSize: Math.max(10, Math.round(size * 0.44)),
  }
  const cls = `user-avatar${showPhoto ? ' user-avatar--photo' : ''}${className ? ` ${className}` : ''}`

  if (showPhoto) {
    return (
      <img
        className={cls}
        style={style}
        src={picture}
        alt=""
        aria-hidden
        referrerPolicy="no-referrer"  /* Google 프로필 이미지는 Referer가 있으면 막힌다 */
        draggable={false}
        onError={() => setFailedSrc(picture)}
      />
    )
  }
  return (
    <span className={cls} style={style} aria-hidden>
      {initial}
    </span>
  )
}
