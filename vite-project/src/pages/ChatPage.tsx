import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useSessions } from '../context/SessionsContext'
import { sendChat, DEFAULT_MODEL } from '../services/chatApi'
import type { ChatMessage } from '../types'

const MODELS = [
  { value: 'gemini/gemini-2.5-flash-lite', label: 'Gemini 2.5 Flash Lite' },
  { value: 'openai/gpt-4o-mini', label: 'GPT-4o mini' },
  { value: 'anthropic/claude-sonnet-4-5', label: 'Claude Sonnet 4.5' },
  { value: 'ollama/llama3.1', label: 'Llama 3.1 (로컬)' },
]

function uid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return 'm-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
}

export default function ChatPage() {
  const { sessionId = '' } = useParams()
  const location = useLocation()
  const { user } = useAuth()
  const { sessions, append } = useSessions()

  const session = sessions.find((s) => s.id === sessionId)
  const messages = session?.messages ?? []

  const [input, setInput] = useState('')
  const [model, setModel] = useState(DEFAULT_MODEL)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const bottomRef = useRef<HTMLDivElement>(null)
  const seededRef = useRef(false)

  // ChatHome 예시 질문에서 넘어온 초기 입력 프리필 (1회)
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
  }, [messages.length, sending])

  const handleSend = async () => {
    const text = input.trim()
    if (!text || sending || !user || !session) return

    setError(null)
    setInput('')

    const userMsg: ChatMessage = { id: uid(), role: 'user', content: text, createdAt: new Date().toISOString() }
    append(sessionId, userMsg)
    setSending(true)

    try {
      const reply = await sendChat({ userId: user.id, sessionId, message: text, model })
      append(sessionId, { id: uid(), role: 'assistant', content: reply, createdAt: new Date().toISOString() })
    } catch (err) {
      const msg = err instanceof Error ? err.message : '응답을 받지 못했습니다.'
      setError(msg)
      append(sessionId, {
        id: uid(),
        role: 'assistant',
        content: `⚠️ 오류가 발생했습니다: ${msg}\n\n백엔드 서버(uv run main.py)가 실행 중인지 확인해주세요.`,
        createdAt: new Date().toISOString(),
      })
    } finally {
      setSending(false)
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  if (!session) {
    return (
      <div className="chat">
        <div className="chat__notfound">
          <h2>세션을 찾을 수 없습니다</h2>
          <p>삭제되었거나 잘못된 주소일 수 있습니다.</p>
          <Link className="btn btn--primary" to="/chat">새 세션 시작</Link>
        </div>
      </div>
    )
  }

  return (
    <div className="chat">
      <header className="chat__header">
        <div className="chat__header-title" title={session.title}>{session.title}</div>
        <label className="chat__model">
          <span>모델</span>
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {MODELS.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </label>
      </header>

      <div className="chat__scroll">
        <div className="chat__messages">
          {messages.length === 0 && (
            <div className="chat__hint">
              <div className="chat__hint-badge" aria-hidden>💬</div>
              <p>궁금한 법률 질문을 입력해보세요.<br />관련 법령과 판례를 근거로 답변해드립니다.</p>
            </div>
          )}

          {messages.map((m) => (
            <div key={m.id} className={`msg msg--${m.role}`}>
              <div className="msg__avatar" aria-hidden>{m.role === 'user' ? (user?.name?.[0]?.toUpperCase() ?? '나') : '⚖️'}</div>
              <div className="msg__bubble">{m.content}</div>
            </div>
          ))}

          {sending && (
            <div className="msg msg--assistant">
              <div className="msg__avatar" aria-hidden>⚖️</div>
              <div className="msg__bubble msg__bubble--typing">
                <span className="dot" /><span className="dot" /><span className="dot" />
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </div>

      <div className="composer">
        {error && <div className="composer__error">{error}</div>}
        <div className="composer__box">
          <textarea
            className="composer__input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="법률 질문을 입력하세요.  (Enter 전송 · Shift+Enter 줄바꿈)"
            rows={1}
            disabled={sending}
          />
          <button className="btn btn--primary composer__send" onClick={handleSend} disabled={sending || !input.trim()}>
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
