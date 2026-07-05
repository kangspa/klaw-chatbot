import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useSessions } from '../context/SessionsContext'

const EXAMPLES = [
  '전세금을 못 받고 있는데 어떻게 대응해야 하나요?',
  '근로기준법 제60조 연차휴가 규정 알려줘',
  '음주운전 처벌 기준이 어떻게 되나요?',
  '상가 임대차 계약 해지 시 주의할 점은?',
]

export default function ChatHome() {
  const { user } = useAuth()
  const { createNew } = useSessions()
  const navigate = useNavigate()

  const start = (seed?: string) => {
    const s = createNew()
    if (!s) return
    // 예시 질문을 클릭했다면 새 세션으로 이동하며 초기 입력을 함께 전달
    navigate(`/chat/${s.id}`, { state: seed ? { seed } : undefined })
  }

  return (
    <div className="home">
      <div className="home__inner">
        <div className="home__badge" aria-hidden>⚖️</div>
        <h1 className="home__title">{user?.name}님, 무엇을 도와드릴까요?</h1>
        <p className="home__desc">
          법령·판례·행정규칙을 실시간으로 조회해 근거와 함께 답변하는 국내법 상담 챗봇입니다.<br />
          새 세션을 만들어 질문을 시작해보세요.
        </p>

        <button className="btn btn--primary btn--lg" onClick={() => start()}>
          <span aria-hidden>＋</span> 새 세션 시작
        </button>

        <div className="home__examples">
          {EXAMPLES.map((q) => (
            <button key={q} className="home__example" onClick={() => start(q)}>
              {q}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
