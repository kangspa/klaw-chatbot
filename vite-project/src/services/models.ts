import { apiFetch } from './http'

/**
 * AI 모델 목록과 사용자별 모델 설정.
 * 모델·표시명·순서는 백엔드 DB(llm_models)에서 관리하므로 프론트는 받아서 보여주기만 한다.
 * (모델을 추가·숨기려면 DB 또는 api/llm_gateway/seed_models.json을 고친다 — 프론트 재빌드 불필요)
 *
 * 사용자가 마이 페이지에서 끈 모델은 채팅 화면의 선택 목록에서만 빠진다.
 * 백엔드는 꺼진 모델의 호출을 막지 않는다 (설정은 화면 정리용).
 */

/** 로컬 모델 상태. 클라우드 모델은 null */
export type LocalStatus =
  | 'installed'    // 받아져 있어 바로 사용 가능
  | 'missing'      // 아직 받지 않음 → 마이 페이지에서 다운로드
  | 'pulling'      // 서버가 받는 중
  | 'unreachable'  // Ollama 서버에 연결할 수 없음
  | 'unmanaged'    // 서버가 모델 파일을 관리하지 않는 런타임 (LM Studio·vLLM)

export interface PullState {
  status: 'pulling' | 'done' | 'error'
  /** 0~100. 전체 크기를 아직 모르면 null */
  percent: number | null
  completed: number
  total: number
  /** Ollama가 보낸 마지막 상태 문구 또는 오류 메시지 */
  message: string
  started_at: number
  finished_at: number | null
}

export interface ModelOption {
  /** 요청 시 model 값으로 보낼 ID ('접두사/모델명') */
  id: string
  /** 선택 목록에 보일 이름 */
  label: string
  /** 선택 목록 그룹 (Google / OpenAI / Anthropic / 로컬 ...) */
  group: string
  isDefault: boolean
  /** 이 사용자가 켜 둔 모델인지 (기본 켜짐) */
  enabled: boolean
  /** 로컬 런타임에서 도는 모델인지 */
  local: boolean
  localStatus: LocalStatus | null
  pull: PullState | null
  /** 설치된 로컬 모델의 디스크 크기 (바이트) */
  sizeBytes: number | null
  /** 마이 페이지에서 사용자가 추가한 커스텀 모델인지 */
  custom: boolean
  /** Ollama 모델명 (Ollama 모델만) */
  ollamaName: string | null
  /** 도구 호출 지원 여부 (커스텀·Hugging Face 모델만 확인, 모르면 null). false면 법률 검색을 할 수 없다 */
  supportsTools: boolean | null
  /** 화면 표시용 부가 정보 (Hugging Face: repo·provider·단가 등) */
  meta: HfMeta
}

/** Hugging Face 모델 부가 정보 */
export interface HfMeta {
  source?: 'huggingface'
  repo?: string
  /** 고정한 추론 제공사 (novita·together 등) */
  provider?: string
  supportsTools?: boolean
  inputPer1M?: number | null
  outputPer1M?: number | null
  contextTokens?: number | null
  /** 이 모델을 제공하는 다른 제공사들 */
  providers?: string[]
}

/** 서버(Ollama)에 받아져 있는 모델 하나 */
export interface StoredModel {
  /** 삭제 요청에 쓸 ID ('ollama/이름') */
  id: string
  name: string
  sizeBytes: number
  /** 모델 목록에 등록된 모델인지. false면 목록에서 빠지고 파일만 남은 모델 */
  registered: boolean
  label: string | null
}

export interface LocalStorage {
  /** Ollama에 연결되었는지 */
  reachable: boolean
  totalBytes: number
  models: StoredModel[]
}

export interface ModelGroup {
  group: string
  models: ModelOption[]
}

/** GET /chatbot/models — 활성 모델 전체 + 사용자 설정·로컬 상태 (서버가 sort_order 순으로 정렬해 준다) */
export async function listModels(): Promise<ModelOption[]> {
  return apiFetch<ModelOption[]>('/chatbot/models')
}

/** PUT /chatbot/models/preference — 모델 사용/사용 안 함 */
export async function setModelEnabled(modelId: string, enabled: boolean): Promise<void> {
  await apiFetch<void>('/chatbot/models/preference', {
    method: 'PUT',
    body: JSON.stringify({ modelId, enabled }),
  })
}

/** POST /chatbot/models/pull — 로컬 모델 다운로드 시작 (서버 백그라운드로 진행) */
export async function startPull(modelId: string): Promise<PullState> {
  return apiFetch<PullState>('/chatbot/models/pull', {
    method: 'POST',
    body: JSON.stringify({ modelId }),
  })
}

/** GET /chatbot/models/pulls — 모델 ID별 다운로드 상태 */
export async function listPulls(): Promise<Record<string, PullState>> {
  return apiFetch<Record<string, PullState>>('/chatbot/models/pulls')
}

/** GET /chatbot/models/storage — 받아진 로컬 모델 전체와 용량 */
export async function getLocalStorage(): Promise<LocalStorage> {
  return apiFetch<LocalStorage>('/chatbot/models/storage')
}

/** POST /chatbot/models/delete — 로컬 모델 파일 삭제 (서버 전체에서 지워진다) */
export async function deleteLocalModel(modelId: string): Promise<{ deleted: boolean; sizeBytes: number }> {
  return apiFetch('/chatbot/models/delete', {
    method: 'POST',
    body: JSON.stringify({ modelId }),
  })
}

/**
 * POST /chatbot/models/custom — Ollama 모델명으로 커스텀 모델 추가.
 * 서버가 다운로드를 시작해 모델이 있는지 확인한 뒤 등록한다 (없는 모델이면 400). 나머지 다운로드는 백그라운드.
 */
export async function addCustomModel(name: string, label?: string): Promise<{ modelId: string; label: string }> {
  return apiFetch('/chatbot/models/custom', {
    method: 'POST',
    body: JSON.stringify({ name, label: label || null }),
  })
}

/** PATCH /chatbot/models/custom — 커스텀 모델 표시 이름 변경 */
export async function renameCustomModel(modelId: string, label: string): Promise<void> {
  await apiFetch<void>('/chatbot/models/custom', {
    method: 'PATCH',
    body: JSON.stringify({ modelId, label }),
  })
}

/** POST /chatbot/models/custom/delete — 다운로드 취소·파일 삭제 후 커스텀 목록에서 제거 */
export async function deleteCustomModel(modelId: string): Promise<{ deleted: boolean; sizeBytes: number }> {
  return apiFetch('/chatbot/models/custom/delete', {
    method: 'POST',
    body: JSON.stringify({ modelId }),
  })
}

/** 바로 대화에 쓸 수 있는 모델인지 (로컬은 받아져 있어야 함, 상태를 모르는 런타임은 시도 허용) */
export function isUsable(m: ModelOption): boolean {
  return !m.local || m.localStatus === 'installed' || m.localStatus === 'unmanaged'
}

/** 로컬 모델이 쓸 수 없을 때 선택 목록·설정 화면에 붙일 짧은 사유 */
export function unusableReason(m: ModelOption): string | null {
  switch (m.localStatus) {
    case 'missing': return '다운로드 필요'
    case 'pulling': return m.pull?.percent != null ? `다운로드 중 ${Math.floor(m.pull.percent)}%` : '다운로드 중'
    case 'unreachable': return 'Ollama 연결 안 됨'
    default: return null
  }
}

/** 서버 정렬 순서를 유지한 채 그룹별로 묶는다 (그룹 순서 = 그룹이 처음 등장한 순서) */
export function groupModels(models: ModelOption[]): ModelGroup[] {
  const groups = new Map<string, ModelOption[]>()
  for (const m of models) {
    const list = groups.get(m.group)
    if (list) list.push(m)
    else groups.set(m.group, [m])
  }
  return [...groups].map(([group, list]) => ({ group, models: list }))
}

/**
 * 처음 선택할 모델 ID.
 * 기본 모델이 사용 가능하면 그것, 아니면 사용 가능한 첫 모델, 그것도 없으면 첫 모델.
 */
export function defaultModelId(models: ModelOption[]): string {
  const usable = models.filter(isUsable)
  return (usable.find((m) => m.isDefault) ?? usable[0] ?? models[0])?.id ?? ''
}

/** 1,234,567,890 바이트 → '1.2 GB' */
export function formatBytes(n: number): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1000)))
  return `${(n / 1000 ** i).toFixed(i >= 3 ? 1 : 0)} ${units[i]}`
}

// ────────────── API 모델 (Google·OpenAI·Anthropic 탭) ──────────────

/** API 제공사 — 모델 ID 접두사와 같다 */
export type ApiProvider = 'gemini' | 'openai' | 'anthropic'

export const API_PROVIDERS: { id: ApiProvider; label: string }[] = [
  { id: 'gemini', label: 'Google' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'anthropic', label: 'Anthropic' },
]

/** 모델 ID의 API 제공사 (API 모델이 아니면 null) */
export function apiProviderOf(modelId: string): ApiProvider | null {
  const prefix = modelId.split('/', 1)[0]
  return API_PROVIDERS.some((p) => p.id === prefix) ? (prefix as ApiProvider) : null
}

/** 설치된 LiteLLM이 아는, 추가할 수 있는 모델 하나 */
export interface CatalogModel {
  id: string
  name: string
  /** 100만 토큰당 USD */
  inputPer1M: number | null
  outputPer1M: number | null
  contextTokens: number | null
  /** 서비스 종료 예정일 (YYYY-MM-DD) */
  deprecationDate: string | null
  supportsVision: boolean
  supportsReasoning: boolean
  /** 이미 목록에 있는지 */
  added: boolean
}

export interface ProviderCatalog {
  provider: ApiProvider
  group: string
  litellmVersion: string
  apiKeyConfigured: boolean
  /** 키를 넣을 .env 변수 이름 */
  apiKeyEnv: string
  models: CatalogModel[]
  /** 삭제했던 기본 제공 모델 (복원 가능) */
  removedDefaults: { id: string; label: string }[]
}

/** GET /chatbot/models/catalog?provider= */
export async function getCatalog(provider: ApiProvider): Promise<ProviderCatalog> {
  return apiFetch<ProviderCatalog>(`/chatbot/models/catalog?provider=${provider}`)
}

/** POST /chatbot/models/api — 카탈로그 모델 추가 */
export async function addApiModel(modelId: string): Promise<{ modelId: string; label: string }> {
  return apiFetch('/chatbot/models/api', { method: 'POST', body: JSON.stringify({ modelId }) })
}

/** POST /chatbot/models/api/delete — API 모델 삭제 (기본 제공 모델도 가능, 기본 모델은 불가) */
export async function deleteApiModel(modelId: string): Promise<{ deleted: boolean; wasDefault: boolean }> {
  return apiFetch('/chatbot/models/api/delete', { method: 'POST', body: JSON.stringify({ modelId }) })
}

/** POST /chatbot/models/api/restore — 삭제했던 기본 제공 모델 되살리기 */
export async function restoreApiDefaults(provider: ApiProvider): Promise<{ restored: number }> {
  return apiFetch('/chatbot/models/api/restore', { method: 'POST', body: JSON.stringify({ provider }) })
}

// ────────────── Hugging Face (Inference Providers) ──────────────

/** POST /chatbot/models/hf — Hub에서 확인한 뒤 도구 호출을 지원하는 추론 제공사로 등록 */
export async function addHfModel(name: string): Promise<{ modelId: string; label: string } & HfMeta> {
  return apiFetch('/chatbot/models/hf', { method: 'POST', body: JSON.stringify({ name }) })
}

/** POST /chatbot/models/hf/delete */
export async function deleteHfModel(modelId: string): Promise<{ deleted: boolean }> {
  return apiFetch('/chatbot/models/hf/delete', { method: 'POST', body: JSON.stringify({ modelId }) })
}
