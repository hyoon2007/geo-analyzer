# HTTP Enqueue Bridge Design

## 1. 목적

호출 측에서 Redis에 TCP로 직접 접속할 수 없는 환경을 위해, Redis enqueue 기능을 HTTP API로 감싼 Enqueue Bridge를 제공한다.

Bridge는 HTTP 요청을 받아 기존 Redis 큐 시스템(`redis_queue.py`)의 `enqueue()`를 호출한다. Redis lock 획득, 중복 URL 방지, task payload 생성은 기존 큐 모듈의 책임으로 유지한다.

## 2. 구성

```text
Caller
  -> HTTP POST /geo/offline/enqueue
  -> Enqueue Bridge API
  -> Redis lock + LPUSH
  -> Queue:GEO:Tasks
  -> worker.py
  -> main.py full pipeline
```

권장 파일 구조:

```text
enqueue_bridge.py      # HTTP API 서버
redis_queue.py         # Redis queue/lock 공통 로직
worker.py              # Redis resident worker
main.py                # GEO 파이프라인
```

## 3. API Contract

### 3.1 Endpoint

```http
POST /geo/offline/enqueue
Authorization: Bearer <token>
Content-Type: application/json
```

### 3.2 Request Body

최소 요청:

```json
{
  "url": "https://www.samsung.com/uk/"
}
```

전체 요청:

```json
{
  "url": "https://www.samsung.com/uk/",
  "mode": "full",
  "priority": 1,
  "request_id": "optional-caller-generated-id",
  "metadata": {
    "source": "edge",
    "tenant": "default"
  }
}
```

필드:

| 필드 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `url` | yes | - | 분석 대상 URL. `http` 또는 `https`만 허용 |
| `mode` | no | `full` | worker 실행 모드. 현재 `full`만 지원 |
| `priority` | no | `0` | task 메타데이터. 현재 Redis list 순서에는 직접 반영하지 않음 |
| `request_id` | no | UUID 자동 생성 | 호출 측 추적 ID. 없으면 bridge/queue가 자동 생성 |
| `metadata` | no | `{}` | 추가 추적 정보. task payload에 `metadata`로 포함 |

### 3.3 Queue Payload

Bridge가 Redis에 넣는 payload:

```json
{
  "request_id": "...",
  "url": "https://www.samsung.com/uk/",
  "mode": "full",
  "priority": 1,
  "token": "...",
  "attempts": 0,
  "enqueued_at": "2026-06-21T00:00:00+00:00",
  "metadata": {
    "source": "edge",
    "tenant": "default"
  }
}
```

`token`은 Redis lock value이며, worker가 완료/실패 후 lock을 해제할 때 일치 검증에 사용한다.

## 4. Response

### 4.1 성공

```http
202 Accepted
```

```json
{
  "status": "enqueued",
  "queue": "Queue:GEO:Tasks",
  "request_id": "...",
  "url": "https://www.samsung.com/uk/",
  "lock_key": "Lock:GEO:Analysis:<sha256>",
  "task": {
    "request_id": "...",
    "url": "https://www.samsung.com/uk/",
    "mode": "full",
    "priority": 1,
    "token": "...",
    "attempts": 0,
    "enqueued_at": "..."
  }
}
```

### 4.2 중복 URL 또는 이미 처리 중

```http
409 Conflict
```

```json
{
  "status": "duplicate",
  "message": "lock already exists",
  "url": "https://www.samsung.com/uk/",
  "lock_key": "Lock:GEO:Analysis:<sha256>"
}
```

### 4.3 인증 실패

```http
401 Unauthorized
```

```json
{
  "status": "unauthorized"
}
```

### 4.4 요청 검증 실패

```http
400 Bad Request
```

```json
{
  "status": "invalid_request",
  "message": "url is required"
}
```

### 4.5 Redis 또는 서버 오류

```http
503 Service Unavailable
```

```json
{
  "status": "unavailable",
  "message": "failed to enqueue task"
}
```

## 5. 인증

1차 구현은 static bearer token 방식을 사용한다.

설정:

```properties
enqueue_bridge.auth_token=<secret-token>
```

환경변수 override:

```bash
export GEO_ENQUEUE_BRIDGE_AUTH_TOKEN=<secret-token>
```

검증 규칙:

```text
Authorization: Bearer <token>
```

토큰이 없거나 일치하지 않으면 `401 Unauthorized`를 반환한다.

향후 Kubernetes에서는 Secret으로 주입한다.

## 6. 설정

추가 설정:

```properties
enqueue_bridge.host=0.0.0.0
enqueue_bridge.port=8080
enqueue_bridge.auth_token=change-me
```

환경변수:

```bash
GEO_ENQUEUE_BRIDGE_HOST=0.0.0.0
GEO_ENQUEUE_BRIDGE_PORT=8080
GEO_ENQUEUE_BRIDGE_AUTH_TOKEN=change-me
```

Redis 설정은 기존 `redis_queue.py` 설정을 그대로 사용한다.

```properties
redis.host=...
redis.port=6379
redis.password=...
redis.queue_name=Queue:GEO:Tasks
redis.failed_queue_name=Queue:GEO:Failed
redis.dead_letter_queue_name=Queue:GEO:DeadLetter
redis.lock_prefix=Lock:GEO:Analysis
```

## 7. 구현 선택

HTTP 서버는 `aiohttp`를 사용한다.

선택 이유:

- 현재 코드가 async Redis client(`redis.asyncio`)를 사용한다.
- FastAPI/Pydantic보다 의존성이 작고 단순한 bridge API에 충분하다.
- shutdown 시 Redis client close를 async lifecycle에서 처리하기 쉽다.

추가 dependency:

```text
aiohttp
```

## 8. Kubernetes 고려사항

권장 배포 단위:

```text
Deployment: geo-enqueue-bridge
Service: geo-enqueue-bridge
Secret: GEO_ENQUEUE_BRIDGE_AUTH_TOKEN, GEO_REDIS_PASSWORD
ConfigMap: Redis host/port/queue names
```

Health endpoints:

```http
GET /healthz
GET /readyz
```

권장 의미:

- `/healthz`: 프로세스 살아 있음
- `/readyz`: Redis `PING` 성공 시 ready

리소스 특성:

- Bridge는 Playwright/LLM을 실행하지 않으므로 가볍다.
- worker와 bridge는 별도 Deployment로 분리한다.
- bridge replica는 여러 개 띄워도 Redis lock이 중복 enqueue를 방지한다.

## 9. 운영 플로우

1. Caller가 `POST /geo/offline/enqueue` 호출
2. Bridge가 Bearer token 검증
3. JSON body 검증
4. `RedisGeoQueue.enqueue()` 호출
5. URL lock 획득 성공 시 `Queue:GEO:Tasks`에 LPUSH
6. lock이 이미 있으면 `409 Conflict`
7. `worker.py`가 BRPOP으로 task 처리
8. worker 완료/실패 후 Lua unlock

## 10. 테스트 계획

### 10.1 Local

```bash
python enqueue_bridge.py
```

```bash
curl -X POST http://127.0.0.1:8080/geo/offline/enqueue \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.samsung.com/uk/","priority":1}'
```

### 10.2 Duplicate

동일 URL을 두 번 호출한다.

기대:

- 첫 번째: `202 Accepted`
- 두 번째: `409 Conflict`

### 10.3 Worker 연동

```bash
python worker.py --once
```

기대:

- worker가 `Queue:GEO:Tasks`에서 task dequeue
- pipeline 수행
- lock release

## 11. 다음 구현 작업

1. `requirements.txt`에 `aiohttp` 추가
2. `enqueue_bridge.py` 추가
3. `config.properties.example`에 bridge 설정 추가
4. README에 bridge 실행/curl 예시 추가
5. `py_compile` 및 `enqueue_bridge.py --help` 검증
6. 네트워크 승인 가능 환경에서 curl smoke test
