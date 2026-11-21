/**
 * KLaw 아이콘 (public/favicon.svg — 브라우저 탭·트레이·exe와 같은 그림).
 * 장식용이라 화면 읽기 프로그램에는 읽히지 않게 둔다 (옆에 'KLaw' 글자가 있다).
 */
export default function AppLogo({ size, className }: { size: number; className?: string }) {
  return <img src="/favicon.svg" width={size} height={size} alt="" aria-hidden className={className} draggable={false} />
}
