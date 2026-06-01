# HTML 주입 프로그램 기술서 (Injection Patcher Spec)

이 문서는 오프라인 워커가 LLM의 JSON을 원본 HTML에 반영하여 최종적으로 생성된 고품질 HTML(Enriched HTML)을 EdgeKV에 저장하기 위한 동작을 정의합니다.

## 개요

### 목적

LLM이 분석한 GEO JSON 데이터를 원본 HTML에 병합(Merge)하여, 실시간 엣지 노드가 즉시 서비스할 수 있는 완전한 형태의 정적 HTML로 재구성합니다.

### 최종 목적지

생성된 최종 HTML 문자열은 Akamai EdgeKV의 geo_opt_data namespace에 기록됩니다.

## 모듈별 동작 정의

### A. Structural Patcher (구조 보정 모듈)

**트리거**
- `structural_audit.status == "needs_improvement"`

**동작**
- `recommendations` 배열을 순회하며 DOM 요소를 변환합니다.

**Action 매핑 규칙**

- `change_tag_to_*`: `element.set_tag_name("h1")` 등을 호출하여 태그 이름만 변경
- `update_text`: `new_text` 필드의 값을 읽어 `element.set_inner_content(&new_text, ContentType::Text)`를 호출하여 텍스트 퀄리티를 대폭 상향

### B. Meta Tag Injector (메타 태그 교체 모듈)

**동작 (Upsert)**

- `<title>` 태그를 만나면 `element.set_inner_content(enriched_meta.title)` 실행
- `<meta name="description">`을 만나면 `element.set_attribute("content", enriched_meta.description)` 실행
- `<meta property="og:...">도 위와 동일하게 처리

**특징**

오프라인 환경이므로, 전체 HTML을 메모리에 올려 파싱 속도에 구애받지 않고 `<head>` 블록을 완벽하게 재구성(Re-write)할 수 있습니다. 누락된 태그는 `</head>` 직전에 일괄 추가합니다.

### C. JSON-LD Manager (구조화 데이터 병합 모듈)

**동작 (Targeted Replace)**

- LLM이 생성한 `json_ld`의 `@type` 값을 추출
- HTML 파싱 중 발견되는 모든 `<script type="application/ld+json">` 태그를 분석
- 기존 JSON의 `@type`이 LLM이 생성한 `@type`과 동일하거나, 실시간 엔진이 생성했던 임시 Skeleton 형태라면 과감히 삭제(`element.remove()`)
- 다른 `@type`은 보존
- `</body>` 닫는 태그 직전에 최고 품질의 LLM JSON-LD를 삽입

## 예외 처리 (Fail-safe)

- **JSON 파싱 실패**: LLM 응답이 올바른 JSON이 아니거나 필수 필드가 누락된 경우, Injection을 즉시 중단하고 원본 HTML을 그대로 반환합니다. 실시간 서비스는 원본을 엣지에서 계속 서빙하므로 장애가 발생하지 않습니다.

- **Hallucination 감지 로직 (Tier 3 Validation)**: `update_text`로 생성된 텍스트나 `json_ld` 내의 숫자가 원본 HTML 텍스트 풀(Pool) 내에 존재하지 않는다면 보정을 거부하고 Drop 시킵니다.

- **Selector 미스매치**: `structural_audit`의 selector가 실제 DOM에서 찾아지지 않아도 시스템 오류를 발생시키지 않고 무시(Skip) 처리합니다.
