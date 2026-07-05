import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 개발 중 프론트의 /chatbot/* 호출을 FastAPI 백엔드(8008)로 프록시.
    // same-origin으로 요청되어 API base URL을 코드에 박지 않아도 되고 CORS 신경 쓸 필요가 없다.
    proxy: {
      '/chatbot': {
        target: 'http://localhost:8008',
        changeOrigin: true,
      },
    },
  },
})
