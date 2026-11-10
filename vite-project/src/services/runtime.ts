import { apiFetch } from './http'

/**
 * 런타임 버전 관리 — Ollama(로컬 모델 서버)와 LiteLLM(모델 호출 라이브러리).
 * 둘 다 바꿀 때는 한 작업으로 처리한다: LiteLLM 호환성 확인 → Ollama 변경 → LiteLLM 설치·서버 재시작.
 * LiteLLM을 바꾸는 동안은 백엔드가 잠시 내려가므로, 진행 상태 조회가 실패해도 계속 다시 시도한다.
 */

export interface Release {
  version: string
  /** YYYY-MM-DD */
  publishedAt: string
  /** LiteLLM만: 호환성 확인 결과 (모르면 null) */
  compatible?: boolean | null
  compatReason?: string | null
}

export interface OllamaRuntime {
  /** 실행 중인 버전. null이면 엔진이 없거나 아직 시작 중 */
  current: string | null
  /** 받아 둔 엔진 버전. null이면 아직 설치하지 않음 (exe에 넣지 않고 처음 쓸 때 받는다) */
  installed: string | null
  /** 엔진은 있는데 아직 응답하지 않음 */
  starting: boolean
  engineBytes: number
  modelsBytes: number
  configured: string
  latest: string | null
  updateAvailable: boolean
  acceleration: string | null
  accelerated: boolean
  releases: Release[]
  releasesError: string | null
}

export interface LiteLLMRuntime {
  current: string
  latest: string | null
  /** 현재보다 새 버전 중 호환되는 최신 버전 (찾는 중이거나 없으면 null) */
  latestCompatible: string | null
  scanning: boolean
  updateAvailable: boolean
  /** 적용할 수 있는지 (개발: main.py로 띄운 경우 / exe: 데스크톱 앱) */
  canRestart: boolean
  /** app: exe — 앱 전체가 잠시 다시 시작 / console: 개발 — 새 콘솔 창에서 설치 후 서버 재시작 */
  restartMode: 'app' | 'console'
  /** exe에 묶인 원래 버전 (개발에서는 null) */
  bundled: string | null
  lockedReason: string | null
  releases: Release[]
  releasesError: string | null
}

export interface JobItem {
  from: string | null
  to: string
  status: 'pending' | 'running' | 'done' | 'error' | 'skipped'
  rolledBack: boolean
}

export interface RuntimeJob {
  /** restarting = LiteLLM을 적용하려고 백엔드가 다시 시작되는 중 */
  status: 'running' | 'restarting' | 'done' | 'error'
  step: string
  items: { ollama?: JobItem; litellm?: JobItem }
  message: string
  startedAt: number
  finishedAt: number | null
  log: string[]
}

export interface RuntimeStatus {
  ollama: OllamaRuntime
  litellm: LiteLLMRuntime
  job: RuntimeJob | null
}

/** GET /chatbot/runtime */
export async function getRuntimeStatus(): Promise<RuntimeStatus> {
  return apiFetch<RuntimeStatus>('/chatbot/runtime')
}

/** POST /chatbot/runtime/update — 둘 중 바꿀 것만 넘긴다 */
export async function startRuntimeUpdate(target: { ollama?: string; litellm?: string }): Promise<RuntimeJob> {
  return apiFetch<RuntimeJob>('/chatbot/runtime/update', { method: 'POST', body: JSON.stringify(target) })
}

/** GET /chatbot/runtime/update */
export async function getRuntimeJob(): Promise<RuntimeJob | null> {
  return apiFetch<RuntimeJob | null>('/chatbot/runtime/update')
}

/** POST /chatbot/runtime/litellm/check — 별도 가상환경에서 호환성 확인 (처음엔 수십 초) */
export async function checkLiteLLM(version: string): Promise<{ version: string; ok: boolean; reason: string }> {
  return apiFetch('/chatbot/runtime/litellm/check', { method: 'POST', body: JSON.stringify({ version }) })
}

/** POST /chatbot/runtime/ollama/remove — 엔진(과 선택 시 받은 모델)을 지워 디스크를 비운다 */
export async function removeOllamaEngine(includeModels: boolean): Promise<{ freedBytes: number }> {
  return apiFetch('/chatbot/runtime/ollama/remove', { method: 'POST', body: JSON.stringify({ includeModels }) })
}
