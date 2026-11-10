import { apiFetch } from './http'

/**
 * 대화 기록 검색용 임베딩 방식 — 내장(CPU, 기본) · Ollama Qwen3 Embedding · Gemini API.
 * 방식을 바꾸면 서버가 DB의 대화 원문으로 새 벡터를 백그라운드에서 다시 만든다 (재인덱싱). 그동안 채팅은 그대로 된다.
 */

export type EmbeddingProviderId = 'local' | 'ollama' | 'gemini'

export interface EmbeddingCollection {
  name: string
  exists: boolean
  points: number
  bytes: number
}

export interface EmbeddingProvider {
  id: EmbeddingProviderId
  label: string
  model: string
  dims: number
  /** 받아야 하는 모델 크기 (gemini는 0) */
  approxBytes: number
  device: string
  privacy: string
  /** 대화가 PC 안에만 머무는지 */
  local: boolean
  /** 고를 수 없는 이유 (엔진 미설치·키 없음). null이면 고를 수 있다 */
  problem: string | null
  downloaded: boolean
  ready: boolean
  /** 디스크의 모델 크기 */
  sizeBytes: number
  /** 모델 받는 중·여는 중 */
  preparing: boolean
  percent: number | null
  message: string
  collection: EmbeddingCollection
}

export interface ReindexJob {
  provider: EmbeddingProviderId
  collection: string
  trigger: 'startup' | 'select' | 'manual'
  status: 'preparing' | 'indexing' | 'done' | 'error' | 'cancelled'
  total: number
  done: number
  skipped: number
  removed: number
  turns: number
  percent: number | null
  message: string
  startedAt: number
  finishedAt: number | null
}

export interface EmbeddingStatus {
  active: EmbeddingProviderId
  providers: EmbeddingProvider[]
  /** DB에 저장된 대화 턴 수 */
  turns: number | null
  reindex: ReindexJob | null
}

/** GET /chatbot/embeddings */
export async function getEmbeddingStatus(): Promise<EmbeddingStatus> {
  return apiFetch<EmbeddingStatus>('/chatbot/embeddings')
}

/** POST /chatbot/embeddings/select — 방식 변경 + 재인덱싱 시작 */
export async function selectEmbeddingProvider(provider: EmbeddingProviderId): Promise<EmbeddingStatus> {
  return apiFetch<EmbeddingStatus>('/chatbot/embeddings/select', { method: 'POST', body: JSON.stringify({ provider }) })
}

/** POST /chatbot/embeddings/reindex — full이면 전부, 아니면 빠지거나 바뀐 턴만 */
export async function reindexEmbeddings(full = false): Promise<EmbeddingStatus> {
  return apiFetch<EmbeddingStatus>('/chatbot/embeddings/reindex', { method: 'POST', body: JSON.stringify({ full }) })
}

/** POST /chatbot/embeddings/prepare — 모델을 미리 받는다 (local·ollama) */
export async function prepareEmbeddingModel(provider: EmbeddingProviderId): Promise<EmbeddingStatus> {
  return apiFetch<EmbeddingStatus>('/chatbot/embeddings/prepare', { method: 'POST', body: JSON.stringify({ provider }) })
}

/** POST /chatbot/embeddings/delete-unused — 지금 방식이 아닌 벡터 컬렉션을 지운다 */
export async function deleteUnusedEmbeddings(): Promise<EmbeddingStatus & { deleted: string[]; freedBytes: number }> {
  return apiFetch('/chatbot/embeddings/delete-unused', { method: 'POST' })
}
