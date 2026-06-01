# Geo Analyzer 실행 가이드

이 문서는 현재 구현을 기준으로, 제3자가 처음 받아도 그대로 실행해 동일한 파이프라인을 재현할 수 있도록 작성되었습니다.

## 1. 파이프라인 개요

`main.py`는 아래 순서로 동작합니다.

1. 웹페이지를 Playwright로 렌더링해 HTML 확보
2. HTML을 Linode Object Storage(S3 호환)에 `public-read`로 업로드
3. 업로드된 공개 URL을 URL encoding하여 선처리기 호출
4. 선처리기 응답 HTML 로컬 저장
5. 선처리기 응답 HTML을 LLM에 전달해 GEO 최적화 결과 생성
6. LLM 전체 응답은 버리고, 순수 GEO 결과 JSON만 추출/저장

## 2. 파일 구조

- `main.py`: 전체 실행 코드
- `config.properties`: 자주 변경되는 설정(스토리지/선처리기/LLM/프롬프트 파일 경로)
- `config.properties.example`: 민감정보 제외된 설정 템플릿
- `geo_prompt_template.txt`: LLM 프롬프트 텍스트
- `geo_prompt_template_v1_original.txt`: 원본 프롬프트 템플릿(보존용)
- `geo_prompt_template_v1_enhanced.txt`: 현재 기본 프롬프트 템플릿
- `geo_prompt_template_v4_enhanced.txt`: RAG 최적화/지식 그래프 확장 프롬프트 템플릿 (권장)
- `call_llm_result5.sh`: LLM 호출 규격 참고 스크립트
- `purge_logs.sh`: 로그 파일 정리 스크립트
- `README.md`: 본 문서

## 3. 사전 준비

### 3.1 요구 사항

- macOS/Linux
- Python 3.10+

### 3.2 가상환경

```bash
cd /Users/hyoon/Projects/geo-analyzer
python3 -m venv .venv
source .venv/bin/activate
```

### 3.3 의존성 설치

```bash
pip install boto3 playwright
playwright install chromium
```

## 4. 외부 연동 설정

### 4.1 Object Storage

- Bucket: `hyoon-geo-bucket`
- Endpoint: `https://jp-osa-1.linodeobjects.com`
- Region: `jp-osa-1`
- Prefix: `rendered-html/`

업로드된 HTML의 공개 URL 형태:

`https://hyoon-geo-bucket.jp-osa-1.linodeobjects.com/rendered-html/<file>.html`

### 4.2 선처리기

- Base URL: `https://449d938d-94dc-4ac6-b3fd-e91b84774f95.fwf.app`
- Endpoint: `/geo/recommend?mode=aggressive&url=${encodedUrl}`

`${encodedUrl}`에는 Object Storage 공개 HTML URL을 `quote(..., safe='')` 규칙으로 인코딩한 값을 넣습니다.

### 4.3 LLM 호출

`call_llm_result5.sh` 규격을 코드로 이식했습니다.

- API: `http://104.64.202.250:8000/v1/chat/completions`
- Model: `gemma-4-fp4`
- 입력: 선처리기 응답 HTML(정리/축약 후 프롬프트 삽입)
- 출력: strict JSON 기대
- 초과 정책: `llm.input_overflow_policy=error|truncate` (기본 `error`)
  - `error`: `llm.max_preprocessor_chars` 초과 시 절단하지 않고 에러 처리
  - `truncate`: 초과 시 `llm.max_preprocessor_chars`까지 절단 후 호출

### 4.4 Playwright 타임아웃

- `playwright.goto_timeout_ms=60000`
- `page.goto(..., wait_until='networkidle')`에 적용되는 타임아웃(ms)
- 타임아웃 시 파이프라인은 중단하지 않고 현재 DOM으로 fallback 처리

### 4.5 설정파일 필수 항목

`config.properties`에는 아래 항목이 반드시 포함되어야 합니다.

1. object_storage 정보
2. llm 정보
3. prompt 정보(프롬프트 파일 경로)
4. preprocessor 정보
5. output 정보(파일 저장 여부/경로)

### 4.6 파일 저장 설정

- `output.save_enabled=true` 이면 로컬 파일 저장을 수행합니다.
- `output.base_dir`에 저장 루트 경로를 지정합니다.
- 기본 경로 권장값: `/Users/hyoon/Projects/geo-log`
- 렌더링 HTML: `output.rendered_html_subdir=rendered_html`
- 선처리기 결과: `output.preprocessor_subdir=pre_response`
- LLM 프롬프트: `output.llm_prompt_subdir=llm_prompt`
- LLM 결과: `output.llm_subdir=llm_response`
- LLM 원본 응답 디버그: `output.llm_raw_response_subdir=llm_raw_response` (옵션, 미지정 시 기본값 사용)
- JSON repair 입출력 디버그: `output.llm_repair_subdir=llm_repair` (옵션, 미지정 시 기본값 사용)
- Semantic 분석 토글: `semantic.analysis_enabled=true|false` (기본 false)
  - false일 때: 프롬프트의 출력 JSON 스키마에서 `semantic_analysis` 블록만 제거하고, 최종 GEO JSON output에서도 `semantic_analysis` 필드를 제외
- RAG TL;DR 가시 주입 토글: `rag.injection_enabled=true|false` (기본 false)
- RAG TL;DR 주입 위치: `rag.injection_target=main|article|body` (기본 main)
- RAG TL;DR 최대 길이: `rag.tldr_max_chars=700`

## 5. 핵심 구현 포인트

### 5.1 실패 복구

- 렌더링: timeout 발생 시 현재 DOM fallback
- 네트워크/런타임 오류: 재시도(`MAX_RETRIES`) 적용

### 5.2 LLM 컨텍스트 초과 방지

- `<script>`, `<style>`, `application/ld+json` 제거
- 공백 압축 및 길이 제한
- `llm.input_overflow_policy=error`면 초과 입력은 절단 없이 에러 처리
- `llm.input_overflow_policy=truncate`면 초과 입력을 절단하여 호출
- `LLM_RETRY_MAX_TOKENS`로 출력 토큰 예산 기반 재시도
- 재시도 시 입력 HTML 크기를 단계별로 줄이지는 않으며, 필요 시 최초 1회만 `llm.max_preprocessor_chars` 기준으로 절단합니다.
- `finish_reason=length` 응답은 성공으로 간주하지 않고, 다음 `max_tokens` 예산으로 재시도합니다.
- 따라서 `llm.retry_max_tokens`는 작은 값에서 큰 값으로 증가하는 순서로 설정하는 것을 권장합니다.

### 5.3 구조 개선 & 메타 태그 주입

- **Structural Audit**: LLM이 제안한 HTML 구조 개선사항 자동 적용
  - selector 기준: 현재 주입 엔진인 BeautifulSoup `select` 호환 CSS selector
  - 기본 action 스키마: `change_tag`, `update_text`
  - `change_tag` 사용 시 `target_tag` 필수
  - 허용 `target_tag`: `h1`, `h2`, `h3`, `h4`, `h5`, `h6`, `article`, `section`, `main`, `nav`, `aside`, `header`, `footer`, `p`, `ul`, `ol`, `li`
  - 하위 호환: legacy action(`change_tag_to_h1`, `change_tag_to_h2`, `change_tag_to_h3`, `change_tag_to_article`, `change_tag_to_section`)도 계속 수용
  - 안전 장치: selector 유일성 검증(0개/2개 이상 매칭 시 skip)
  - 결과: 구조 변경 내역과 사유 상세 기록

- **Meta Tag Injection**: title, description, OG 태그 업데이트
  - OG 태그는 LLM 출력의 `enriched_meta.og_tags` 구조 지원
  - Fallback: 평탄 키(`og:title` 등)도 인식
  - Upsert 방식: 기존 태그 수정, 없으면 신규 생성

- **JSON-LD**: Schema.org 메타데이터 추가/업데이트
  - 같은 `@type` 기존 JSON-LD 제거 후 신규 추가

- **RAG Optimization (Visible TL;DR)**: `rag_optimization.tldr_passage`를 본문 가시 영역에 주입
  - 설정으로 on/off 가능 (`rag.injection_enabled`)
  - 주입 위치 선택 가능 (`rag.injection_target`)
  - 길이 상한 적용 (`rag.tldr_max_chars`)
  - 기존 블록이 있으면 업데이트, 동일 텍스트면 no-op 처리

### 5.4 순수 GEO JSON만 저장

- 저장 대상: `choices[0].message.content`에서 추출한 JSON 객체
- 저장 제외: chat completion envelope 전체(`id`, `usage`, `choices` 등)
- 보강 파싱:
1. 코드블록/본문에서 JSON 블록 추출
2. 후행 쉼표 등 경미 오류 보정 후 재파싱
3. 여전히 실패하면 보조 LLM 호출로 strict JSON 복구 시도

### 5.5 주입 결과 리포트 자동 저장

- 저장 경로: `output.base_dir/injection_report/`
- 파일명: `injection_report_<domain>_<hash>_<timestamp>.json`
- 포함 내용:
  - structural_audit: 각 항목별 적용/스킵/실패 상태와 사유
  - enriched_meta: 메타 태그 변경 이력
  - json_ld: JSON-LD 반영 여부
  - rag_optimization: TL;DR 가시 주입 반영 여부
- 용도: 자동화된 품질 검증 및 디버깅

### 5.6 LLM 파싱 실패 디버그 산출물

- 파싱 실패 시 원본 LLM 응답을 저장합니다.
  - 경로: `output.base_dir/llm_raw_response/`
  - 파일: `llm_raw_response_<trace_id>.json`
- `finish_reason=length`로 잘린 응답도 동일 경로에 저장되어 재시도 전 상태를 확인할 수 있습니다.
- JSON repair 사용 시 입력/출력을 저장합니다.
  - 경로: `output.base_dir/llm_repair/`
  - 파일: `llm_repair_input_*_<trace_id>.txt`, `llm_repair_output_*_<trace_id>.txt`, `llm_repair_request_payload_<trace_id>.json`, `llm_repair_response_json_<trace_id>.json`
- 목적: 콘솔 로그만으로 재현이 어려운 malformed 응답을 사후 분석하기 위함

## 6. 실행 방법

`main.py`는 3가지 실행 모드를 지원합니다.

### 6.1 전체 파이프라인 모드 (기본)

URL 렌더링부터 주입까지 전체 단계를 실행합니다.

```bash
cd /Users/hyoon/Projects/geo-analyzer
source .venv/bin/activate
python main.py https://www.lg.com/us/
```

### 6.2 Rendered HTML 이후 재개 모드

이미 저장된 rendered HTML 파일을 입력받아, 아래 단계만 실행합니다.

- Object Storage 업로드
- Preprocessor 호출
- LLM 호출/파싱
- Enriched HTML 주입/저장

```bash
python main.py \
  --rendered-html-file rendered_www_lg_com_e3ddaf47_20260511_155735.html
```

참고:
- 파일명을 상대경로로 넣으면 `output.rendered_html_subdir` 기준으로 찾습니다.
- 필요 시 절대경로도 사용할 수 있습니다.
- `--source-url`을 주지 않으면 파일명에서 도메인을 자동 유추합니다.

### 6.3 Injection-Only 모드 (Preprocessed + LLM Response)

`preprocessed` HTML과 `llm response` JSON 파일만으로 주입 절차를 실행합니다.

- Structural Audit 적용
- Meta/OG 태그 반영
- JSON-LD 반영
- Injection Report 저장
- Enriched HTML 저장

```bash
python main.py \
  --preprocessed-file preprocessor_response_www_lg_com_e3ddaf47_20260511_155744.html \
  --llm-response-file llm_result_www_lg_com_e3ddaf47_20260511_155807.json \
  --source-url https://www.lg.com/us/
```

참고:
- `--llm-response-file`은 아래 두 형식 모두 지원합니다.
  - 순수 GEO JSON (`structural_audit`, `enriched_meta`, `json_ld` 포함)
  - Chat Completions envelope (`choices[0].message.content` 포함)

### 6.4 인자 조합 규칙

- 전체 모드: `target_url`만 전달
- post-render 모드: `--rendered-html-file`만 전달
- injection-only 모드: `--preprocessed-file` + `--llm-response-file` 함께 전달
- 위 조합을 벗어나면 argparse 에러로 종료

## 7. 산출물 위치

로컬 디렉터리: `output.base_dir` (기본 `/Users/hyoon/Projects/geo-log`)

생성 파일:

- `rendered_html/rendered_<...>.html`: Headless browser가 렌더링한 원본 HTML
- `pre_response/preprocessor_response_<...>.html`: 선처리기 응답 HTML
- `llm_prompt/llm_prompt_<...>.txt`: LLM 최종 프롬프트(디버깅용)
- `llm_response/llm_result_<...>.json`: 순수 GEO 결과 JSON만 저장된 파일
- `injection_report/injection_report_<...>.json`: 주입 결과 리포트
- `enriched_html/enriched_<...>.html`: 최종 반영된 enriched HTML

## 8. 로그 정리 및 정책

### 8.1 자동 정리 스크립트

`purge_logs.sh`를 사용하여 불필요한 로그 파일을 안전하게 삭제할 수 있습니다.

#### 기본 사용법

```bash
# Dry-run: 삭제될 파일 목록만 확인
./purge_logs.sh --dry-run

# Dry-run + 필터: 7일 이상 된 파일만 확인
./purge_logs.sh --dry-run --days 7

# 모든 파일 삭제 (확인 프롬프트 있음)
./purge_logs.sh --all --confirm

# 특정 타입만 삭제 (선처리기 결과만)
./purge_logs.sh --type preprocessor --all --confirm

# LLM 프롬프트 파일 중 3일 이상 된 파일 삭제
./purge_logs.sh --type llm_prompt --days 3 --confirm
```

#### 옵션 설명

| 옵션 | 설명 |
|------|------|
| `--dry-run` | 삭제 시뮬레이션 (파일 삭제 안 함) |
| `--days N` | N일 이상 된 파일만 대상 |
| `--type {preprocessor\|llm_prompt\|llm_response}` | 특정 디렉터리만 대상 |
| `--all` | 조건 없이 삭제 (--dry-run이 아닌 경우 필수) |
| `--confirm` | 삭제 확인 프롬프트 스킵 |
| `--config PATH` | config.properties 경로 (기본값: `./config.properties`) |

#### 권장 정리 정책

- **일일 정리**: `./purge_logs.sh --days 30 --all --confirm` (30일 이상 파일 삭제)
- **주간 정리**: `./purge_logs.sh --days 7 --all --confirm` (7일 이상 파일 삭제)
- **월간 전체 정리**: `./purge_logs.sh --all --confirm` (모든 로그 삭제)

#### cron 예제

```bash
# 매일 자정에 30일 이상 된 로그 정리
0 0 * * * cd /Users/hyoon/Projects/geo-analyzer && \
  source .venv/bin/activate && \
  ./purge_logs.sh --days 30 --all --confirm >> /tmp/purge.log 2>&1
```

## 9. 동작 확인 체크리스트

1. Object Storage 업로드 성공 로그 확인
2. 선처리기 호출 성공 로그 확인
3. `GEO result JSON saved to ...` 로그 확인
4. `llm_result_<...>.json` 파일 열어 JSON object 형태 확인

## 10. 트러블슈팅

### 10.1 LLM 400 context length 오류

- 증상: `maximum context length is 8192 tokens`
- 대응: 이미 `LLM_RETRY_MAX_TOKENS` 재시도 로직 적용됨
- 추가 대응: `llm.retry_max_tokens` 값을 더 낮춤

### 10.2 LLM 응답이 JSON이 아닌 경우

- 증상: 파싱 실패 로그 출력
- 대응: 내장된 JSON 복구 단계가 자동 실행
- 그래도 실패하면 프롬프트를 더 엄격히 줄이거나 HTML 길이를 추가 축소

### 10.4 `--llm-response-file` 파싱 실패

- 증상: `Failed to read JSON file` 또는 `Expecting property name enclosed in double quotes`
- 원인: JSON 파일에 스마트 따옴표(`“ ”`) 또는 미이스케이프 쌍따옴표가 포함됨
- 대응:
  1. JSON validator(`jq . <file>`)로 문법 검증
  2. 스마트 따옴표를 ASCII 따옴표(`"`)로 치환
  3. 문자열 내부 `"`가 필요한 위치(예: `39\"`)는 이스케이프 처리

### 10.3 Object Storage 업로드 실패

확인 항목:

1. Access key/Secret key 유효성
2. Bucket/Endpoint/Region 값 일치
3. 네트워크 접근 가능 여부

## 11. 보안 권장사항

현재 코드는 빠른 검증을 위해 키가 코드에 포함되어 있습니다. 운영 전 아래 방식으로 변경을 권장합니다.

1. Access key/Secret key를 환경변수로 이동
2. 코드에서 `os.getenv`로 주입
3. 키가 포함된 커밋 기록 제거 및 키 재발급
