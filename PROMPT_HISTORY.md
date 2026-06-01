# Prompt History & Changelog

## Version 1.2: Length-Aware Retry and Repair Observability (2026-06-01)
- **Date**: 2026-06-01
- **Scope**: `main.py`, `README.md`, `config.properties.example`
- **Status**: Applied
- **Key Changes**:
  - `finish_reason=length` 응답을 성공으로 반환하지 않고 다음 `max_tokens` 예산으로 재시도하도록 변경.
  - JSON repair 입출력에 request/response 메타데이터와 실패 사유 로깅 추가.
  - UTC 디버그 timestamp를 timezone-aware 형식으로 수정.
  - 디버그 산출물 디렉터리(`llm_raw_response`, `llm_repair`)와 재시도 정책 문서화.
- **Why**:
  - 잘린 JSON 응답을 조기에 식별해 파싱 실패를 줄이기 위함.
  - repair 실패 원인을 사후 분석 가능하게 만들기 위함.
  - 현재 코드 기준 재시도 정책을 문서와 일치시키기 위함.

## Version 1.1: Runtime-Aligned Schema Update (2026-06-01)
- **Date**: 2026-06-01
- **Scope**: `geo_prompt_template_v1_original.txt`, `geo_prompt_template.txt`, `utils/html_injection.py`
- **Status**: Applied
- **Key Changes**:
  - Parser wording aligned to runtime selector engine (BeautifulSoup `select`), removing `lol_html` mismatch.
  - Structural action schema changed from fixed 6-action enum to extensible form:
    - `action`: `change_tag` | `update_text`
    - `target_tag`: required when `action=change_tag`
  - Added allow-list based `target_tag` validation in injector.
  - Kept backward compatibility for legacy actions (`change_tag_to_h1` 등).
  - Added defensive handling for non-object recommendation items.
- **Why**:
  - Reduce prompt-runtime mismatch errors.
  - Improve extensibility without breaking existing LLM outputs.
  - Keep structural injection behavior explicit and auditable.

## Version 1: Original (v1_original.txt)
- **Date**: 2026-05-07
- **Size**: 3,141 bytes
- **Description**: Original prompt with detailed Korean instructions, examples, and constraints
- **Token Estimate**: ~780 tokens
- **Status**: Current production baseline
- **Details**:
  - Verbose role definition
  - Detailed task explanations
  - Complete JSON output examples
  - Comprehensive hallucination control instructions
  - Good for high-quality outputs but token-intensive

## Version 2: Optimized (v2_optimized.txt)
- **Date**: 2026-05-07
- **Size**: 1,602 bytes (~49% reduction)
- **Description**: Condensed prompt focusing on core instructions
- **Token Estimate**: ~400 tokens (~49% reduction)
- **Status**: Testing
- **Details**:
  - Simplified role definition (English-friendly)
  - Concise task descriptions
  - JSON structure without examples
  - Maintains core quality requirements
  - **Benefit**: Allows 2x more HTML content (6000 vs 3000 chars)
  - **Tested**: Samsung.com (25594 chars) → 6000 chars processed successfully

## Configuration Mapping

### Using Original Prompt (v1)
```
prompt.file=geo_prompt_template_v1_original.txt
llm.max_preprocessor_chars=3000
llm.retry_max_tokens=[1000,1500,2500]
```

### Using Optimized Prompt (v2)
```
prompt.file=geo_prompt_template_v2_optimized.txt
llm.max_preprocessor_chars=6000
llm.retry_max_tokens=[1000,1500,2500]
```

## Testing Results

### Test Case: Samsung.com (25,594 chars preprocessed HTML)

**V1 (Original) Performance**:
- HTML truncation: 25594 → 3000 chars (88% loss)
- LLM input tokens: ~900
- Result: Generated 2 structural recommendations
- Status: ✅ Success

**V2 (Optimized) Performance**:
- HTML truncation: 25594 → 6000 chars (76% loss, 2x improvement)
- LLM input tokens: ~450
- Result: Generated 2 structural recommendations
- Status: ✅ Success

## Decision Criteria

| Criterion | V1 (Original) | V2 (Optimized) |
|-----------|---------------|----------------|
| Quality | Higher (more detailed) | High (sufficient) |
| Content Preservation | Lower (3000 chars) | Higher (6000 chars) |
| Token Efficiency | Poor | Excellent |
| Complexity | High | Low |
| Maintenance | Harder | Easier |

### Recommendation
- **For production**: Use V2 (optimized) - better token efficiency
- **For fine-tuning**: Use V1 (original) - more detailed instructions
- **For future**: Consider prompt engineering with even shorter syntax

## Future Versions
- **V3**: Consider abbreviated JSON structure (remove explanation fields)
- **V4**: Test with different LLM models (e.g., Llama 3.1 with larger context)
