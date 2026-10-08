import type { UsageModel, UsageRow } from '../../services/dashboard'
import { formatCompact, formatUsd } from '../../services/dashboard'

/**
 * 대시보드 차트·표가 함께 쓰는 계열(모델) 정의와 지표 계산.
 * 색은 모델의 전체 목록 순서로 고정한다 (기간·필터가 바뀌어도 같은 모델은 같은 색).
 */

export type UsageMetric = 'cost' | 'tokens'

// 범주 팔레트 (밝은 화면용, 순서가 색약 대비를 보장하므로 바꾸지 않는다)
const PALETTE = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948']
export const OTHER = { key: '__other', label: '기타 (로컬 등)', color: '#9aa2b1' }

export interface Series {
  key: string
  label: string
  color: string
}

/** 모델 → 차트 계열 (색 고정) */
export function seriesOf(model: UsageModel): Series {
  if (model.order !== null && model.order < PALETTE.length) {
    return { key: model.id, label: model.label, color: PALETTE[model.order] }
  }
  return OTHER
}

/** 사용량 행 하나를 지표 값으로 */
export function metricValue(row: UsageRow, metric: UsageMetric): number {
  return metric === 'cost' ? row.costUsd : row.inputTokens + row.outputTokens
}

export function formatMetric(v: number, metric: UsageMetric): string {
  return metric === 'cost' ? formatUsd(v) : formatCompact(v)
}


/** 사용된 모델들의 계열 목록 (모델 순서대로, '기타'는 하나로 합쳐 맨 뒤) */
export function seriesList(models: UsageModel[]): Series[] {
  const out: Series[] = []
  for (const m of models) {
    const s = seriesOf(m)
    if (!out.some((x) => x.key === s.key)) out.push(s)
  }
  return [...out.filter((s) => s.key !== OTHER.key), ...out.filter((s) => s.key === OTHER.key)]
}
