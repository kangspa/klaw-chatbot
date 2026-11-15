import { readFileSync } from 'node:fs'
import http from 'node:http'
import { fileURLToPath } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// 백엔드가 이번 실행에 고른 웹 포트 (api/stack.py ensure_web_port가 .runtime/web_port에 적는다).
// 다른 프로그램이 8008을 쓰고 있으면 백엔드가 8009~8012 등 빈 포트로 뜨므로, 요청마다 읽어서 따라간다.
const PORT_FILE = fileURLToPath(new URL('../.runtime/web_port', import.meta.url))
const backendPort = () => {
  try {
    return Number(readFileSync(PORT_FILE, 'utf-8').trim()) || 8008
  } catch {
    return 8008
  }
}

/** 개발 중 /auth/*, /chatbot/* 호출을 FastAPI 백엔드로 넘긴다 (same-origin이라 CORS·API 주소 설정이 필요 없다). */
function backendProxy(prefixes: string[]): Plugin {
  return {
    name: 'klaw-backend-proxy',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url || !prefixes.some((p) => req.url!.startsWith(p))) return next()
        const port = backendPort()
        // 백엔드는 127.0.0.1에만 열린다 (localhost는 ::1로 먼저 풀릴 수 있다)
        const upstream = http.request(
          { host: '127.0.0.1', port, path: req.url, method: req.method, headers: { ...req.headers, host: `127.0.0.1:${port}` } },
          (up) => {
            res.writeHead(up.statusCode ?? 502, up.headers)
            up.pipe(res)
          },
        )
        upstream.on('error', (e) => {
          res.statusCode = 502
          res.setHeader('Content-Type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ detail: `백엔드(127.0.0.1:${port})에 연결할 수 없습니다: ${e.message}` }))
        })
        req.pipe(upstream)
      })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), backendProxy(['/auth', '/chatbot'])],
})
