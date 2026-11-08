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
  "model": "gemini/gemini-3.5-flash-lite"
}
```

# 사용자 / Google 계정 연결 (선택)

이 앱은 웹 서비스가 아니라 **각자의 PC에서 띄워 쓰는 개인용 클라이언트**다. 그래서 **로그인 없이 모든 기능을 쓴다.**
서버가 처음 뜰 때 이 PC의 **로컬 사용자**(`users.is_local`) 하나를 만들고, 모든 API가 토큰 없이 그 사용자로 동작한다
(`api/auth/deps.py`). 모델 호출은 각자 입력한 API 키와 이 PC의 로컬 모델을 쓴다.

**Google 계정 연결은 선택 사항**이다. 연결한 계정의 개인 Google 드라이브에 대화 내역·API 키·설정을 저장하는 용도로 쓴다.
연결해도 사용자가 바뀌지 않고 **같은 로컬 사용자 행에 Google 정보만 채우므로**, 연결 전에 나눈 대화·설정이 연결 후·해제 후에도 그대로 이어진다.

```
[마이 페이지] Google 계정 연결 버튼 (GIS)
    │  ① Google이 서명한 ID 토큰(credential)
    ▼
[백엔드] POST /auth/google
    │  ② Google 공개키(JWKS)로 서명·aud·iss·exp 검증
    │  ③ 로컬 사용자 행에 google_sub·이메일·이름·사진 기록 (액세스 토큰은 발급하지 않는다)
```

- Google ID 토큰은 연결 순간에만 쓰고 저장하지 않는다. `google_sub`은 API 응답으로 내보내지 않는다.
- 예전(로그인 필수 시절)에 만든 Google 사용자만 있는 DB라면, 가장 최근에 쓴 사용자를 로컬 사용자로 삼아 그 데이터를 잇는다.

## 사용자 API

| Method | Path | 동작 |
| --- | --- | --- |
| `GET` | `/auth/config` | 프론트가 쓸 Google 클라이언트 ID (비밀값 아님, 없으면 연결 버튼 대신 안내) |
| `GET` | `/auth/me` | 로컬 사용자 정보와 Google 연결 여부 (`googleLinked`) |
| `POST` | `/auth/google` | ID 토큰 검증 → 로컬 사용자에 Google 계정 연결 |
| `POST` | `/auth/google/unlink` | 연결 해제 (데이터는 그대로) |

## Google Cloud Console 설정 (Google 계정 연결을 쓸 때만, 최초 1회)

1. [Google Cloud Console](https://console.cloud.google.com/) → **API 및 서비스 → OAuth 동의 화면** 구성 (외부 / 테스트 사용자에 본인 계정 추가)
2. **사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**
   - 애플리케이션 유형: **웹 애플리케이션**
   - **승인된 JavaScript 원본**: `http://localhost:5173` (Vite 개발 서버 주소)
   - 클라이언트 보안 비밀번호(secret)는 이 방식에서 쓰지 않는다
3. 발급된 클라이언트 ID를 `.env`의 `GOOGLE_CLIENT_ID=` 에 붙여넣고 서버 재시작

> 프론트에는 클라이언트 ID를 심지 않는다. `/auth/config`로 백엔드에서 받아가므로 앞뒤가 어긋날 일이 없다.

# 대화 세션 저장소 구조

네 저장소가 각각 다른 역할을 맡고, 두 개의 키로 서로 연결된다.

| 저장소 | 역할 | 단위 |
| --- | --- | --- |
| **Postgres (users)** | 회원 정보 | 회원 1명 = 1행 |
| **Postgres (chat_*)** | 사용자에게 그대로 보여줄 대화 원문 (진실 원본) | 메시지 1건 = 1행 |
| **Qdrant** | AI가 과거 대화를 의미 검색하기 위한 벡터 | 대화 1턴(질문+답변) = 1 point |
| **LangGraph checkpointer** | 진행 중인 대화의 실행 상태 (MemorySaver, 서버 재시작 시 휘발) | thread 1개 = 세션 1개 |

연결 키

- `chat_messages.turn_id` = **Qdrant point id**. 같은 턴의 질문/답변 두 행이 이 값을 공유한다.
- `chat_messages.graph_message_id` = LangGraph state 상의 message id. state를 어느 지점부터 잘라낼지 판단하는 기준.

세션 id 하나가 곧 `LangGraph thread_id` = `Qdrant payload.thread_id` = `chat_sessions.id` 이다.

# 대화 세션 관리 API

모두 `Authorization: Bearer <accessToken>` 필요. 소유자는 토큰에서 결정되므로 `userId`를 보내지 않는다.

| Method | Path | 동작 |
| --- | --- | --- |
| `GET` | `/chatbot/sessions` | 로컬 사용자의 세션 목록 (메시지 포함, 최근 활동순) |
| `POST` | `/chatbot/sessions` | 세션 생성 `{ title? }` |
| `GET` | `/chatbot/sessions/{sessionId}` | 세션 + 메시지 전체 |
| `PATCH` | `/chatbot/sessions/{sessionId}` | 제목 변경 `{ title }` |
| `DELETE` | `/chatbot/sessions/{sessionId}` | 세션 삭제 (Postgres 행 + Qdrant thread 벡터 전체 + state) |
| `DELETE` | `/chatbot/sessions/{sessionId}/messages/{messageId}` | **① 삭제** |
| `POST` | `/chatbot/sessions/{sessionId}/messages/{messageId}/branch` | **② 브랜치** `{ title? }` |
| `POST` | `/chatbot/sessions/{sessionId}/messages/{messageId}/regenerate` | **③ 다시 실행** `{ isotime?, model? }` |

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
docker compose up -d      # postgres / qdrant / searxng / korean-law-mcp / ollama
uv run python scripts/setup_local_llm.py --apply --pull   # 로컬 모델 GPU 가속 자동 구성 + 모델 받기 (선택)
uv sync                   # asyncpg, pyjwt 등 의존성 설치
uv run main.py            # 서버 기동 (startup에서 테이블·Qdrant 컬렉션 자동 생성)

cd vite-project && npm run dev   # 프론트 (http://localhost:5173)
```

`.env` 필수 항목

| 변수 | 설명 |
| --- | --- |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `docker-compose.yml`의 postgres 서비스와 값이 일치해야 한다 |
| `GOOGLE_CLIENT_ID` | (선택) Google OAuth 웹 클라이언트 ID. 비어 있어도 앱은 그대로 쓰고, 마이 페이지의 Google 계정 연결만 안내 문구로 바뀐다 |
| `ANNEX_OCR_ENABLED` | 이미지 기반 별표(PDF) 응답을 PaddleOCR로 텍스트화하는 후처리. 기본 켜짐, `0`이면 끔 |
| `CHAT_FALLBACK_MODELS` | 기본 모델 실패(429 등) 시 순차 폴백할 모델 목록 (쉼표 구분, 선택). DB에 등록·활성화된 모델만 사용 |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 각 클라우드 모델 호출용. Gemini 키는 임베딩(대화 기억)에도 쓰이므로 필수 |
| `LLM_REQUEST_TIMEOUT` | 클라우드 모델 요청 타임아웃(초). 기본 300. 초과 시 재시도·폴백 |
| `LLM_REGISTRY_TTL` | DB 모델 목록을 다시 읽는 주기(초). 기본 30 |
| `MCP_AUTH_TOKEN` | korean-law-mcp 접근 토큰. docker-compose가 컨테이너에 주입하고 앱이 Bearer 헤더로 보낸다 (**필수** — 없으면 compose가 기동을 거부) |
| `CRAWL_MAX_CHARS` | `page_crawl4ai` 결과 최대 길이(자). 기본 6000 — 페이지 전체를 넘기면 호출당 수만 토큰이 된다 |

## LLM 게이트웨이 (`api/llm_gateway`)

모델은 `접두사/모델명` 한 줄로 지정한다. **호출과 토큰 단가 계산은 LiteLLM**이 맡고, 게이트웨이는
LiteLLM 채팅 모델을 감싼 `GatewayChatModel`(입출력 보정·비용 집계용 표식)과 DB 모델 목록 관리를 담당한다.
LangGraph 노드에서 `ainvoke` / `bind_tools` / `with_structured_output`을 그대로 쓴다.

| 접두사 | LiteLLM 표기 | 대상 | 관련 환경 변수 (기본값) |
| --- | --- | --- | --- |
| `gemini/` | `gemini/` | Google Gemini API | `GEMINI_API_KEY` |
| `openai/` | `openai/` | OpenAI API | `OPENAI_API_KEY` |
| `anthropic/` | `anthropic/` | Anthropic API | `ANTHROPIC_API_KEY` |
| `ollama/` | `ollama_chat/` | Ollama (도구 호출 지원 chat API) | `OLLAMA_BASE_URL` (`http://localhost:11434`), `OLLAMA_NUM_CTX` (`32768`) |
| `lmstudio/` | `lm_studio/` | LM Studio | `LMSTUDIO_BASE_URL` (`http://localhost:1234/v1`) |
| `vllm/` | `hosted_vllm/` | vLLM | `VLLM_BASE_URL` (`http://localhost:8000/v1`), `VLLM_API_KEY` |

토큰 단가는 **LiteLLM 가격표**로 계산한다. LiteLLM은 서버 시작 시 최신 가격표를 받아 오고(실패하면 설치된
버전의 내장 가격표 사용), 캐시 토큰 할인과 장문 입력 구간 단가까지 반영한다. 새 모델·가격 변경을 직접 관리할
필요가 없다. 가격표에 없는 유료 모델은 0으로 집계하고 경고 로그를 남기며, 로컬 런타임은 0원이다.

### 모델 목록 관리 (Postgres)

호출 가능한 모델 목록은 Postgres의 `llm_models` 테이블에서 관리한다.
서버는 이 목록을 `LLM_REGISTRY_TTL`초(기본 30)마다 다시 읽으므로 **DB를 고치면 재시작·재빌드 없이 반영**된다.

- API로 받은 `model` 값은 DB에 등록·활성화된 모델만 허용한다 (아니면 400). 비우면 `is_default` 모델.
- 프론트 선택 목록은 `GET /chatbot/models`에서 받아온다 (표시명·그룹·순서 모두 DB 기준).
- `options`(JSONB)는 LiteLLM 호출 인자로 그대로 넘어간다 (예: Ollama `{"num_ctx": 32768}`).

### 사용자별 모델 설정 (마이 페이지)

`llm_models.enabled`가 서버 전체의 스위치라면, 사용자는 **마이 페이지(`/settings`) > 모델 설정**에서
채팅 화면 선택 목록에 보일 모델을 각자 켜고 끈다. 끈 모델만 `user_disabled_models`에 저장하므로
새로 추가된 모델은 모두에게 켜진 상태로 보인다.

- 이 설정은 **화면 정리용**이다. 백엔드는 꺼진 모델의 호출을 막지 않는다.
- **API 모델 (회사별 탭: Google · OpenAI · Anthropic)**: 기본 제공 모델(seed)에 더해 **지금 설치된 LiteLLM이 아는 모델**
  중에서 골라 추가·삭제한다. 목록은 LiteLLM에 내장된 가격표에서 채팅 모델 · 도구 호출 지원 · 종료일이 지나지 않은 것만 추린다
  (`api/llm_gateway/catalog.py`). LiteLLM을 올리면 고를 수 있는 모델도 늘어난다.
  - 추가한 모델은 `is_custom`으로 등록되고 temperature를 보내지 않는다 (최신 모델 대부분이 기본값 외 temperature를 거부).
  - 기본 제공 모델도 지울 수 있다. 지운 기본 모델은 `llm_models_removed`에 기억해 `sync_llm_models.py`가 되살리지 않으며,
    탭의 **기본 모델 복원**으로 되살린다. 기본 모델(`is_default`)은 지울 수 없다.
  - 회사 API 키(`GEMINI_API_KEY` 등)가 `.env`에 없으면 탭에 경고를 띄운다.
- **Ollama 탭**: 등록된 Ollama 모델의 다운로드·삭제. 로컬 런타임은 Ollama만 쓰고, **이 PC에서 도는 모델만** 쓴다
  (컨테이너에 `OLLAMA_NO_CLOUD=1` — 클라우드 모델 실행이 막히고, 커스텀 모델 추가에서도 `…-cloud` 태그를 거부한다).
- Hugging Face **클라우드 추론(Inference Providers)은 지원하지 않는다**. 호출 계층에 `huggingface/` 접두사가 없어
  그런 모델 ID는 등록·호출 단계에서 거부된다. HF는 모델 **다운로드** 인증에만 쓴다 (아래 API 키 관리).
- 아직 받지 않은 로컬 모델로 채팅하면 `400 "… 마이 페이지 > 모델 설정에서 다운로드하세요."`를 돌려준다.
- 로컬(Ollama) 모델은 마이 페이지의 **다운로드** 버튼으로 받고, **삭제** 버튼으로 지운다. 서버가 백그라운드로 받고, 화면은 진행률을 보여준다
  (페이지를 떠나도 계속 받는다). Ollama는 서버 전체가 공유하므로 한 번 받으면 모든 사용자에게 설치된 것으로 보인다.
  받을 수 있는 모델은 `llm_models`에 등록된 Ollama 모델로 한정된다.
- 받은 로컬 모델은 같은 화면의 **삭제** 버튼으로 지워 디스크를 정리한다. 서버 전체에서 지워지며, 목록 항목은 남으므로
  다시 다운로드하면 된다. **로컬 저장 공간** 영역에 전체 사용량과, 목록에서 빠졌는데 파일만 남은 모델이 함께 보인다.
- **커스텀 모델**: [Ollama 모델 목록](https://ollama.com/search)의 모델명(예: `qwen3:1.7b`)을 입력하고 **추가**를 누르면
  서버가 받기 시작하고 `llm_models`에 `is_custom`으로 등록된다. 채팅 선택 목록의 '커스텀' 그룹에 보이며, 표시 이름은 ✎로 바꾼다.
  - 없는 모델명은 Ollama가 몇 초 안에 거절하므로 등록하지 않고 오류만 보여준다. `ollama run …`·ollama.com 주소를 붙여 넣어도 모델명만 쓴다.
  - 도구 호출(tools)을 지원하지 않는 모델은 경고를 표시한다. 이 챗봇은 법령·웹 검색에 도구를 쓰므로 답변이 실패할 수 있다.
  - 최신 모델 중에는 더 새 Ollama가 필요한 것이 있다 (예: `lfm3-omni:2b`). 이때는 런타임 버전 카드에서 Ollama를 올린다.
  - Hugging Face GGUF 저장소는 `hf.co/org/repo`로 넣으면 `huggingface.co/…`로 바꿔 받는다 (Ollama가 `hf.co`로는 승인 저장소의
    인증 단계에서 'realm host' 오류로 멈추기 때문). 승인(gated)·비공개 저장소는 API 키 화면의 Ollama SSH 공개키를 HF 계정에 등록해야 한다.
  - 삭제는 받는 중이면 취소하고, 받아져 있으면 파일까지 지운 뒤 목록에서 뺀다.
  - `sync_llm_models.py --prune`은 커스텀 모델을 지우지 않는다.

| API | 용도 |
| --- | --- |
| `GET /chatbot/models` | 활성 모델 + 사용자 사용 여부(`enabled`) + 로컬 상태(`localStatus`: installed·missing·pulling·unreachable·unmanaged) |
| `PUT /chatbot/models/preference` | `{modelId, enabled}` 사용 / 사용 안 함 |
| `POST /chatbot/models/pull` | `{modelId}` 로컬 모델 다운로드 시작 |
| `GET /chatbot/models/pulls` | 모델별 다운로드 진행 상태 |
| `POST /chatbot/models/custom` | `{name, label?}` 커스텀 모델 추가 (없는 모델이면 400) |
| `PATCH /chatbot/models/custom` | `{modelId, label}` 커스텀 모델 표시 이름 변경 |
| `POST /chatbot/models/custom/delete` | `{modelId}` 다운로드 취소·파일 삭제 후 커스텀 목록에서 제거 |
| `GET /chatbot/models/storage` | Ollama에 받아진 모델 전체와 용량 (`registered: false` = 파일만 남은 모델) |
| `POST /chatbot/models/delete` | `{modelId}` 로컬 모델 파일 삭제 (받는 중이면 409) |
| `GET /chatbot/models/catalog?provider=` | 회사별로 추가할 수 있는 모델 (설치된 LiteLLM 기준) · API 키 설정 여부 · 복원 가능한 기본 모델 |
| `POST /chatbot/models/api` | `{modelId}` API 모델 추가 |
| `POST /chatbot/models/api/delete` | `{modelId}` API 모델 삭제 (기본 제공 모델은 복원 가능, 기본 모델은 불가) |
| `POST /chatbot/models/api/restore` | `{provider}` 삭제한 기본 제공 모델 되살리기 |
| `GET /chatbot/runtime` | Ollama·LiteLLM 현재/최신/(LiteLLM) 최신 호환 버전, 릴리스 목록, 진행 중·마지막 작업 |
| `POST /chatbot/runtime/update` | `{ollama?, litellm?}` 버전 변경 시작 (둘 다 주면 한 번에) |
| `GET /chatbot/runtime/update` | 진행 단계·로그 |
| `POST /chatbot/runtime/litellm/check` | `{version}` LiteLLM 버전 호환성 확인 (별도 가상환경) |

### API 키 관리 (`/keys`)

사이드바 **API 키** 화면에서 OpenAI · Gemini · Anthropic 키, Hugging Face 다운로드 토큰(`HF_TOKEN`), 법제처 `LAW_OC`를
입력·변경·삭제하고 **연결 확인**을 한다 (`api/chatbot/keys.py`). 개인 PC용 서비스라 키는 이 PC의 루트 `.env`에 저장하고
(`api/env_store.py` — 해당 줄만 바꾸고 나머지는 그대로 둔다), 응답에는 원문 대신 앞뒤 4글자만 담는다.

| 키 | 저장 후 적용 |
| --- | --- |
| OpenAI · Gemini · Anthropic | 바로 (LiteLLM이 호출마다 환경 변수를 읽는다) |
| Hugging Face (`HF_TOKEN`) | 바로 — 모델 **다운로드** 인증(`huggingface_hub`, 예: 법령 별표 OCR 모델)에만 쓰고 호출에는 쓰지 않는다. 예전 이름(`HUGGINGFACEHUB_API_TOKEN` — huggingface_hub가 읽지 않는다)이면 저장할 때 `HF_TOKEN`으로 옮긴다 |
| `LAW_OC` | korean-law-mcp 컨테이너를 다시 만들고 '민법' 검색으로 인증 확인. docker-compose 필수 값이라 삭제는 막는다 |

연결 확인은 토큰을 쓰지 않는 요청만 보낸다 (모델 목록·계정 조회: OpenAI `/v1/models`, Gemini `models`, Anthropic `/v1/models`,
HF `whoami-v2`, LAW_OC는 법령 검색).

Ollama는 로컬 모델만 쓰므로 키가 없다. 대신 같은 화면에 **Ollama SSH 공개키**(`GET /chatbot/keys/ollama-ssh`)를 보여준다 —
Ollama의 `hf.co`/`huggingface.co` 다운로드는 `HF_TOKEN`이 아니라 이 키로 인증하므로, HF 계정 > SSH and GPG keys에 등록해야
승인·비공개 저장소를 받을 수 있다.

| API | 용도 |
| --- | --- |
| `GET /chatbot/keys` | 키 목록 (설정 여부·가린 값·적용 방식, LAW_OC 적용 진행 상태) |
| `PUT /chatbot/keys` | `{id, value}` 저장·변경 |
| `POST /chatbot/keys/delete` | `{id}` 삭제 (LAW_OC 불가) |
| `POST /chatbot/keys/test` | `{id}` 연결 확인 |

초기 데이터는 `api/llm_gateway/seed_models.json`이다. 테이블이 비어 있으면 서버 시작 시 자동으로 채워지고,
이후 JSON을 고쳤다면 아래 스크립트로 반영한다.

```bash
uv run python scripts/sync_llm_models.py           # JSON의 모델 추가·갱신, 없는 모델은 유지
uv run python scripts/sync_llm_models.py --prune   # JSON에 없는 모델까지 삭제
```

### 로컬 모델 (Docker Ollama)

로컬 모델은 다른 서비스와 마찬가지로 Docker 컨테이너(`ollama`, 포트 11434)로 띄운다.
이미지는 `docker/ollama/Dockerfile` — Ollama + CUDA·Vulkan 백엔드 + Mesa 드라이버(ANV/RADV/Dozen)를
버전 고정으로 담은 단일 이미지다.

**Ollama 버전**은 루트 `.env`의 `OLLAMA_VERSION` / `OLLAMA_SHA256`(없으면 0.34.4)으로 정해지고, 이미지는
`klaw-ollama:<버전>` 태그로 버전마다 따로 남는다. 마이 페이지 **런타임 버전**의 Ollama 줄에서 버전을 고르면 서버가
GitHub 릴리스의 SHA-256을 확인 → 이미지 빌드 → 컨테이너 재시작 → 버전·GPU 인식 확인까지 하고, 성공하면 두 값을 `.env`에 적는다.
확인에 실패하면(응답 없음, 이전엔 잡히던 GPU를 못 잡음 등) 이전 버전 이미지로 자동으로 되돌린다. 받아 둔 모델은 볼륨에 있어 그대로 남는다.
최신 모델 중에는 새 Ollama가 필요한 것이 있다 (예: `lfm3-omni:2b`는 0.34.4에서 받을 수 없다).
백엔드가 docker CLI가 있는 호스트에서 실행되어야 한다 (개인 PC 사용 전제 — 별도 권한 확인 없음).

**LiteLLM 버전**도 같은 카드에서 바꾼다. LiteLLM은 백엔드가 직접 쓰는 패키지라 순서가 다르다:

1. 별도 가상환경에서 호환성 확인 (`scripts/check_litellm_compat.py` — 도구 호출 경로, LangChain 연동, 등록 모델 단가).
   지금 설치된 `mcp`·`langchain-litellm` 버전으로 묶어 확인하므로, 1.106+처럼 mcp 2.3이 필요한 버전은 여기서 걸러진다.
   결과는 `.runtime/litellm_compat.json`에 캐시하고, 마이 페이지를 열면 현재보다 새 버전 중 **최신 호환 버전**을 백그라운드로 찾는다.
2. 서버가 `scripts/apply_litellm_update.py`를 **새 콘솔 창**으로 띄우고 스스로 종료한다.
3. 도우미가 `pyproject.toml`·`uv.lock`을 백업 → `"litellm==<버전>"`으로 고정 → `uv lock`·`uv sync` → 설치 버전·호환성 확인.
   실패하면 백업을 되돌리고 `uv sync`. 결과를 `.runtime/runtime_job.json`에 적은 뒤 그 창에서 서버를 다시 띄운다.
4. 화면은 서버가 내려간 동안에도 계속 조회하다가 다시 연결되면 결과를 보여준다.

자동 재시작은 서버를 `uv run python main.py`로 띄웠을 때만 된다 (`api/runtime_control.py`).
Ollama와 LiteLLM을 함께 바꾸면 **LiteLLM 호환성 확인 → Ollama 변경 → LiteLLM 적용** 순서로 한 작업으로 처리한다
(LiteLLM이 안 맞으면 아무것도 바꾸지 않고, Ollama가 실패하면 LiteLLM은 건드리지 않는다). 둘 다 새 버전이 있으면
카드 위에 **한 번에 업데이트** 버튼이 나온다.

GPU 가속은 호스트마다 컨테이너에 넘길 장치가 달라서, **설정 스크립트가 호스트를 감지해
`docker-compose.override.yml`(docker compose가 자동 병합, git 제외)을 만든다.**

| 감지 결과 | 컨테이너 설정 | Ollama 백엔드 |
| --- | --- | --- |
| NVIDIA GPU | GPU 예약 (NVIDIA Container Toolkit) | CUDA |
| Windows Docker Desktop(WSL2) + Intel/AMD GPU | `/dev/dxg` + `/usr/lib/wsl` 마운트 | Vulkan on D3D12 (Mesa Dozen) |
| Linux + `/dev/dri` | `/dev/dri` 전달 | Vulkan (Mesa ANV/RADV) |
| GPU 없음 / macOS | 없음 | CPU |

내장 GPU는 Ollama가 기본으로 제외하므로 NVIDIA가 아닌 경로에서는 `OLLAMA_IGPU_ENABLE=1`을 함께 켠다.

```bash
uv run python scripts/setup_local_llm.py --apply --pull --verify
#  감지 → override 생성 → 컨테이너 빌드·재생성 → DB의 활성 로컬 모델 받기 → 가벼운 모델로 GPU 사용률 확인
```

서버 시작 시에도 로컬 모델 상태(컨테이너 응답·모델 누락·가속 장치)를 점검해 로그(`[local-llm]`)로 알린다.
받은 모델은 Docker 볼륨 `klaw-chatbot_ollama_models`에 저장된다. Windows 폴더 바인드 마운트는 쓰지 않는다 —
Docker Desktop에서 GPU 버퍼로 모델을 읽을 때 `read error: Bad address`로 로드가 실패하기 때문이다.

LM Studio / vLLM으로 띄울 경우 DB(또는 seed JSON)의 모델 ID를 `lmstudio/<모델명>`, `vllm/<모델명>`으로
등록하면 된다 (모델명은 각 서버의 `/v1/models` 응답 기준).

## korean-law-mcp (git 서브모듈)

`korean-law-mcp`는 git 서브모듈로 관리한다. 이 저장소에는 **검증한 특정 커밋**만 기록되므로,
원격 저장소가 업데이트돼도 명시적으로 올리기 전까지는 도구가 바뀌지 않는다.

```bash
# 처음 clone 했을 때 (기록된 커밋으로 받기)
git submodule update --init

# 새 버전으로 올리기 (pull 대신 사용)
git submodule update --remote korean-law-mcp        # 원격 main 최신 커밋으로 이동
docker compose up -d --build korean-law-mcp         # 새 코드로 컨테이너 재빌드
uv run python scripts/check_mcp_tools.py            # 도구 변경·호환성 점검 (문제 있으면 종료 코드 1)
#   → 문제 없으면 스냅샷 갱신 후 서브모듈 커밋과 함께 커밋
uv run python scripts/check_mcp_tools.py --update
git add korean-law-mcp scripts/mcp_tools_snapshot.json && git commit -m "korean-law-mcp vX.Y.Z"

# 되돌리기 (기록된 커밋으로 복귀)
git submodule update korean-law-mcp
docker compose up -d --build korean-law-mcp
```

`check_mcp_tools.py`는 도구 이름·인자·설명의 변경과, 앱이 의존하는 부분(`search_law`의 `query` 인자,
`[NOT_FOUND]`·`재시도 제안`·`이미지 기반 PDF` 응답 표식)이 남아 있는지를 확인한다.
응답 문구의 **의미** 변화(새 오류 라벨 등)는 스크립트가 잡지 못하므로 서브모듈의 `CHANGELOG.md`도 함께 확인한다.

## 법령 검색 안정성 장치

- **인증 preflight**: 서버 시작 시 canary 법령('민법')을 검색해 법제처 Open API(LAW_OC) 인증이
  실제로 동작하는지 확인한다. 실패해도 기동은 계속하되 로그에 크게 경고를 남긴다.
  대화 중 인증 실패 시그니처가 감지되면 재검색을 즉시 중단하고, 사용자에게
  "질문을 바꿔라"가 아니라 **시스템 문제**로 안내한다 (`law_auth.py`).
- **temperature 에스컬레이션**: 법령 재검색 라운드마다 0.2 → 0.5 → 0.8 → 1.0(이후 반복)으로
  올려 같은 검색어를 반복 생성하는 고착을 깬다. **Gemini 3+ / GPT-5+ / Claude 5+에는 temperature를
  지정하지 않는다** (Gemini 3+는 deprecated + `<1.0` 무한루프·추론저하 경고, GPT-5+·Claude 5+는
  `temperature=1` 외 값을 거부 — DB `llm_models.fixed_temperature`).
- **별표 이미지 OCR**: 도구 응답에 "이미지 기반 PDF" 안내나 `<img>` 조각이 섞여 있으면
  파일을 내려받아 PaddleOCR(한국어)로 텍스트를 추출해 응답 뒤에 덧붙인다 (`tool_annex_ocr.py`).
  실행 디바이스는 런타임에 동적 결정 — CUDA 지원 paddlepaddle + GPU가 보이면 GPU, 아니면 CPU.
  GPU 서버에서는 `paddlepaddle-gpu`만 별도 설치하면 코드 수정 없이 GPU로 동작한다.

Postgres 연결에 실패하면 서버는 뜨지만 `/auth/*` 와 `/chatbot/*` 는 503을 반환한다 (서버 로그의 `[Postgres]` 항목 확인).

# 저장소 완전 초기화

회원 체계가 바뀌었거나 데이터를 통째로 비우고 싶을 때 사용한다.

```bash
uv run python scripts/reset_db.py         # 확인 프롬프트 후 실행
uv run python scripts/reset_db.py --yes   # 확인 없이 실행
```

- Postgres: `users` / `chat_sessions` / `chat_messages` DROP 후 재생성
- Qdrant: `conversation_history` 컬렉션 삭제 (서버 startup에서 자동 재생성)
- LangGraph 상태(MemorySaver)는 서버를 재시작하면 함께 비워진다