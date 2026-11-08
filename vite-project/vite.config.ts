import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 개발 중 프론트의 /auth/*, /chatbot/* 호출을 FastAPI 백엔드(8008)로 프록시.
    // same-origin으로 요청되어 API base URL을 코드에 박지 않아도 되고 CORS 신경 쓸 필요가 없다.
    proxy: {
      '/auth': {
        target: 'http://127.0.0.1:8008',   // 백엔드는 127.0.0.1에만 열린다 (localhost는 ::1로 먼저 풀릴 수 있다)
        changeOrigin: true,
      },
      '/chatbot': {
        target: 'http://127.0.0.1:8008',   // 백엔드는 127.0.0.1에만 열린다 (localhost는 ::1로 먼저 풀릴 수 있다)
        changeOrigin: true,
      },
    },
  },
})
