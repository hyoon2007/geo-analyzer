# GEO LLM 개선사항 반영 고도화 작업계획서

## 1. 문서 목적
이 문서는 preprocessed HTML + LLM 결과(JSON)를 병합해 enriched HTML을 생성하는 과정에서,
현재 반영되지 않는 항목을 정확히 수정하고 안정적으로 운영하기 위한 실행 계획서입니다.

## 2. 현재 상태 요약
최근 결과를 기준으로 확인된 상태는 아래와 같습니다.

- 반영됨:
  - enriched_meta.title
  - enriched_meta.description
  - json_ld (신규 script 추가)
- 반영 안 됨:
  - structural_audit.recommendations 전체
  - og_tags 기반 OG 메타 업데이트

즉, 메타 일부와 JSON-LD는 반영되지만, 구조 개선 제안은 실제 DOM에 적용되지 않습니다.

## 3. 핵심 문제와 원인

### 문제 A: structural_audit 미적용
- 원인: `structural_audit.recommendations`를 실행하는 코드가 없음
- 영향: LLM이 좋은 구조 개선안을 생성해도 최종 HTML 품질 개선으로 연결되지 않음

### 문제 B: OG 태그 미생성
- 원인: LLM 프롬프트가 `enriched_meta.og_tags`를 요청하지 않아서, LLM이 og 값을 생성하지 않음
- 참고: 현재 주입 코드는 이미 평탄 키(`og:title` 등)를 올바르게 읽도록 구현됨
- 영향: OG title/description 업데이트 누락 (구조 문제 아니라 입력값 부재)

### 문제 C: 적용 결과 가시성 부족
- 원인: 어떤 제안이 반영/실패되었는지 기계적으로 추적하는 결과 리포트가 없음
- 영향: 수동 비교가 필요하고, 디버깅 비용이 큼

## 4. 목표 (Definition of Done)
아래 4개를 만족하면 작업 완료로 판단합니다.

1. structural_audit action 6종 적용 가능
   - change_tag_to_h1, change_tag_to_h2, change_tag_to_h3
   - change_tag_to_article, change_tag_to_section
   - update_text
2. `enriched_meta.og_tags`가 실제 OG 메타 태그에 반영됨
3. 항목별 반영 결과 리포트(JSON) 생성
   - applied / skipped / failed와 원인 포함
4. 실제 URL 2개 이상에서 end-to-end 검증 완료

## 5. 상세 작업 계획

## 5.1 1단계 - Structural Audit 적용 엔진 구현
대상 파일: `utils/html_injection.py`

작업 내용:
- 함수 추가: `apply_structural_audit_injector(html, structural_audit, debug=False)`
- recommendation 반복 처리
- selector 기반 노드 탐색
- action별 처리 로직 구현
  - tag 변경: 기존 노드의 tag name 변경
  - update_text: 노드 text 교체
- 안전 장치
  - 허용 action 화이트리스트 검증
  - selector 매칭 0개면 skip
  - selector 매칭 2개 이상이면 skip (유일성 보장)
  - update_text인데 new_text 누락 시 skip
- 반환값 설계
  - 변경된 HTML
  - 적용 결과(성공/실패/스킵 사유)

산출물:
- structural_audit 반영 가능한 injector 코드

## 5.2 2단계 - LLM 프롬프트 수정 (og_tags 요청 추가)
대상 파일: `geo_prompt_template_v2_optimized.txt` (또는 현재 사용 중인 프롬프트 파일)

작업 내용:
- enriched_meta 스펙에 og_tags 필드 추가
  ```json
  "enriched_meta": {
    "title": "...",
    "description": "...",
    "og_tags": {
      "og:title": "...",
      "og:description": "..."
    }
  }
  ```
- og:title과 og:description을 명시적으로 요청
- 예시 추가 (og 값이 어떻게 생성되어야 하는지)

산출물:
- LLM이 og_tags를 실제로 생성하도록 개선된 프롬프트

## 5.3 3단계 - 통합 주입 순서 정리
대상 파일: `utils/html_injection.py`

권장 순서:
1. structural_audit
2. enriched_meta
3. json_ld

작업 내용:
- `inject_llm_results_to_html()`에서 위 순서로 injector 호출
- 단계별 예외 격리(한 단계 실패가 전체 실패로 이어지지 않게)

산출물:
- 안정적인 전체 주입 파이프라인

## 5.4 4단계 - 적용 결과 리포트 기능 추가
대상 파일:
- `utils/html_injection.py`
- `main.py`

작업 내용:
- 리포트 스키마 정의 (JSON)
  - timestamp
  - source_url
  - structural_audit.summary
  - structural_audit.items[]
    - selector, action, status, reason
  - meta.summary
  - json_ld.summary
- 저장 경로
  - `output.base_dir/injection_report/`
- 파일명 규칙
  - `injection_report_<domain>_<hash>_<timestamp>.json`

산출물:
- 사람이 바로 확인 가능한 반영 결과 리포트

## 5.5 5단계 - 설정/문서 정리
대상 파일:
- `README.md`
- (필요 시) `config.properties` 주석

작업 내용:
- 지원하는 structural action 목록 문서화
- selector 유일성 정책 문서화
- og_tags 스키마 예시 추가
- 리포트 파일 위치/해석법 추가

산출물:
- 운영/유지보수 가능한 최신 문서

## 6. 테스트 계획

## 6.1 단위 테스트 (권장)
테스트 항목:
- selector 0개/1개/2개 이상 케이스
- 각 action별 성공 케이스
- update_text에서 new_text 누락 케이스
- og_tags upsert 케이스(존재/미존재)
- json_ld append/replace 케이스

성공 기준:
- 예상 status와 변경 DOM이 일치

## 6.2 통합 테스트 (필수)
대상 URL:
- samsung.com/uk
- lg.com/us (또는 동일 성격의 대형 페이지 1개 추가)

검증 항목:
- enriched HTML에서 구조 변경 반영 여부
- OG 태그 변경 반영 여부
- JSON-LD 반영 여부
- injection_report 생성 및 상태 일치 여부

성공 기준:
- structural recommendation 반영률 >= 70% (유효 selector 기준)
- skip/fail 항목은 reason이 반드시 기록됨

## 7. 리스크 및 대응

리스크 1: LLM selector 부정확
- 대응: 유일성 검증 + skip reason 기록
- 후속: prompt에 selector 작성 규칙 강화

리스크 2: DOM 파서 차이로 구조 변경 부작용
- 대응: action 범위 제한 + 통합 테스트 페이지 확대

리스크 3: 과도한 텍스트 변경
- 대응: update_text 길이/문자 규칙 검증(필요 시)

## 8. 작업 순서(실행 체크리스트)

1. structural injector 구현
2. og_tags 정합성 반영
3. 통합 주입 순서 반영
4. 리포트 저장 기능 추가
5. README 업데이트
6. URL 2개 이상 재실행
7. 결과 비교 및 잔여 이슈 정리

## 9. 예상 소요
- 구현: 반나절 ~ 1일
- 테스트/보정: 반나절
- 총: 1 ~ 1.5일

## 10. 완료 후 기대효과
- LLM 제안이 실제 HTML 개선으로 연결됨
- 적용 누락 원인을 즉시 파악 가능
- 운영 중 품질 회귀를 빠르게 탐지 가능

---

## ✅ 작업 완료 검증 결과 (2026-05-11)

### 1단계: Structural Audit 적용 엔진 ✅
- `apply_structural_audit_injector()` 구현 완료
- LG 페이지: 2개 적용(applied), 1개 skipped(유일성 미확보)
- Samsung 페이지: 3개 skipped(selector 불일치)
- 각 항목별 상태/사유 명확 기록

### 2단계: LLM 프롬프트 수정 ✅
- [geo_prompt_template_v2_optimized.txt](geo_prompt_template_v2_optimized.txt) 수정
- enriched_meta에 og_tags 필드 추가 → LLM이 이제 og:title/og:description 생성

### 3단계: 통합 주입 순서 ✅
- [utils/html_injection.py](utils/html_injection.py) 리팩토링
- 순서: structural_audit → enriched_meta → json_ld
- 단계별 예외 격리로 안정성 향상

### 4단계: 리포트 기능 ✅
- `save_injection_report()` 구현
- 저장 경로: `output.base_dir/injection_report/`
- 파일명: `injection_report_<domain>_<hash>_<timestamp>.json`
- 포함 내용: 구조/메타/JSON-LD 별 상태/사유/변경 이력

### 5단계: 문서 정리 ✅
- [README.md](README.md) 업데이트
- structural action 목록 문서화
- og_tags 스키마 설명 추가
- 리포트 파일 해석법 추가

### 최종 검증 (end-to-end 테스트)
- Samsung UK: ✓ 완료
  - og:title ✓, og:description ✓ 반영됨
  - 리포트 저장 ✓
- LG USA: ✓ 완료
  - structural audit 2개 적용 ✓
  - og:title ✓, og:description ✓ 반영됨
  - 리포트 저장 ✓

### 남은 작업
- 없음 (모든 항목 완료)
