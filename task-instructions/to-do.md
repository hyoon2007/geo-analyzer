# GEO 고도화 To-Do

## 배경
최근 프롬프트/주입기 정합성 개선으로 아래가 반영되었습니다.
- selector 기준을 실행기와 일치 (BeautifulSoup select)
- 구조 변경 action을 change_tag + target_tag 형태로 확장
- 기존 6개 legacy action 하위 호환 유지

아래 항목은 운영 품질과 확장성을 높이기 위한 후속 고도화 과제입니다.

## 우선순위 P0 (안전/정합성)
- [ ] 3-Tier 검증 및 Fail-safe 정책 구체화
  - Tier 1: JSON Schema + 날짜/TTL 논리 검증 + EdgeKV 최종 payload byte size 검증
  - Tier 2: structural_audit selector를 실제 적용 엔진과 동일한 BeautifulSoup `select()` 기준으로 검증
  - Tier 3: JSON-LD 핵심 fact를 원본 클린 HTML evidence에 grounding
  - regex 기반 의미 보정은 금지하고, parse 실패 시 repair prompt 1회 후 재검증
  - 실패 정책은 전체 폐기보다 field/entity/recommendation 단위 부분 폐기와 단계적 강등을 우선
- [ ] target_tag 정책을 2계층으로 분리
  - semantic_landmark: article, section, main, nav, aside, header, footer
  - content_structure: h1~h6, p, ul, ol, li
- [ ] action-target_tag 매트릭스 문서화
  - 어떤 action에서 어떤 target_tag가 허용되는지 명시
- [ ] selector 프로파일 명시
  - 허용: 태그, class, id, 결합자, :nth-of-type
  - 제한/금지: 과도한 광역 selector 및 런타임 비호환 pseudo-class

## 우선순위 P1 (실행 품질)
- [ ] EdgeKV 1MB 초과 HTML 저장 회피
  - EdgeKV payload 최종 JSON byte 크기 기준으로 plain/gzip+base64 저장 방식을 선택
  - 작은 HTML은 `html_encoding=identity`로 원문 저장
  - 큰 HTML은 gzip 압축 후 base64 인코딩해 `html_encoding=gzip+base64`로 저장
  - EdgeWorkers 응답 시 gzip 지원 요청에는 base64 decode 후 압축 body 그대로 전달
  - `Content-Encoding: gzip`, `Content-Type`, `Vary: Accept-Encoding` 처리 및 double compression 방지
  - gzip+base64 이후에도 상한 초과 시 Object Storage URL fallback 검토
- [ ] structural recommendation 사전 검증 필드 도입
  - expected_match_count (기본 1)
  - confidence (0.0~1.0)
  - fallback_selector (선택)
- [ ] structural_audit 적용 전/후 검증 실행
  - selector match count와 target_tag 허용 여부 확인
  - no-op 변경 탐지 및 skip 처리
  - 적용 후 HTML parse 가능 여부 확인
  - 실패한 recommendation은 전체 publish 실패가 아니라 해당 항목만 제외
- [ ] update_text 안전 규칙 강화
  - 길이 상한
  - 숫자/가격/재고 등 신규 사실 생성 탐지 규칙
- [ ] JSON-LD field-level grounding 도입
  - name, brand, price, availability, description 등 핵심 필드별 evidence text 또는 source selector 요구
  - 숫자/가격/날짜/공백/HTML entity는 normalization 후 비교
  - evidence가 없거나 불확실한 필드/entity는 제거 또는 confidence 하향
- [ ] Fail-safe 강등 정책을 validation code로 표준화
  - schema invalid: EdgeKV publish 금지
  - selector 일부 실패: 해당 recommendation만 skip
  - 핵심 fact 미검증: 해당 JSON-LD field/entity 제거
  - HTML size 초과: gzip+base64 또는 Object Storage fallback
- [ ] report 스키마 확장
  - normalized_action, target_tag, validation_code, skip_reason_code 추가

## 우선순위 P2 (운영/확장)
- [ ] 오프라인 워커 URL preflight check 도입
  - 작업을 dequeue한 후 Playwright를 구동하기 전에 가벼운 HTTP `HEAD` 요청을 먼저 송신
  - `HEAD`가 지원되지 않거나 판정이 애매하면 제한된 range/timeout의 `GET` fallback 검토
  - `200 OK` 등 정상 응답이면 Playwright 렌더링 및 LLM 분석 진행
  - `404`, `410`, 명확한 `5xx` 등 즉시 실패로 볼 수 있는 응답이면 태스크 drop, 실패 기록, lock 해제
  - preflight timeout, 403/405, bot challenge 의심 응답은 정책 결정 필요
- [ ] prompt schema versioning
  - structural_audit.schema_version 필드 도입
  - v1(legacy action) / v2(change_tag) 동시 지원 정책 명시
- [ ] 허용 target_tag 외부 설정화
  - 코드 상수에서 config 기반으로 전환 여부 검토
- [ ] 회귀 테스트 세트 구축
  - 실제 URL 샘플 + fixture HTML 기반 스모크 테스트 자동화
- [ ] semantic_analysis 지식베이스 적재 파이프라인 설계
  - `semantic_analysis.primary_topic`, `subtopics`, `extracted_entities`, `proven_facts`를 정규화해 저장
  - 문서 단위 versioning/idempotency 키 설계 및 upsert 정책 정의
  - 검색 인덱스/그래프 저장소 스키마(엔티티, 관계, 증거 문장) 매핑 정의

## 검토 필요 사항
- [ ] semantic-only 모드 도입 여부
  - strict 모드: landmark만 허용
  - relaxed 모드: heading/list/paragraph 허용
- [ ] 하위 호환 폐기 시점
  - legacy action(change_tag_to_*) deprecation timeline 합의

## 완료 기준
- [ ] 문서/코드/프롬프트가 동일 스키마를 참조
- [ ] 샘플 2개 이상에서 적용률/실패 사유 리포트 일치
- [ ] 운영자가 스키마 변경 시 코드를 수정하지 않고 정책만 변경 가능
