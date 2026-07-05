/**
 * 백엔드가 요구하는 오프셋 포함 ISO8601 문자열을 생성.
 * 형식: YYYY-MM-DDTHH:mm:ss+HH:mm  (예: 2026-07-05T14:19:49+09:00)
 * (Date.prototype.toISOString은 항상 UTC(Z)라서 로컬 오프셋 형식을 직접 만든다.)
 */
export function localIsoWithOffset(date: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')

  const y = date.getFullYear()
  const mo = pad(date.getMonth() + 1)
  const d = pad(date.getDate())
  const h = pad(date.getHours())
  const mi = pad(date.getMinutes())
  const s = pad(date.getSeconds())

  // getTimezoneOffset(): UTC 기준 분 단위, 한국(UTC+9)은 -540을 반환하므로 부호를 뒤집는다.
  const offsetMin = -date.getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '-'
  const absMin = Math.abs(offsetMin)
  const offH = pad(Math.floor(absMin / 60))
  const offM = pad(absMin % 60)

  return `${y}-${mo}-${d}T${h}:${mi}:${s}${sign}${offH}:${offM}`
}

/** 사람이 읽기 좋은 상대/절대 시각 표기 (사이드바·요약용) */
export function formatWhen(iso: string): string {
  const date = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const min = Math.floor(diffMs / 60000)

  if (min < 1) return '방금 전'
  if (min < 60) return `${min}분 전`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour}시간 전`
  const day = Math.floor(hour / 24)
  if (day < 7) return `${day}일 전`

  return date.toLocaleDateString('ko-KR', { year: 'numeric', month: 'long', day: 'numeric' })
}
