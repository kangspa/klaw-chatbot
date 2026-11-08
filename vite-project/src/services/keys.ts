import { apiFetch } from './http'

/**
 * API 키 관리 — 키는 이 PC의 .env에 저장되고, 서버는 원문을 돌려주지 않는다 (앞뒤 몇 글자만).
 * 연결 확인은 해당 회사 API에만 키를 보낸다 (모델 목록·계정 조회 — 토큰을 쓰지 않는다).
 */

export interface ApiKeyItem {
  /** OPENAI | GEMINI | ANTHROPIC | HUGGINGFACE | OLLAMA | LAW_OC */
  id: string
  label: string
  /** 저장되는 .env 변수 이름 */
  env: string
  description: string
  configured: boolean
  /** 'sk-1…abcd' 형태 (없으면 null) */
  masked: string | null
  deletable: boolean
  /** 저장 후 적용 방식: 바로 / 모델 캐시 초기화 / 법령 검색 서버 재시작 */
  apply: 'immediate' | 'reset_models' | 'restart_law'
  /** LAW_OC만: 법령 검색 서버 재시작·인증 확인 진행 상태 */
  applyStatus?: { status: 'running' | 'done' | 'error' | null; message: string; at: number | null }
}

export async function listKeys(): Promise<ApiKeyItem[]> {
  return apiFetch<ApiKeyItem[]>('/chatbot/keys')
}

export async function setKey(id: string, value: string): Promise<ApiKeyItem> {
  return apiFetch<ApiKeyItem>('/chatbot/keys', { method: 'PUT', body: JSON.stringify({ id, value }) })
}

export async function deleteKey(id: string): Promise<ApiKeyItem> {
  return apiFetch<ApiKeyItem>('/chatbot/keys/delete', { method: 'POST', body: JSON.stringify({ id }) })
}

export async function testKey(id: string): Promise<{ ok: boolean; message: string }> {
  return apiFetch('/chatbot/keys/test', { method: 'POST', body: JSON.stringify({ id }) })
}
