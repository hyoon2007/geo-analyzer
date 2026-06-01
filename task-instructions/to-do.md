# GEO 고도화 To-Do

## 배경
최근 프롬프트/주입기 정합성 개선으로 아래가 반영되었습니다.
- selector 기준을 실행기와 일치 (BeautifulSoup select)
- 구조 변경 action을 change_tag + target_tag 형태로 확장
- 기존 6개 legacy action 하위 호환 유지

아래 항목은 운영 품질과 확장성을 높이기 위한 후속 고도화 과제입니다.

## 우선순위 P0 (안전/정합성)
- [ ] target_tag 정책을 2계층으로 분리
  - semantic_landmark: article, section, main, nav, aside, header, footer
  - content_structure: h1~h6, p, ul, ol, li
- [ ] action-target_tag 매트릭스 문서화
  - 어떤 action에서 어떤 target_tag가 허용되는지 명시
- [ ] selector 프로파일 명시
  - 허용: 태그, class, id, 결합자, :nth-of-type
  - 제한/금지: 과도한 광역 selector 및 런타임 비호환 pseudo-class

## 우선순위 P1 (실행 품질)
- [ ] structural recommendation 사전 검증 필드 도입
  - expected_match_count (기본 1)
  - confidence (0.0~1.0)
  - fallback_selector (선택)
- [ ] update_text 안전 규칙 강화
  - 길이 상한
  - 숫자/가격/재고 등 신규 사실 생성 탐지 규칙
- [ ] report 스키마 확장
  - normalized_action, target_tag, validation_code, skip_reason_code 추가

## 우선순위 P2 (운영/확장)
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
