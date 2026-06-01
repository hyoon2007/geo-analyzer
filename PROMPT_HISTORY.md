# Prompt History & Changelog

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
llm.retry_budgets=[{"max_chars":3000,"max_tokens":200},...}]
```

### Using Optimized Prompt (v2)
```
prompt.file=geo_prompt_template_v2_optimized.txt
llm.max_preprocessor_chars=6000
llm.retry_budgets=[{"max_chars":6000,"max_tokens":200},...}]
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
