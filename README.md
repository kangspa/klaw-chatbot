# Docker 명령어 구성

| 명령어 | 동작 설명 |
| --- | --- |
| `docker compose up -d` | 멈춰있던 컨테이너를 그대로 재시작 (기존 이미지 재사용) |
| `docker compose up -d --build` | `build` 지시가 있는 서비스만 재빌드 후 기동 |
| `docker compose up -d --build --force-recreate` | 이미지 변경 사항이 없더라도 컨테이너를 강제로 재생성 |
| `docker compose up -d --build --no-cache` | 빌드 캐시를 무시하고 처음부터 빌드 (Dockerfile 수정 시 캐시 문제 의심될 때 사용) |
| `docker compose down -v` | 컨테이너와 함께 볼륨까지 제거 (**DB 데이터 완전 초기화 주의** ⚠️) |

- DB 데이터까지 날리기
```bash
docker compose down -v
docker compose up -d --build --no-cache
```

- 이미지 재빌드 및 컨테이너 실행
```bash
docker compose down
docker compose up -d --build
```

# API Request Body Sample

```json
{
  "userId": "admin",
  "sessionId": "test",
  "isotime": "2026-06-02T20:30:00+09:00",
  "message": "한번 일반적인 대화도 잘 받아쳐주는지 궁금한데, 오늘자 한국에서 발생한 뉴스에 대해서 하나 아무거나 알려줄 수 있어?",
  "model": "gemini/gemini-2.5-flash-lite"
}
```

# 대화 세션 저장소 구조

세 저장소가 각각 다른 역할을 맡고, 두 개의 키로 서로 연결된다.

| 저장소 | 역할 | 단위 |
| --- | --- | --- |
| **Postgres** | 사용자에게 그대로 보여줄 대화 원문 (진실 원본) | 메시지 1건 = 1행 |
| **Qdrant** | AI가 과거 대화를 의미 검색하기 위한 벡터 | 대화 1턴(질문+답변) = 1 point |
| **LangGraph checkpointer** | 진행 중인 대화의 실행 상태 (MemorySaver, 서버 재시작 시 휘발) | thread 1개 = 세션 1개 |

연결 키

- `chat_messages.turn_id` = **Qdrant point id**. 같은 턴의 질문/답변 두 행이 이 값을 공유한다.
- `chat_messages.graph_message_id` = LangGraph state 상의 message id. state를 어느 지점부터 잘라낼지 판단하는 기준.

세션 id 하나가 곧 `LangGraph thread_id` = `Qdrant payload.thread_id` = `chat_sessions.id` 이다.

# 대화 세션 관리 API

| Method | Path | 동작 |
| --- | --- | --- |
| `GET` | `/chatbot/sessions?userId=` | 세션 목록 (메시지 포함, 최근 활동순) |
| `POST` | `/chatbot/sessions` | 세션 생성 `{ userId, title? }` |
| `GET` | `/chatbot/sessions/{sessionId}` | 세션 + 메시지 전체 |
| `PATCH` | `/chatbot/sessions/{sessionId}` | 제목 변경 `{ title }` |
| `DELETE` | `/chatbot/sessions/{sessionId}` | 세션 삭제 (Postgres 행 + Qdrant thread 벡터 전체 + state) |
| `DELETE` | `/chatbot/sessions/{sessionId}/messages/{messageId}` | **① 삭제** |
| `POST` | `/chatbot/sessions/{sessionId}/messages/{messageId}/branch` | **② 브랜치** `{ userId, title? }` |
| `POST` | `/chatbot/sessions/{sessionId}/messages/{messageId}/regenerate` | **③ 다시 실행** `{ userId, isotime?, model? }` |

세 버튼이 각 저장소에 적용되는 범위

| 버튼 | Postgres | Qdrant | LangGraph state |
| --- | --- | --- | --- |
| **① 삭제** | 같은 `turn_id`의 질문+답변 행 삭제 | 해당 `turn_id` point 삭제 | 그 턴의 첫 메시지부터 이후 전부 제거 (중간 ToolMessage 포함) |
| **② 브랜치** | 기준 메시지까지를 새 `session_id`로 복사 (새 id / 새 `turn_id`) | 원본 point를 벡터·payload 그대로 복제, id와 `thread_id`만 교체 (임베딩 재호출 없음) | 기준 메시지까지 잘라 새 thread로 복사 |
| **③ 다시 실행** | 기존 assistant 행을 UPDATE (id·seq 유지 → 순서 불변) | **같은 `turn_id`로 upsert → 기존 point 덮어쓰기** | 해당 질문 직전까지 롤백 후 재실행 (요약 채널도 직전 체크포인트 값으로 복원) |

> ① 삭제가 턴 단위인 이유: Qdrant는 질문+답변을 point 1개로 저장하므로, 답변만 지우면 벡터에 반쪽짜리 기록이 남는다.
> 따라서 답변을 지우면 짝이 되는 질문도 함께 사라진다 (프론트에서 확인 다이얼로그로 안내).

# 실행 전 준비

```bash
docker compose up -d      # postgres / qdrant / searxng / korean-law-mcp
uv sync                   # asyncpg 등 의존성 설치
uv run main.py            # 서버 기동 (startup에서 테이블·Qdrant 컬렉션 자동 생성)
```

`.env`의 `DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD` 는 `docker-compose.yml`의 postgres 서비스와 값이 일치해야 한다.
Postgres 연결에 실패하면 서버는 뜨지만 `/chatbot/sessions/*` 는 503을 반환한다 (서버 로그의 `[Postgres]` 항목 확인).