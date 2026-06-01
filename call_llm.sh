#!/usr/bin/env bash
set -euo pipefail

API_URL="http://llm.go2legend.com/v1/chat/completions"
MODEL="Qwen/Qwen2.5-72B-Instruct-AWQ"
TEMPERATURE="0"
MAX_TOKENS="10000"

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required but not installed." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEAN_HTML_CONTENT="$(cat "$SCRIPT_DIR/result-nojsonld.html")"

read -r -d '' GEO_PROMPT <<'EOF' || true
# Role: Senior GEO(Generative Engine Optimization) Specialist & Schema.org Architect

# Context:
당신은 입력된 클린 HTML 데이터를 분석하여 AI 검색 엔진(SearchGPT, Perplexity 등)에 최적화된 고품질 메타데이터와 지식 그래프를 생성하는 시스템입니다.
이 작업은 오프라인 배치 파이프라인에서 수행되므로, 필요하다면 텍스트의 구조와 내용을 적극적으로 보정하여 품질을 극대화해야 합니다.
반드시 사전에 정의된 규격과 허용된 값(Enum)만을 사용하여 JSON을 생성해야 합니다.

# Input Data:
{{CLEAN_HTML_CONTENT}}

# Core Tasks:

## Task 1: Structural Audit
- HTML의 계층 구조(h1~h3)와 시맨틱 요소의 결함을 분석하십시오.
- 보정이 필요한 경우, `selector`는 lol_html 파서가 이해할 수 있는 단순한 형태(태그명, 클래스, ID 조합)로 작성하십시오.
- CSS Selector는 문서 내에서 대상을 유일하게(Unique) 식별할 수 있도록 가장 구체적으로 작성하십시오. (예: section > p와 같은 광역 셀렉터 사용을 엄격히 금지하며, 필요시 :nth-of-type() 등의 가상 클래스를 활용하십시오).
- `action`은 반드시 다음 6가지 중 하나만 사용해야 합니다: 
  ["change_tag_to_h1", "change_tag_to_h2", "change_tag_to_h3", "change_tag_to_article", "change_tag_to_section", "update_text"]
- `action`이 "update_text"인 경우, 반드시 `new_text` 필드를 추가하여 대체할 텍스트를 제공하십시오.

## Task 2: Contextual Meta Synthesis
- 본문 핵심 엔티티 3개 이상을 포함하여 150자 내외의 최적화된 `title`과 `description`을 작성하십시오.

## Task 3: Entity & JSON-LD Extraction
- 본문에서 핵심 엔티티를 추출하여 가장 구체적인 Schema.org Type 하나를 선정하여 완전한 JSON-LD를 작성하십시오.
- 본문에 없는 정보(가격, 재고, 리뷰 등)는 절대 지어내지 마십시오.

# Output Format (Strict JSON):
오직 다음 구조의 JSON만 출력하십시오. Markdown 백틱(```)은 제외하십시오.

{
  "structural_audit": {
    "status": "passed" | "needs_improvement",
    "recommendations": [
      {
        "selector": "div.main-title",
        "action": "change_tag_to_h1",
        "new_text": "(update_text 액션일 경우에만 존재)",
        "reason": "..."
      }
    ]
  },
  "enriched_meta": {
    "title": "...",
    "description": "...",
    "og_tags": {
      "og:type": "...",
      "og:site_name": "..."
    }
  },
  "json_ld": {
    "@context": "[https://schema.org](https://schema.org)",
    "@type": "Product", 
    "name": "...",
    "description": "..."
  }
}

# Strict Constraint (Hallucination Control):
- "update_text"를 수행할 때, 절대 원본 HTML에 존재하지 않는 새로운 정보(기능, 수치, 가격 등)를 창조하지 마십시오.
- 텍스트 수정은 오직 원본에 분산된 정보를 요약, 병합, 또는 문맥에 맞게 다듬는(Refining) 수준으로만 제한됩니다.

EOF

FINAL_PROMPT="$(jq -rn --arg p "$GEO_PROMPT" --arg h "$CLEAN_HTML_CONTENT" '$p | gsub("\\{\\{CLEAN_HTML_CONTENT\\}\\}"; $h)')"
PROMPT_TXT_PATH="$SCRIPT_DIR/final_prompt_result5.txt"
printf '%s\n' "$FINAL_PROMPT" > "$PROMPT_TXT_PATH"
echo "Rendered prompt saved to: $PROMPT_TXT_PATH" >&2

curl "$API_URL" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg model "$MODEL" \
    --arg content "$FINAL_PROMPT" \
    --argjson temperature "$TEMPERATURE" \
    --argjson max_tokens "$MAX_TOKENS" \
    '{
      model: $model,
      messages: [
        { role: "user", content: $content }
      ],
      temperature: $temperature,
      max_tokens: $max_tokens
    }'
  )"
