import { apiFetch } from './http'

/**
 * 로컬 모델 서버(Ollama) 버전 관리.
 * 버전을 바꾸면 서버가 Ollama 이미지를 새로 빌드하고 컨테이너를 다시 띄운다 (실패하면 이전 버전으로 되돌림).
 */

export interface OllamaRelease {
  version: string
  /** YYYY-MM-DD */
  publishedAt: string
}

export interface UpgradeJob {
  status: 'running' | 'done' | 'error'
  /** 현재 단계 (릴리스 확인 / 이미지 빌드 / 컨테이너 재시작 / 동작 확인 / 되돌리는 중 …) */
  step: string
  target: string
  previous: string
  rolledBack: boolean
  message: string
  startedAt: number
  finishedAt: number | null
  /** docker compose 출력 등 최근 로그 줄 */
  log: string[]
}

export interface OllamaStatus {
  /** 실행 중인 Ollama 버전. null이면 연결할 수 없음 */
  runningVersion: string | null
  /** .env(OLLAMA_VERSION)에 저장된 버전 — 컨테이너를 다시 만들 때 쓰인다 */
  configuredVersion: string
  latestVersion: string | null
  updateAvailable: boolean
  acceleration: string | null
  accelerated: boolean
  releases: OllamaRelease[]
  releasesError: string | null
  job: UpgradeJob | null
}

/** GET /chatbot/ollama */
export async function getOllamaStatus(): Promise<OllamaStatus> {
  return apiFetch<OllamaStatus>('/chatbot/ollama')
}

/** POST /chatbot/ollama/upgrade — 버전 변경 시작 (백그라운드로 진행) */
export async function startOllamaUpgrade(version: string): Promise<UpgradeJob> {
  return apiFetch<UpgradeJob>('/chatbot/ollama/upgrade', {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

/** GET /chatbot/ollama/upgrade — 진행 상태·로그 */
export async function getUpgradeJob(): Promise<UpgradeJob | null> {
  return apiFetch<UpgradeJob | null>('/chatbot/ollama/upgrade')
}
