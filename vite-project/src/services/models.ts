import { apiFetch } from './http'

/**
 * 선택 가능한 AI 모델 목록.
 * 모델·표시명·순서는 백엔드 DB(llm_models)에서 관리하므로 프론트는 받아서 보여주기만 한다.
 * (모델을 추가·숨기려면 DB 또는 api/llm_gateway/seed_models.json을 고친다 — 프론트 재빌드 불필요)
 */
export interface ModelOption {
  /** 요청 시 model 값으로 보낼 ID ('접두사/모델명') */
  id: string
  /** 선택 목록에 보일 이름 */
  label: string
  /** 선택 목록 그룹 (Google / OpenAI / Anthropic / 로컬 ...) */
  group: string
  isDefault: boolean
}

export interface ModelGroup {
  group: string
  models: ModelOption[]
}

/** GET /chatbot/models — 활성 모델 목록 (서버가 sort_order 순으로 정렬해 준다) */
export async function listModels(): Promise<ModelOption[]> {
  return apiFetch<ModelOption[]>('/chatbot/models')
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

/** 기본 모델 ID (isDefault가 없으면 첫 번째 모델) */
export function defaultModelId(models: ModelOption[]): string {
  return (models.find((m) => m.isDefault) ?? models[0])?.id ?? ''
}
