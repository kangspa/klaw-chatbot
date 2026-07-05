import { Link } from 'react-router-dom'
import { useSessions } from '../context/SessionsContext'
import { toSummary } from '../services/sessions'
import { formatWhen } from '../services/time'

export default function HistoryPage() {
  const { sessions } = useSessions()
  const summaries = sessions.map(toSummary) // sessions는 이미 최신순 정렬

  return (
    <div className="history">
      <header className="history__header">
        <h1 className="history__title">대화 내역</h1>
        <p className="history__desc">세션별로 정리된 상담 요약입니다. 카드를 눌러 이어서 대화하세요.</p>
      </header>

      {summaries.length === 0 ? (
        <div className="history__empty">
          <p>아직 저장된 대화가 없습니다.</p>
          <Link className="btn btn--primary" to="/chat">새 세션 시작</Link>
        </div>
      ) : (
        <div className="history__grid">
          {summaries.map((s) => (
            <Link key={s.id} to={`/chat/${s.id}`} className="summary-card">
              <div className="summary-card__top">
                <h2 className="summary-card__title">{s.title}</h2>
                <span className="summary-card__count">{s.messageCount}개 메시지</span>
              </div>

              <div className="summary-card__row">
                <span className="summary-card__tag">질문</span>
                <p className="summary-card__text">{s.firstUserMessage}</p>
              </div>

              {s.lastMessage && (
                <div className="summary-card__row">
                  <span className="summary-card__tag summary-card__tag--answer">최근</span>
                  <p className="summary-card__text summary-card__text--muted">{s.lastMessage}</p>
                </div>
              )}

              <div className="summary-card__foot">
                <span>생성 {formatWhen(s.createdAt)}</span>
                <span>업데이트 {formatWhen(s.updatedAt)}</span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
