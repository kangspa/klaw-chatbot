import { memo } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkCjkFriendly from 'remark-cjk-friendly'
import remarkGfm from 'remark-gfm'

/*
 * AI 답변용 마크다운 렌더러 (GitHub 방식: 표·체크리스트·취소선·자동 링크).
 * - 원문 HTML은 렌더링하지 않는다 (rehype-raw를 쓰지 않음 → 태그는 무시된다).
 * - javascript: 같은 위험한 주소는 react-markdown 기본 urlTransform이 걸러낸다.
 * - 스타일은 index.css의 `.md` 구역에 있다.
 */

// remarkCjkFriendly: 한국어에서 **굵게(괄호)**조사 처럼 문장부호 뒤에 조사가 붙어도 강조가 깨지지 않게
const remarkPlugins = [remarkGfm, remarkCjkFriendly]

const components: Components = {
  // 외부 링크는 항상 새 탭으로 — 앱 화면을 벗어나지 않게
  a: ({ href, title, children }) => {
    // 위험한 주소라 기본 urlTransform이 비운 경우: 링크 없이 글자만
    if (!href) return <span>{children}</span>
    // 같은 답변 안의 각주(#user-content-fn-1 등)는 현재 탭에서 이동
    if (href.startsWith('#')) return <a href={href} title={title}>{children}</a>
    return (
      <a href={href} title={title} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    )
  },
  // 넓은 표는 말풍선 안에서 가로 스크롤
  table: ({ children }) => (
    <div className="md__table-wrap">
      <table>{children}</table>
    </div>
  ),
  img: ({ src, alt, title }) => (
    <img src={src} alt={alt ?? ''} title={title} loading="lazy" referrerPolicy="no-referrer" />
  ),
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
}

// 스트리밍 중 다른 메시지가 바뀔 때 이미 끝난 답변을 다시 파싱하지 않도록 memo
export default memo(Markdown)
