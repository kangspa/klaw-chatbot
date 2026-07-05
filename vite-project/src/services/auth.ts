import type { User } from '../types'

/**
 * localStorage 기반 목업 인증 서비스.
 *
 * ⚠️ 프로토타입용입니다. 비밀번호를 평문으로 브라우저에 저장하므로 실제 서비스에서는
 *    반드시 서버 인증 API로 교체해야 합니다. 아래 함수 시그니처를 유지한 채 내부만
 *    fetch 호출로 바꾸면 나머지 코드는 수정 없이 동작합니다.
 */

const USERS_KEY = 'klaw.users'          // { [email]: { user, password } }
const CURRENT_KEY = 'klaw.currentUser'  // User

interface StoredAccount {
  user: User
  password: string
}

type UserStore = Record<string, StoredAccount>

function readUsers(): UserStore {
  try {
    return JSON.parse(localStorage.getItem(USERS_KEY) ?? '{}') as UserStore
  } catch {
    return {}
  }
}

function writeUsers(store: UserStore): void {
  localStorage.setItem(USERS_KEY, JSON.stringify(store))
}

export function getCurrentUser(): User | null {
  try {
    const raw = localStorage.getItem(CURRENT_KEY)
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

export async function signup(name: string, email: string, password: string): Promise<User> {
  const store = readUsers()
  const key = email.trim().toLowerCase()

  if (!name.trim()) throw new Error('이름을 입력해주세요.')
  if (!key) throw new Error('이메일을 입력해주세요.')
  if (password.length < 4) throw new Error('비밀번호는 4자 이상이어야 합니다.')
  if (store[key]) throw new Error('이미 가입된 이메일입니다.')

  const user: User = { id: key, name: name.trim() }
  store[key] = { user, password }
  writeUsers(store)
  localStorage.setItem(CURRENT_KEY, JSON.stringify(user))
  return user
}

export async function login(email: string, password: string): Promise<User> {
  const store = readUsers()
  const key = email.trim().toLowerCase()
  const account = store[key]

  if (!account || account.password !== password) {
    throw new Error('이메일 또는 비밀번호가 올바르지 않습니다.')
  }

  localStorage.setItem(CURRENT_KEY, JSON.stringify(account.user))
  return account.user
}

export function logout(): void {
  localStorage.removeItem(CURRENT_KEY)
}
