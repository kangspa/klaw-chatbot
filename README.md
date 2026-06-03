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
  "projectId": "test",
  "isotime": "2026-06-02T20:30:00+09:00",
  "message": "한번 일반적인 대화도 잘 받아쳐주는지 궁금한데, 오늘자 한국에서 발생한 뉴스에 대해서 하나 아무거나 알려줄 수 있어?",
  "model": "gemini/gemini-2.5-flash-lite"
}
```