# GEO Analyzer Deployment Guide

이 문서는 현재 개발된 GEO Analyzer를 서버에 배포하는 절차를 정리합니다.

## 1. 배포 대상 구성

현재 서버에 배포할 주요 모듈은 다음과 같습니다.

- `main.py`: Playwright 렌더링, Object Storage 업로드, Preprocessor, LLM, Injection, EdgeKV 저장 파이프라인
- `worker.py`: Redis 큐를 감시하는 상주형 워커
- `enqueue_bridge.py`: HTTP API로 Redis enqueue를 감싸는 bridge
- `prod_enqueue.py`: 운영/테스트용 CLI enqueue 도구
- `redis_queue.py`: Redis queue, lock, retry, result 저장 모듈
- `edgekv_store.py`: Akamai EdgeKV 저장 모듈
- `page_type_registry.py`: 고객/page type별 TTL rule resolver
- `customer_page_types.json`: 임시 고객/page type rule seed

## 2. 로컬 변경사항 커밋 및 푸시

서버 배포 전 로컬 변경사항을 먼저 커밋하고 원격 저장소에 푸시합니다.

```bash
git add README.md DEPLOYMENT.md config.properties.example main.py edgekv_store.py page_type_registry.py \
  customer_page_types.json enqueue_bridge.py prod_enqueue.py redis_queue.py worker.py \
  requirements.txt design/edgeKV-data-schema.json design/edgeKV-data-sample.json \
  design/http_enqueue_bridge_design.md samples/

git commit -m "Add Redis worker, enqueue bridge, and EdgeKV publishing"
git push
```

## 3. 서버에 코드 배포

신규 서버라면:

```bash
cd /opt
git clone https://github.com/hyoon2007/geo-analyzer.git
cd geo-analyzer
```

이미 clone 되어 있다면:

```bash
cd /opt/geo-analyzer
git pull
```

## 4. Python 환경 구성

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Ubuntu 계열 서버에서 Playwright OS dependency가 부족하면 다음 명령도 실행합니다.

```bash
playwright install-deps chromium
```

## 5. 서버 설정 파일 구성

서버에서는 예제 파일을 복사해 실제 설정 파일을 만듭니다.

```bash
cp config.properties.example config.properties
```

아래 항목은 반드시 서버 환경에 맞게 수정합니다.

```properties
object_storage.bucket_name=
object_storage.endpoint=
object_storage.public_base_url=
object_storage.region=
object_storage.prefix=rendered-html
object_storage.access_key=
object_storage.secret_key=

preprocessor.base_url=
preprocessor.path=/geo/recommend?mode=aggressive&url=

llm.api_url=
llm.model=

redis.host=
redis.port=6379
redis.password=
redis.db=0
redis.queue_name=Queue:GEO:Tasks
redis.failed_queue_name=Queue:GEO:Failed
redis.dead_letter_queue_name=Queue:GEO:DeadLetter
redis.lock_prefix=Lock:GEO:Analysis
redis.lock_ttl_seconds=3600

enqueue_bridge.host=0.0.0.0
enqueue_bridge.port=8080
enqueue_bridge.auth_token=<strong-token>

customer.id=default
customer.page_types_path=customer_page_types.json
```

## 6. EdgeKV 설정

EdgeKV 저장은 기본적으로 꺼둡니다.

```properties
edgekv.enabled=false
```

실제 EdgeKV 저장을 활성화하려면 다음 값을 설정합니다.

```properties
edgekv.enabled=true
edgekv.namespace=geo_opt_data
edgekv.group_id=url_metadata
edgekv.network=production
edgekv.edgerc_path=/home/geo/.edgerc
edgekv.edgerc_section=default
edgekv.timeout_seconds=30
edgekv.data_version=2.0
```

`.edgerc` 파일은 서버의 안전한 위치에 배치하고 권한을 제한합니다.

```bash
cp ~/.edgerc /home/geo/.edgerc
chmod 600 /home/geo/.edgerc
```

EdgeKV item key는 `host + path + ?query`를 base64url로 인코딩하고 padding을 제거한 뒤 `k` prefix를 붙입니다.

최종 저장 HTML 정책은 다음과 같습니다.

- LLM과 injection이 모두 켜져 있으면 enriched HTML 저장
- LLM 또는 injection이 꺼져 있으면 Playwright rendered HTML 저장
- preprocessed 파일에서 시작하는 파일 기반 모드처럼 rendered HTML이 없으면 입력 HTML 저장

## 7. 고객/page type TTL 관리

현재는 `customer_page_types.json`을 임시 rule seed로 사용합니다.

```json
{
  "default_customer_id": "default",
  "customers": {
    "default": {
      "default_ttl_seconds": 86400,
      "page_types": [
        {
          "name": "flight_status",
          "ttl_seconds": 300,
          "patterns": ["*/flight-status*"]
        }
      ]
    }
  }
}
```

향후 고객/page type 정보는 Akamai Functions KV에서 받아오도록 `page_type_registry.py`의 loader를 교체할 예정입니다.

## 8. 수동 동작 테스트

서버에서 먼저 CLI enqueue와 worker 단건 처리를 테스트합니다.

```bash
source .venv/bin/activate

python prod_enqueue.py 'https://www.samsung.com/uk/'
python worker.py --once
```

동일 URL을 연속 enqueue하면 Redis lock 때문에 두 번째 요청은 skip됩니다.

HTTP enqueue bridge를 테스트하려면 bridge를 실행합니다.

```bash
source .venv/bin/activate
python enqueue_bridge.py
```

다른 터미널에서 호출합니다.

```bash
curl -X POST http://127.0.0.1:8080/geo/offline/enqueue \
  -H 'Authorization: Bearer <strong-token>' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.samsung.com/uk/","priority":1}'
```

상태 확인:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz
```

## 9. systemd 운영 예시

운영에서는 `worker`와 `enqueue_bridge`를 별도 service로 실행하는 것을 권장합니다.

### 9.1 Worker service

`/etc/systemd/system/geo-worker.service`

```ini
[Unit]
Description=GEO Analyzer Redis Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=geo
WorkingDirectory=/opt/geo-analyzer
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/geo-analyzer/.venv/bin/python /opt/geo-analyzer/worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 9.2 Enqueue bridge service

`/etc/systemd/system/geo-enqueue-bridge.service`

```ini
[Unit]
Description=GEO Analyzer HTTP Enqueue Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=geo
WorkingDirectory=/opt/geo-analyzer
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/geo-analyzer/.venv/bin/python /opt/geo-analyzer/enqueue_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

서비스 등록:

```bash
sudo systemctl daemon-reload
sudo systemctl enable geo-worker
sudo systemctl enable geo-enqueue-bridge
sudo systemctl start geo-worker
sudo systemctl start geo-enqueue-bridge
```

로그 확인:

```bash
journalctl -u geo-worker -f
journalctl -u geo-enqueue-bridge -f
```

## 10. 보안 권장사항

- `config.properties`는 git에 커밋하지 않습니다.
- `.edgerc`는 `chmod 600`으로 제한합니다.
- `enqueue_bridge.auth_token`은 충분히 긴 임의 문자열을 사용합니다.
- 외부 HTTPS는 앱에서 직접 처리하기보다 Nginx, Ingress, API Gateway에서 TLS terminate 하는 것을 권장합니다.
- Redis는 외부 공개망에 직접 노출하지 않습니다.

## 11. Kubernetes 전환 메모

향후 Kubernetes 환경에서는 다음 단위로 분리하는 것을 권장합니다.

- `geo-worker`: Redis queue consumer Deployment
- `geo-enqueue-bridge`: HTTP enqueue API Deployment + Service
- `redis`: StatefulSet 또는 managed Redis
- `config.properties`: ConfigMap과 Secret으로 분리
- `.edgerc`, Object Storage key, Redis password, enqueue token: Secret

Kubernetes에서도 기본 처리 흐름은 동일합니다.

```text
HTTP caller
-> enqueue_bridge
-> Redis Queue:GEO:Tasks
-> worker
-> Playwright render
-> Object Storage upload
-> Preprocessor
-> LLM
-> Injection
-> EdgeKV publish
```
