import { formatBytes, type ModelOption, type PullState } from '../../services/models'

/** 마이 페이지의 모델 행들이 함께 쓰는 작은 조각 (상태 배지 · 다운로드 진행률 · 사용 스위치) */

export function LocalBadge({ model: m, pulling }: { model: ModelOption; pulling: boolean }) {
  if (pulling) return <span className="badge badge--info">다운로드 중</span>
  switch (m.localStatus) {
    case 'installed': return <span className="badge badge--ok">설치됨</span>
    case 'missing': return <span className="badge badge--warn">미설치</span>
    case 'unreachable': return <span className="badge badge--muted">상태 확인 불가</span>
    default: return <span className="badge badge--muted">로컬</span>
  }
}

export function PullProgress({ pull }: { pull: PullState | null }) {
  return (
    <div className="pull-progress" aria-live="polite">
      <div className="pull-progress__bar">
        <span style={{ width: `${pull?.percent ?? 0}%` }} />
      </div>
      <div className="pull-progress__meta">
        {pull?.percent != null ? `${Math.floor(pull.percent)}%` : '준비 중'}
        {pull && pull.total > 0 && ` · ${formatBytes(pull.completed)} / ${formatBytes(pull.total)}`}
        {pull?.message && ` · ${pull.message}`}
      </div>
    </div>
  )
}

interface ModelSwitchProps {
  model: ModelOption
  saving: boolean
  /** 켜진 모델이 이것 하나뿐이면 끌 수 없다 (채팅에 쓸 모델이 없어지므로) */
  isLastEnabled: boolean
  onToggle: () => void
}

export function ModelSwitch({ model: m, saving, isLastEnabled, onToggle }: ModelSwitchProps) {
  return (
    <label
      className="switch"
      title={isLastEnabled ? '모델을 하나 이상 켜 두어야 합니다.' : m.enabled ? '사용 안 함으로 바꾸기' : '사용으로 바꾸기'}
    >
      <input
        type="checkbox"
        checked={m.enabled}
        disabled={saving || isLastEnabled}
        onChange={onToggle}
        aria-label={`${m.label} 사용`}
      />
      <span className="switch__track" aria-hidden><span className="switch__thumb" /></span>
      <span className="switch__label">{m.enabled ? '사용' : '사용 안 함'}</span>
    </label>
  )
}

/** Ollama 모델의 디스크 크기·예상 VRAM과, 이 PC GPU에 다 올라가는지 */
export function ModelHardware({ model: m }: { model: ModelOption }) {
  if (!m.local || (m.downloadBytes == null && m.vramBytes == null)) return null
  // 작업 관리자·Ollama GPU 표시와 같은 1024 기준 (16GB 그래픽카드가 17.1GB로 보이지 않게)
  const gb = (b: number) => `${(b / 1024 ** 3).toFixed(1)} GB`
  const ctx = m.numCtx ? `${Math.round(m.numCtx / 1024)}K` : null
  let fit: { tone: string; text: string } | null = null
  if (m.vramBytes && m.gpuVramBytes) {
    fit = m.vramBytes <= m.gpuVramBytes * 0.95
      ? { tone: 'ok', text: `이 PC GPU(${gb(m.gpuVramBytes)})에 올라감` }
      : { tone: 'warn', text: `이 PC GPU(${gb(m.gpuVramBytes)})보다 큼 — 일부를 CPU로 돌려 느려짐` }
  }
  return (
    <div className="model-hw">
      {m.downloadBytes != null && <span>디스크 {gb(m.downloadBytes)}</span>}
      {m.vramBytes != null && (
        <span title="모델 가중치 + 대화 문맥(KV 캐시) + 계산용 여유. 문맥을 짧게 쓰면 실제로는 더 적게 씁니다.">
          예상 VRAM 최대 {gb(m.vramBytes)}{ctx ? ` (문맥 ${ctx})` : ''}
        </span>
      )}
      {fit && <span className={`badge badge--${fit.tone}`}>{fit.text}</span>}
    </div>
  )
}
