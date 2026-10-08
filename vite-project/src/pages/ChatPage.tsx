import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useSessions } from '../context/SessionsContext'
import { sendChat } from '../services/chatApi'
import { defaultModelId, groupModels, listModels, type ModelOption } from '../services/models'
import { branchFromMessage, deleteMessage, getSession, regenerateMessage } from '../services/sessions'
import { localIsoWithOffset } from '../services/time'
import type { ChatMessage } from '../types'

/*
 * 모델 목록은 백엔드 DB(llm_models)에서 받아온다 (services/models.ts).
 * 법률 검색 노드의 재검색 루프는 모델의 도구 호출(tool calling) 능력에 크게 좌우된다.
 * (도구 호출을 잘 못하면 코드가 재검색을 강제하는 폴백으로 넘어간다 — agent.py의 force_next_search)
 */

function uid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return 'm-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
}

/** 진행 중인 메시지 단위 작업 (버튼 중복 클릭 방지 + 스피너 표시용) */
type BusyAction = 'delete' | 'branch' | 'regenerate'

export default function ChatPage() {
  const { sessionId = '' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { sessions, loading, applyMessages, upsert, refresh } = useSessions()

  const session = sessions.find((s) => s.id === sessionId)
  const messages = session?.messages ?? []

  const [input, setInput] = useState('')
  // 빈 문자열이면 서버 기본 모델을 쓴다 (목록을 받기 전에 보내는 경우 포함)
  const [model, setModel] = useState('')
  const [models, setModels] = useState<ModelOption[]>([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // 낙관적 렌더링: 서버 응답 전까지 화면에만 보여줄 사용자 메시지
  const [pending, setPending] = useState<ChatMessage | null>(null)
  const [busy, setBusy] = useState<{ id: string; action: BusyAction } | null>(null)

  const bottomRef = useRef<HTMLDivElement>(null)
  const seededRef = useRef(false)
  // 목록에 없는 세션을 서버에서 직접 받아오는 시도는 sessionId마다 1회만 (실패 시 무한 재요청 방지)
  const fetchedRef = useRef<Set<string>>(new Set())
  // 서버 직접 조회를 마친 sessionId. 이걸로 '조회 중' 여부를 파생시킨다(effect 내 동기 setState 회피).
  const [probed, setProbed] = useState<string | null>(null)

  const visibleMessages = pending ? [...messages, pending] : messages
  const isBusy = sending || busy !== null
  const resolving = !session && !loading && probed !== sessionId

  /**
   * 세션 목록에 아직 없는 세션을 열었을 때 서버에서 직접 조회해 채운다.
   * 직접 URL 진입, 다른 탭에서 만든 세션, 최초 목록 로딩과의 경합을 모두 흡수한다.
   */
  useEffect(() => {
    if (session || loading || !sessionId) return
    if (fetchedRef.current.has(sessionId)) return
    fetchedRef.current.add(sessionId)

    let alive = true
    getSession(sessionId).then(
      (s) => { if (alive) { upsert(s); setProbed(sessionId) } },
      () => { if (alive) setProbed(sessionId) },   // 실제로 없는 세션 → notfound 화면 유지
    )
    return () => { alive = false }
  }, [session, loading, sessionId, upsert])

  // 선택 가능한 모델 목록을 받아오고, 아직 고른 모델이 없으면 기본 모델을 선택한다
  useEffect(() => {
    let alive = true
    listModels().then(
      (list) => {
        if (!alive) return
        setModels(list)
        setModel((cur) => (list.some((m) => m.id === cur) ? cur : defaultModelId(list)))
      },
      () => { if (alive) setError('모델 목록을 불러오지 못했습니다. 기본 모델로 전송됩니다.') },
    )
    return () => { alive = false }
  }, [])

  // 대시보드 예시 질문에서 넘어온 초기 입력 프리필 (1회)
  useEffect(() => {
    const seed = (location.state as { seed?: string } | null)?.seed
    if (seed && !seededRef.current) {
      seededRef.current = true
      setInput(seed)
      window.history.replaceState({}, '') // 뒤로가기 시 재적용 방지
    }
  }, [location.state])

  // 새 메시지/전송상태 변화 시 하단으로 스크롤
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [visibleMessages.length, sending, busy])

  const handleSend = async () => {
    const text = input.trim()
    if (!text || isBusy || !user || !session) return

    setError(null)
    setNotice(null)
    setInput('')
    setPending({ id: uid(), role: 'user', content: text, createdAt: new Date().toISOString(), pending: true })
    setSending(true)

    try {
      const result = await sendChat({ sessionId, message: text, model: model || undefined })
      // 서버가 Postgres에 저장한 실제 행(id/seq/turnId 포함)으로 교체한다.
      // 이 id들이 있어야 삭제/브랜치/재생성 버튼이 동작한다.
      // 첫 메시지면 백엔드가 세션 제목도 자동 생성하므로 result.session으로 함께 반영한다.
      if (result.userMessage && result.assistantMessage) {
        applyMessages(
          sessionId,
          [...messages, result.userMessage, result.assistantMessage],
          result.session ?? undefined,
        )
      } else {
        await refresh()
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '응답을 받지 못했습니다.'
      setError(`${msg}\n백엔드 서버(uv run main.py)와 Postgres 컨테이너가 실행 중인지 확인해주세요.`)
    } finally {
      setPending(null)
      setSending(false)
    }
  }

  /** ① 삭제 — 이 답변과 짝이 되는 질문을 Postgres·Qdrant·대화 상태에서 모두 제거 */
  const handleDelete = async (message: ChatMessage) => {
    if (isBusy) return
    const confirmText = message.turnId
      ? '이 답변과 짝이 되는 질문까지 함께 삭제됩니다.\n(대화 검색용 벡터 기록도 함께 제거됩니다)\n\n계속할까요?'
      : '이 메시지를 삭제할까요?'
    if (!confirm(confirmText)) return

    setError(null)
    setNotice(null)
    setBusy({ id: message.id, action: 'delete' })
    try {
      const result = await deleteMessage(sessionId, message.id)
      applyMessages(sessionId, result.messages)
      setNotice('대화 내역에서 삭제했습니다.')
    } catch (err) {
      setError(err instanceof Error ? err.message : '삭제하지 못했습니다.')
    } finally {
      setBusy(null)
    }
  }

  /** ② 브랜치 — 이 메시지까지의 대화를 물려받는 새 세션 생성 후 이동 */
  const handleBranch = async (message: ChatMessage) => {
    if (isBusy || !user || !session) return
    if (!confirm('이 메시지까지의 대화를 복사해 새 세션(분기)을 만듭니다.\n계속할까요?')) return

    setError(null)
    setNotice(null)
    setBusy({ id: message.id, action: 'branch' })
    try {
      const result = await branchFromMessage(sessionId, message.id)
      await refresh()
      navigate(`/chat/${result.session.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '브랜치를 만들지 못했습니다.')
    } finally {
      setBusy(null)
    }
  }

  /** ③ 다시 실행 — 같은 질문으로 답변 재생성 (Qdrant의 같은 벡터를 덮어씀) */
  const handleRegenerate = async (message: ChatMessage) => {
    if (isBusy || !user) return

    setError(null)
    setNotice(null)
    setBusy({ id: message.id, action: 'regenerate' })
    try {
      const result = await regenerateMessage(
        sessionId, message.id, localIsoWithOffset(), model || undefined,
      )
      applyMessages(sessionId, result.messages)
      setNotice('답변을 다시 생성했습니다.')
    } catch (err) {
      setError(err instanceof Error ? err.message : '답변을 다시 생성하지 못했습니다.')
    } finally {
      setBusy(null)
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  if (!session) {
    const busyLoading = loading || resolving
    return (
      <div className="chat">
        <div className="chat__notfound">
          <h2>{busyLoading ? '세션을 불러오는 중…' : '세션을 찾을 수 없습니다'}</h2>
          {!busyLoading && (
            <>
              <p>삭제되었거나 잘못된 주소일 수 있습니다.</p>
              <Link className="btn btn--primary" to="/dashboard">대시보드로 가기</Link>
            </>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="chat">
      <header className="chat__header">
        <div className="chat__header-title" title={session.title}>
          {session.parentSessionId && <span className="chat__branch-badge" title="브랜치로 만들어진 세션">분기</span>}
          {session.title}
        </div>
        <label className="chat__model">
          <span>모델</span>
          <select value={model} onChange={(e) => setModel(e.target.value)} disabled={isBusy || models.length === 0}>
            {models.length === 0 && <option value="">불러오는 중…</option>}
            {groupModels(models).map((g) => (
              <optgroup key={g.group} label={g.group}>
                {g.models.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
      </header>

      <div className="chat__scroll">
        <div className="chat__messages">
          {visibleMessages.length === 0 && (
            <div className="chat__hint">
              <div className="chat__hint-badge" aria-hidden>💬</div>
              <p>궁금한 법률 질문을 입력해보세요.<br />관련 법령과 판례를 근거로 답변해드립니다.</p>
            </div>
          )}

          {visibleMessages.map((m) => {
            const busyAction = busy?.id === m.id ? busy.action : null
            const actionable = m.role === 'assistant' && !m.pending
            return (
              <div key={m.id} className={`msg msg--${m.role}`}>
                <div className="msg__avatar" aria-hidden>
                  {m.role === 'user' ? (user?.name?.[0]?.toUpperCase() ?? '나') : '⚖️'}
                </div>
                <div className="msg__body">
                  <div className={`msg__bubble${busyAction === 'regenerate' ? ' is-regenerating' : ''}`}>
                    {m.content}
                  </div>

                  {actionable && (
                    <div className="msg__actions">
                      <button
                        className="msg__action"
                        onClick={() => handleRegenerate(m)}
                        disabled={isBusy}
                        title="같은 질문으로 답변을 다시 생성합니다 (기존 답변과 대화 기록을 덮어씁니다)"
                      >
                        <span aria-hidden>↻</span>
                        {busyAction === 'regenerate' ? '재생성 중…' : '다시 실행'}
                      </button>
                      <button
                        className="msg__action"
                        onClick={() => handleBranch(m)}
                        disabled={isBusy}
                        title="이 메시지까지의 대화를 복사해 새 세션(분기)을 만듭니다"
                      >
                        <span aria-hidden>⑂</span>
                        {busyAction === 'branch' ? '분기 중…' : '브랜치'}
                      </button>
                      <button
                        className="msg__action msg__action--danger"
                        onClick={() => handleDelete(m)}
                        disabled={isBusy}
                        title="이 답변과 짝이 되는 질문을 대화 내역에서 삭제합니다"
                      >
                        <span aria-hidden>🗑</span>
                        {busyAction === 'delete' ? '삭제 중…' : '삭제'}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )
          })}

          {sending && (
            <div className="msg msg--assistant">
              <div className="msg__avatar" aria-hidden>⚖️</div>
              <div className="msg__body">
                <div className="msg__bubble msg__bubble--typing">
                  <span className="dot" /><span className="dot" /><span className="dot" />
                </div>
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </div>

      <div className="composer">
        {notice && <div className="composer__notice">{notice}</div>}
        {error && <div className="composer__error">{error}</div>}
        <div className="composer__box">
          <textarea
            className="composer__input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="법률 질문을 입력하세요.  (Enter 전송 · Shift+Enter 줄바꿈)"
            rows={1}
            disabled={isBusy}
          />
          <button className="btn btn--primary composer__send" onClick={handleSend} disabled={isBusy || !input.trim()}>
            전송
          </button>
        </div>
        <p className="composer__disclaimer">
          본 답변은 일반 정보 제공 목적이며, 구체적 사건은 변호사 상담이 필요합니다.
        </p>
      </div>
    </div>
  )
}
