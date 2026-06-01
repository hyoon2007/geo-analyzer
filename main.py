import asyncio
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from utils.html_injection import inject_llm_results_to_html, save_injection_report


def load_properties(config_path: Path) -> dict[str, str]:
    """
    key=value 형식의 properties 파일을 로드합니다.
    """
    properties: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        properties[key.strip()] = value.strip()
    return properties


def require_property(properties: dict[str, str], key: str) -> str:
    """
    필수 property 값을 조회합니다.
    """
    value = properties.get(key)
    if not value:
        raise ValueError(f'Missing required property: {key}')
    return value


def parse_bool(value: str) -> bool:
    """
    properties의 문자열 bool 값을 파싱합니다.
    """
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config.properties'
PROPERTIES = load_properties(CONFIG_PATH)

BUCKET_NAME = require_property(PROPERTIES, 'object_storage.bucket_name')
OBJECT_STORAGE_ENDPOINT = require_property(PROPERTIES, 'object_storage.endpoint')
OBJECT_STORAGE_PUBLIC_BASE_URL = require_property(
    PROPERTIES,
    'object_storage.public_base_url',
)
OBJECT_STORAGE_REGION = require_property(PROPERTIES, 'object_storage.region')
OBJECT_STORAGE_PREFIX = require_property(PROPERTIES, 'object_storage.prefix')
OBJECT_STORAGE_ACCESS_KEY = require_property(
    PROPERTIES,
    'object_storage.access_key',
)
OBJECT_STORAGE_SECRET_KEY = require_property(
    PROPERTIES,
    'object_storage.secret_key',
)

PREPROCESSOR_BASE_URL = require_property(PROPERTIES, 'preprocessor.base_url')
PREPROCESSOR_PATH = require_property(PROPERTIES, 'preprocessor.path')

LLM_API_URL = require_property(PROPERTIES, 'llm.api_url')
DEBUG_ENABLED = parse_bool(PROPERTIES.get('debug.enabled', 'true'))
PLAYWRIGHT_GOTO_TIMEOUT_MS = int(PROPERTIES.get('playwright.goto_timeout_ms', '60000'))

LLM_MODEL = require_property(PROPERTIES, 'llm.model')
LLM_TEMPERATURE = float(require_property(PROPERTIES, 'llm.temperature'))
LLM_MAX_PREPROCESSOR_CHARS = int(PROPERTIES.get('llm.max_preprocessor_chars', '30000'))
LLM_INPUT_OVERFLOW_POLICY = PROPERTIES.get('llm.input_overflow_policy', 'error').lower()
LLM_RETRY_MAX_TOKENS = json.loads(require_property(PROPERTIES, 'llm.retry_max_tokens'))
LLM_TIMEOUT_SECONDS = int(PROPERTIES.get('llm.timeout_seconds', '120'))

if LLM_INPUT_OVERFLOW_POLICY not in {'error', 'truncate'}:
    raise ValueError(
        "Invalid llm.input_overflow_policy. Use 'error' or 'truncate'."
    )

PROMPT_FILE_PATH = BASE_DIR / require_property(PROPERTIES, 'prompt.file')
GEO_PROMPT_TEMPLATE = PROMPT_FILE_PATH.read_text(encoding='utf-8')

SAVE_TO_FILE = parse_bool(require_property(PROPERTIES, 'output.save_enabled'))
OUTPUT_BASE_DIR = Path(require_property(PROPERTIES, 'output.base_dir'))
RENDERED_HTML_DIR = OUTPUT_BASE_DIR / PROPERTIES.get(
    'output.rendered_html_subdir',
    'rendered_html',
)
PREPROCESSOR_RESULT_DIR = OUTPUT_BASE_DIR / require_property(
    PROPERTIES,
    'output.preprocessor_subdir',
)
LLM_PROMPT_DIR = OUTPUT_BASE_DIR / require_property(
    PROPERTIES,
    'output.llm_prompt_subdir',
)
LLM_RESULT_DIR = OUTPUT_BASE_DIR / require_property(
    PROPERTIES,
    'output.llm_subdir',
)
LLM_RAW_RESPONSE_DIR = OUTPUT_BASE_DIR / PROPERTIES.get(
    'output.llm_raw_response_subdir',
    'llm_raw_response',
)
LLM_REPAIR_DIR = OUTPUT_BASE_DIR / PROPERTIES.get(
    'output.llm_repair_subdir',
    'llm_repair',
)
INJECTION_REPORT_DIR = OUTPUT_BASE_DIR / PROPERTIES.get(
    'output.injection_report_subdir',
    'injection_report',
)

if SAVE_TO_FILE:
    RENDERED_HTML_DIR.mkdir(parents=True, exist_ok=True)
    PREPROCESSOR_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    LLM_PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    LLM_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    LLM_RAW_RESPONSE_DIR.mkdir(parents=True, exist_ok=True)
    LLM_REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    INJECTION_REPORT_DIR.mkdir(parents=True, exist_ok=True)

s3_client = boto3.client(
    's3',
    endpoint_url=OBJECT_STORAGE_ENDPOINT,
    region_name=OBJECT_STORAGE_REGION,
    aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY,
    aws_secret_access_key=OBJECT_STORAGE_SECRET_KEY,
)

USER_AGENT = (
    'Akam-GEOAgent/1.0'
)
MAX_RETRIES = 3
RETRY_DELAY = 2  # 초

async def fetch_and_render_html(page, url: str, timeout_ms: int = 30000) -> str | None:
    """
    Playwright 페이지를 재사용하여 URL을 렌더링하고 networkidle 상태의 DOM을 추출합니다.
    
    Args:
        page: 재사용 가능한 Playwright 페이지 객체
        url: 렌더링할 URL
        timeout_ms: 타임아웃 (ms)
    
    Returns:
        HTML 문자열 또는 None
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until='networkidle', timeout=timeout_ms)
            html_content = await page.content()
            print(f"[Success] Rendered {url} (Attempt {attempt}/{MAX_RETRIES})")
            return html_content
        except PlaywrightTimeoutError:
            print(f"[Warning] Timeout fetching {url} (Attempt {attempt}/{MAX_RETRIES}). "
                  f"Fallback to current DOM.")
            try:
                return await page.content()
            except (OSError, RuntimeError) as e:
                print(f"[Error] Failed to get DOM after timeout: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)
                continue
        except (OSError, RuntimeError) as e:
            print(f"[Error] Failed to fetch {url} (Attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                return None
    
    return None

def generate_filename(url: str) -> str:
    """
    URL 기반으로 고유한 파일명을 생성합니다.
    
    Args:
        url: 대상 URL
    
    Returns:
        안전하고 고유한 파일명 (중복 방지)
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    # URL의 해시를 포함하여 중복 방지
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    parsed = urlparse(url)
    domain = parsed.netloc.replace('.', '_')
    
    return f"{domain}_{url_hash}_{timestamp}.html"


def generate_trace_id(url: str) -> str:
    """
    Generate a stable trace id per pipeline call for artifact correlation.
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    parsed = urlparse(url)
    domain = parsed.netloc.replace('.', '_') or 'unknown'
    return f"{domain}_{url_hash}_{timestamp}"


def save_debug_text(
    output_dir: Path,
    file_name: str,
    content: str,
) -> Path | None:
    """
    Save debug text artifact to local output directory.
    """
    if not SAVE_TO_FILE:
        return None

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / file_name
        file_path.write_text(content, encoding='utf-8')
        return file_path
    except OSError as e:
        print(f"[Warning] Failed to save debug artifact: {e}")
        return None


def save_debug_json(
    output_dir: Path,
    file_name: str,
    payload: dict,
) -> Path | None:
    """
    Save debug JSON artifact to local output directory.
    """
    if not SAVE_TO_FILE:
        return None

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / file_name
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return file_path
    except (OSError, TypeError) as e:
        print(f"[Warning] Failed to save debug JSON artifact: {e}")
        return None

def upload_to_object_storage(url: str, html_content: str) -> str | None:
    """
    추출된 HTML을 Linode Object Storage에 public-read로 업로드합니다.
    
    Args:
        url: 원본 URL
        html_content: HTML 콘텐츠
    
    Returns:
        공개 접근 가능한 URL 또는 None
    """
    try:
        filename = generate_filename(url)
        object_key = f"{OBJECT_STORAGE_PREFIX}/{filename}"

        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=object_key,
            Body=html_content.encode('utf-8'),
            ContentType='text/html; charset=utf-8',
            ACL='public-read',
        )
        public_url = f"{OBJECT_STORAGE_PUBLIC_BASE_URL}/{object_key}"
        print(f"[Success] Uploaded to {public_url}")
        return public_url
    except (BotoCoreError, ClientError) as e:
        print(f"[Error] Failed to upload HTML to object storage: {e}")
        return None


def generate_preprocessor_result_filename(source_url: str) -> str:
    """
    선처리기 응답 HTML용 파일명을 생성합니다.
    """
    filename = generate_filename(source_url)
    return f"preprocessor_response_{filename}"


def generate_rendered_html_filename(source_url: str) -> str:
    """
    Headless browser가 렌더링한 원본 HTML용 파일명을 생성합니다.
    """
    filename = generate_filename(source_url)
    return f"rendered_{filename}"


def build_preprocessor_url(public_html_url: str) -> str:
    """
    업로드된 공개 HTML URL을 선처리기 호출 URL로 변환합니다.
    """
    encoded_url = quote(public_html_url, safe='')
    return f"{PREPROCESSOR_BASE_URL}{PREPROCESSOR_PATH}{encoded_url}"


def fetch_preprocessor_html(public_html_url: str) -> str | None:
    """
    선처리기를 호출해 응답 HTML을 가져옵니다.
    """
    request_url = build_preprocessor_url(public_html_url)
    try:
        with urlopen(request_url, timeout=60) as response:
            charset = response.headers.get_content_charset() or 'utf-8'
            html_content = response.read().decode(charset, errors='replace')
            print(f"[Success] Preprocessor called: {request_url}")
            return html_content
    except OSError as e:
        print(f"[Error] Failed to call preprocessor: {e}")
        return None


def save_preprocessor_result(source_url: str, html_content: str) -> Path | None:
    """
    선처리기 응답 HTML을 로컬에 저장합니다.
    """
    if not SAVE_TO_FILE:
        print('[Info] File save is disabled. Skip preprocessor response save.')
        return None

    try:
        file_path = PREPROCESSOR_RESULT_DIR / generate_preprocessor_result_filename(
            source_url,
        )
        file_path.write_text(html_content, encoding='utf-8')
        print(f"[Success] Preprocessor response saved to {file_path}")
        return file_path
    except OSError as e:
        print(f"[Error] Failed to save preprocessor response locally: {e}")
        return None


def save_rendered_html(source_url: str, html_content: str) -> Path | None:
    """
    Headless browser가 렌더링한 원본 HTML을 로컬에 저장합니다.
    """
    if not SAVE_TO_FILE:
        print('[Info] File save is disabled. Skip rendered HTML save.')
        return None

    try:
        file_path = RENDERED_HTML_DIR / generate_rendered_html_filename(source_url)
        file_path.write_text(html_content, encoding='utf-8')
        print(f"[Success] Rendered HTML saved to {file_path}")
        return file_path
    except OSError as e:
        print(f"[Error] Failed to save rendered HTML locally: {e}")
        return None


def generate_llm_result_filename(source_url: str) -> str:
    """
    LLM 응답 JSON 파일명을 생성합니다.
    """
    filename = generate_filename(source_url).replace('.html', '.json')
    return f"llm_result_{filename}"


def generate_llm_prompt_filename(source_url: str) -> str:
    """
    LLM 최종 프롬프트 저장용 파일명을 생성합니다.
    """
    filename = generate_filename(source_url).replace('.html', '.txt')
    return f"llm_prompt_{filename}"


def generate_enriched_html_filename(source_url: str) -> str:
    """
    Enriched HTML 저장용 파일명을 생성합니다.
    """
    return f"enriched_{generate_filename(source_url)}"


def save_enriched_html(source_url: str, enriched_html: str) -> Path | None:
    """
    Enriched HTML을 로컬에 저장합니다.
    """
    if not SAVE_TO_FILE:
        return None

    try:
        enriched_html_dir = OUTPUT_BASE_DIR / 'enriched_html'
        enriched_html_dir.mkdir(parents=True, exist_ok=True)
        file_path = enriched_html_dir / generate_enriched_html_filename(source_url)
        file_path.write_text(enriched_html, encoding='utf-8')
        print(f"[Success] Enriched HTML saved to {file_path}")
        return file_path
    except OSError as e:
        print(f"[Error] Failed to save enriched HTML locally: {e}")
        return None





def build_llm_prompt(clean_html_content: str) -> str:
    """
    shell 스크립트와 동일한 규칙으로 최종 프롬프트를 생성합니다.
    """
    return GEO_PROMPT_TEMPLATE.replace('{{CLEAN_HTML_CONTENT}}', clean_html_content)


def save_llm_prompt_text(
    source_url: str,
    prompt_text: str,
    trace_id: str = '',
    attempt: int = 0,
) -> Path | None:
    """
    최종 LLM 프롬프트를 임시 로컬 파일로 저장합니다.
    """
    if not SAVE_TO_FILE:
        print('[Info] File save is disabled. Skip LLM prompt save.')
        return None

    try:
        if trace_id:
            file_path = LLM_PROMPT_DIR / f"llm_prompt_{trace_id}_a{attempt}.txt"
        else:
            file_path = LLM_PROMPT_DIR / generate_llm_prompt_filename(source_url)
        file_path.write_text(prompt_text, encoding='utf-8')
        print(f"[Success] LLM prompt saved to {file_path}")
        return file_path
    except OSError as e:
        print(f"[Error] Failed to save LLM prompt locally: {e}")
        return None


def save_llm_raw_response_artifact(
    source_url: str,
    llm_response: dict,
    reason: str,
    trace_id: str = '',
) -> Path | None:
    """
    Save raw LLM response for parse-failure diagnostics.
    """
    payload = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'source_url': source_url,
        'reason': reason,
        'llm_response': llm_response,
    }
    effective_trace = trace_id or generate_trace_id(source_url)
    file_name = f"llm_raw_response_{effective_trace}.json"
    return save_debug_json(LLM_RAW_RESPONSE_DIR, file_name, payload)


def save_llm_repair_text_artifact(
    source_url: str,
    label: str,
    content: str,
    trace_id: str = '',
) -> Path | None:
    """
    Save JSON-repair input/output text artifacts.
    """
    effective_trace = trace_id or generate_trace_id(source_url)
    file_name = f"llm_repair_{label}_{effective_trace}.txt"
    return save_debug_text(LLM_REPAIR_DIR, file_name, content)


def save_llm_repair_json_artifact(
    source_url: str,
    label: str,
    payload: dict[str, Any],
    trace_id: str = '',
) -> Path | None:
    """
    Save JSON-repair metadata artifacts.
    """
    effective_trace = trace_id or generate_trace_id(source_url)
    file_name = f"llm_repair_{label}_{effective_trace}.json"
    return save_debug_json(LLM_REPAIR_DIR, file_name, payload)


def get_first_choice(llm_response: dict[str, Any]) -> dict[str, Any] | None:
    """
    Return the first response choice when available.
    """
    choices = llm_response.get('choices')
    if not isinstance(choices, list) or not choices:
        return None
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None
    return first_choice


def call_llm_for_geo_optimization(source_url: str, clean_html_content: str) -> dict | None:
    """
    call_llm_result5.sh 규격으로 LLM API를 호출합니다.
    """
    if len(clean_html_content) > LLM_MAX_PREPROCESSOR_CHARS:
        if LLM_INPUT_OVERFLOW_POLICY == 'truncate':
            truncated_content = clean_html_content[:LLM_MAX_PREPROCESSOR_CHARS]
            print(
                f"[Info] Preprocessor content truncated from {len(clean_html_content)} to "
                f"{LLM_MAX_PREPROCESSOR_CHARS} chars."
            )
            clean_html_content = truncated_content
        else:
            print(
                f"[Error] Preprocessor content size ({len(clean_html_content)} chars) "
                f"exceeds llm.max_preprocessor_chars ({LLM_MAX_PREPROCESSOR_CHARS}). "
                "Set llm.input_overflow_policy=truncate to allow truncation."
            )
            return None
    
    trace_id = generate_trace_id(source_url)

    for idx, max_tokens in enumerate(LLM_RETRY_MAX_TOKENS, start=1):
        final_prompt = build_llm_prompt(clean_html_content)
        save_llm_prompt_text(source_url, final_prompt, trace_id=trace_id, attempt=idx)

        payload = {
            'model': LLM_MODEL,
            'messages': [
                {
                    'role': 'user',
                    'content': final_prompt,
                }
            ],
            'temperature': LLM_TEMPERATURE,
            'max_tokens': max_tokens,
        }
        request = Request(
            LLM_API_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )

        try:
            with urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
                body = response.read().decode('utf-8', errors='replace')
                response_json = json.loads(body)
                if isinstance(response_json, dict):
                    response_json['_trace'] = {
                        'trace_id': trace_id,
                        'attempt': idx,
                        'max_tokens': max_tokens,
                    }

                first_choice = get_first_choice(response_json) if isinstance(response_json, dict) else None
                finish_reason = first_choice.get('finish_reason') if first_choice else None
                if finish_reason == 'length':
                    save_llm_raw_response_artifact(
                        source_url,
                        response_json if isinstance(response_json, dict) else {'raw_body': body},
                        f'finish_reason_length_a{idx}',
                        trace_id=trace_id,
                    )
                    print(
                        f"[Warning] LLM response truncated by max_tokens "
                        f"(attempt={idx}, max_tokens={max_tokens}). Retrying."
                    )
                    continue

                print(
                    f"[Success] LLM called: {LLM_API_URL} "
                    f"(attempt={idx}, max_tokens={max_tokens})"
                )
                return response_json
        except HTTPError as e:
            error_body = e.read().decode('utf-8', errors='replace')
            save_llm_repair_text_artifact(
                source_url,
                f'http_error_a{idx}',
                error_body,
                trace_id=trace_id,
            )
            print(
                f"[Warning] LLM API HTTP {e.code} "
                f"(attempt={idx}): {error_body}"
            )
            continue
        except json.JSONDecodeError as e:
            save_llm_repair_text_artifact(
                source_url,
                f'invalid_json_a{idx}',
                body if 'body' in locals() else str(e),
                trace_id=trace_id,
            )
            print(
                f"[Warning] LLM call returned invalid JSON "
                f"(attempt={idx}): {e}"
            )
            continue
        except OSError as e:
            print(
                f"[Warning] LLM call failed "
                f"(attempt={idx}): {e}"
            )
            continue

    print('[Error] LLM call failed after all retry attempts.')
    return None


def extract_json_text(raw_text: str) -> str | None:
    """
    텍스트에서 JSON 본문만 추출합니다.
    """
    stripped = raw_text.strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        return stripped

    fenced_match = re.search(
        r'```(?:json)?\s*(\{.*\})\s*```',
        raw_text,
        flags=re.DOTALL,
    )
    if fenced_match:
        return fenced_match.group(1).strip()

    start = raw_text.find('{')
    end = raw_text.rfind('}')
    if start != -1 and end != -1 and start < end:
        return raw_text[start:end + 1].strip()

    return None


def try_parse_geo_json(json_text: str) -> dict | None:
    """
    JSON 텍스트를 파싱하고, 경미한 문법 오류를 보정해 재시도합니다.
    """
    try:
        parsed = json.loads(json_text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    sanitized = re.sub(r',\s*([}\]])', r'\1', json_text)
    try:
        parsed = json.loads(sanitized)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None

    return None


def call_llm_json_repair(
    raw_content: str,
    source_url: str = 'https://unknown.local/',
    trace_id: str = '',
) -> dict | None:
    """
    깨진 JSON 텍스트를 strict JSON 객체로 정규화합니다.
    """
    repair_prompt = (
        'Return only one strict JSON object. Do not include markdown or explanation. '\
        'Repair the following content into valid JSON while preserving original meaning:\n\n'
        f'{raw_content}'
    )
    repair_max_tokens = max([500, *LLM_RETRY_MAX_TOKENS])
    payload = {
        'model': LLM_MODEL,
        'messages': [
            {
                'role': 'user',
                'content': repair_prompt,
            }
        ],
        'temperature': 0,
        'max_tokens': repair_max_tokens,
    }
    request = Request(
        LLM_API_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    save_llm_repair_text_artifact(
        source_url,
        'input_raw_content',
        raw_content,
        trace_id=trace_id,
    )
    save_llm_repair_text_artifact(
        source_url,
        'input_prompt',
        repair_prompt,
        trace_id=trace_id,
    )
    save_llm_repair_json_artifact(
        source_url,
        'request_payload',
        payload,
        trace_id=trace_id,
    )

    try:
        with urlopen(request, timeout=120) as response:
            body = response.read().decode('utf-8', errors='replace')
            save_llm_repair_text_artifact(
                source_url,
                'output_raw',
                body,
                trace_id=trace_id,
            )
            response_json = json.loads(body)
            save_llm_repair_json_artifact(
                source_url,
                'response_json',
                response_json,
                trace_id=trace_id,
            )
            first_choice = get_first_choice(response_json)
            finish_reason = first_choice.get('finish_reason') if first_choice else None
            if finish_reason == 'length':
                save_llm_repair_text_artifact(
                    source_url,
                    'failure_reason',
                    'Repair response was truncated by max_tokens (finish_reason=length).',
                    trace_id=trace_id,
                )
                return None
            choices = response_json.get('choices')
            if not isinstance(choices, list) or not choices:
                save_llm_repair_text_artifact(
                    source_url,
                    'failure_reason',
                    'Repair response missing choices.',
                    trace_id=trace_id,
                )
                return None
            repaired_content = choices[0].get('message', {}).get('content', '')
            if not isinstance(repaired_content, str) or not repaired_content.strip():
                save_llm_repair_text_artifact(
                    source_url,
                    'failure_reason',
                    'Repair response has empty message content.',
                    trace_id=trace_id,
                )
                return None

            save_llm_repair_text_artifact(
                source_url,
                'output_content',
                repaired_content,
                trace_id=trace_id,
            )

            repaired_text = extract_json_text(repaired_content)
            if not repaired_text:
                save_llm_repair_text_artifact(
                    source_url,
                    'failure_reason',
                    'Unable to locate JSON block in repair output content.',
                    trace_id=trace_id,
                )
                return None

            repaired_obj = try_parse_geo_json(repaired_text)
            if repaired_obj:
                print('[Success] LLM JSON repair succeeded.')
                return repaired_obj
            save_llm_repair_text_artifact(
                source_url,
                'failure_reason',
                'Repair output still could not be parsed into a JSON object.',
                trace_id=trace_id,
            )
            return None
    except HTTPError as e:
        error_body = e.read().decode('utf-8', errors='replace')
        save_llm_repair_text_artifact(
            source_url,
            'failure_reason',
            f'Repair HTTPError {e.code}',
            trace_id=trace_id,
        )
        save_llm_repair_text_artifact(
            source_url,
            'failure_http_body',
            error_body,
            trace_id=trace_id,
        )
        return None
    except json.JSONDecodeError as e:
        save_llm_repair_text_artifact(
            source_url,
            'failure_reason',
            f'Repair response JSON decode error: {e}',
            trace_id=trace_id,
        )
        return None
    except OSError as e:
        save_llm_repair_text_artifact(
            source_url,
            'failure_reason',
            f'Repair OSError: {e}',
            trace_id=trace_id,
        )
        return None


def extract_geo_json_from_llm_response(
    llm_response: dict,
    source_url: str = 'https://unknown.local/',
) -> dict | None:
    """
    LLM 응답에서 순수 GEO 결과 JSON만 파싱합니다.
    """
    try:
        trace = llm_response.get('_trace', {})
        trace_id = trace.get('trace_id', '') if isinstance(trace, dict) else ''

        choices = llm_response.get('choices')
        if not isinstance(choices, list) or not choices:
            print('[Error] LLM response missing choices.')
            save_llm_raw_response_artifact(
                source_url,
                llm_response,
                'missing_choices',
                trace_id=trace_id,
            )
            return None

        message = choices[0].get('message', {})
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            print('[Error] LLM response has empty message content.')
            save_llm_raw_response_artifact(
                source_url,
                llm_response,
                'empty_message_content',
                trace_id=trace_id,
            )
            return None

        json_text = extract_json_text(content)
        if not json_text:
            print('[Error] Unable to locate JSON block in LLM content.')
            save_llm_raw_response_artifact(
                source_url,
                llm_response,
                'json_block_not_found',
                trace_id=trace_id,
            )
            return None

        parsed = try_parse_geo_json(json_text)
        if parsed:
            return parsed

        save_llm_raw_response_artifact(
            source_url,
            llm_response,
            'initial_parse_failed',
            trace_id=trace_id,
        )

        repaired = call_llm_json_repair(
            content,
            source_url=source_url,
            trace_id=trace_id,
        )
        if repaired:
            return repaired

        print('[Error] Parsed GEO result is not a valid JSON object.')
        save_llm_raw_response_artifact(
            source_url,
            llm_response,
            'repair_failed',
            trace_id=trace_id,
        )
        return None
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        print(f'[Error] Failed to parse GEO JSON from LLM response: {e}')
        return None


def save_llm_response_json(source_url: str, geo_result_json: dict) -> Path | None:
    """
    순수 GEO 결과 JSON을 임시 로컬 파일로 저장합니다.
    """
    if not SAVE_TO_FILE:
        print('[Info] File save is disabled. Skip GEO result JSON save.')
        return None

    try:
        file_path = LLM_RESULT_DIR / generate_llm_result_filename(source_url)
        file_path.write_text(
            json.dumps(geo_result_json, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        print(f"[Success] GEO result JSON saved to {file_path}")
        return file_path
    except OSError as e:
        print(f"[Error] Failed to save LLM response locally: {e}")
        return None


def load_text_file(file_path: Path) -> str | None:
    """
    텍스트 파일을 UTF-8로 로드합니다.
    """
    try:
        return file_path.read_text(encoding='utf-8')
    except OSError as e:
        print(f"[Error] Failed to read file: {file_path} ({e})")
        return None


def load_json_file(file_path: Path) -> dict | None:
    """
    JSON 파일을 로드합니다.
    """
    try:
        return json.loads(file_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[Error] Failed to read JSON file: {file_path} ({e})")
        return None


def infer_source_url_from_filename(file_path: Path) -> str:
    """
    파일명에서 도메인을 유추해 source URL을 생성합니다.
    예: rendered_www_samsung_com_xxxxxxxx_20260511_153810.html -> https://www.samsung.com/
    """
    stem = file_path.stem
    # Remove known prefixes.
    for prefix in ('rendered_', 'preprocessor_response_', 'llm_result_', 'enriched_'):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break

    match = re.match(r'(?P<domain>.+)_[0-9a-f]{8}_\d{8}_\d{6}$', stem)
    if not match:
        return 'https://unknown.local/'

    domain_part = match.group('domain').replace('_', '.')
    return f'https://{domain_part}/'


def resolve_input_file(input_value: str, default_dir: Path) -> Path:
    """
    파일 입력값을 절대경로로 변환합니다.
    - 절대경로면 그대로 사용
    - 상대경로면 default_dir 기준으로 해석
    """
    candidate = Path(input_value)
    if candidate.is_absolute():
        return candidate
    return (default_dir / candidate).resolve()


def normalize_geo_result_json(
    llm_json: dict,
    source_url: str = 'https://unknown.local/',
) -> dict | None:
    """
    LLM 응답 JSON을 순수 GEO JSON으로 정규화합니다.
    - 이미 GEO JSON이면 그대로 반환
    - chat completion envelope면 파싱해 반환
    """
    if not isinstance(llm_json, dict):
        return None

    if any(k in llm_json for k in ('structural_audit', 'enriched_meta', 'json_ld')):
        return llm_json

    if 'choices' in llm_json:
        return extract_geo_json_from_llm_response(llm_json, source_url=source_url)

    return None


def run_injection_steps(
    source_url: str,
    preprocessed_html: str,
    geo_result_json: dict,
) -> tuple[Path | None, Path | None]:
    """
    preprocessed HTML + GEO JSON으로 주입 및 결과 저장을 수행합니다.
    """
    print('[Step][Start] Inject GEO result into HTML')
    enriched_html, injection_report = inject_llm_results_to_html(
        source_url,
        preprocessed_html,
        geo_result_json,
        debug=DEBUG_ENABLED,
    )
    print('[Step][End] Inject GEO result into HTML: success')

    print('[Step][Start] Save injection report locally')
    domain_hash = hashlib.md5(source_url.encode()).hexdigest()[:8]
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    injection_report_path = save_injection_report(
        injection_report,
        INJECTION_REPORT_DIR,
        domain=f"{urlparse(source_url).netloc.replace('.', '_')}_{domain_hash}",
        timestamp=timestamp,
    )
    print(
        f"[Step][End] Save injection report locally: "
        f"{'saved' if injection_report_path else 'skipped_or_failed'}"
    )

    print('[Step][Start] Save enriched HTML locally')
    enriched_html_path = save_enriched_html(source_url, enriched_html)
    print(
        f"[Step][End] Save enriched HTML locally: "
        f"{'saved' if enriched_html_path else 'skipped_or_failed'}"
    )
    return enriched_html_path, injection_report_path


def run_pipeline_from_preprocessed_and_llm_files(
    preprocessed_file: Path,
    llm_response_file: Path,
    source_url: str | None = None,
) -> None:
    """
    preprocessed 파일 + llm_response 파일로 주입 단계만 실행합니다.
    """
    source_url = source_url or infer_source_url_from_filename(preprocessed_file)
    print(f"[Info] Start injection-only mode for {source_url}")

    preprocessed_html = load_text_file(preprocessed_file)
    if preprocessed_html is None:
        return

    llm_json = load_json_file(llm_response_file)
    if llm_json is None:
        return

    geo_result_json = normalize_geo_result_json(llm_json, source_url=source_url)
    if geo_result_json is None:
        print('[Error] Unable to normalize LLM response to GEO JSON.')
        return

    _, injection_report_path = run_injection_steps(
        source_url,
        preprocessed_html,
        geo_result_json,
    )

    print("\n" + "=" * 60)
    print('Injection-Only Complete:')
    print("=" * 60)
    print(f"✓ Source URL: {source_url}")
    print(f"  Preprocessed File: {preprocessed_file}")
    print(f"  LLM Response File: {llm_response_file}")
    print(f"  Injection Report Path: {injection_report_path}")
    print("=" * 60)


def run_pipeline_from_rendered_file(
    rendered_file: Path,
    source_url: str | None = None,
) -> None:
    """
    rendered HTML 파일을 입력받아 렌더 이후 단계를 실행합니다.
    (upload -> preprocessor -> llm -> parse -> save -> inject)
    """
    source_url = source_url or infer_source_url_from_filename(rendered_file)
    print(f"[Info] Start post-render mode for {source_url}")

    rendered_html = load_text_file(rendered_file)
    if rendered_html is None:
        return

    print('[Step][Start] Upload rendered HTML to object storage')
    public_url = upload_to_object_storage(source_url, rendered_html)
    print(
        f"[Step][End] Upload rendered HTML to object storage: "
        f"{'success' if public_url else 'failed'}"
    )
    if not public_url:
        return

    print('[Step][Start] Call preprocessor')
    preprocessed_html = fetch_preprocessor_html(public_url)
    print(
        f"[Step][End] Call preprocessor: "
        f"{'success' if preprocessed_html else 'failed'}"
    )
    if not preprocessed_html:
        return

    print('[Step][Start] Save preprocessor response locally')
    preprocessor_response_path = save_preprocessor_result(source_url, preprocessed_html)
    print(
        f"[Step][End] Save preprocessor response locally: "
        f"{'saved' if preprocessor_response_path else 'skipped_or_failed'}"
    )

    print('[Step][Start] Call LLM for GEO optimization')
    llm_response = call_llm_for_geo_optimization(source_url, preprocessed_html)
    print(
        f"[Step][End] Call LLM for GEO optimization: "
        f"{'success' if llm_response else 'failed'}"
    )
    if not llm_response:
        return

    print('[Step][Start] Parse GEO JSON from LLM response')
    geo_result_json = extract_geo_json_from_llm_response(
        llm_response,
        source_url=source_url,
    )
    print(
        f"[Step][End] Parse GEO JSON from LLM response: "
        f"{'success' if geo_result_json else 'failed'}"
    )
    if not geo_result_json:
        return

    print('[Step][Start] Save GEO result JSON locally')
    llm_result_path = save_llm_response_json(source_url, geo_result_json)
    print(
        f"[Step][End] Save GEO result JSON locally: "
        f"{'saved' if llm_result_path else 'skipped_or_failed'}"
    )

    enriched_html_path, injection_report_path = run_injection_steps(
        source_url,
        preprocessed_html,
        geo_result_json,
    )

    print("\n" + "=" * 60)
    print('Post-Render Complete:')
    print("=" * 60)
    print(f"✓ Source URL: {source_url}")
    print(f"  Rendered File: {rendered_file}")
    print(f"  Preprocessor Response Path: {preprocessor_response_path}")
    print(f"  LLM Result JSON Path: {llm_result_path}")
    print(f"  Injection Report Path: {injection_report_path}")
    print(f"  Enriched HTML Path: {enriched_html_path}")
    print("=" * 60)

async def process_url(url: str):
    """
    단일 URL을 처리합니다.
    
    Args:
        url: 대상 URL
    """
    print(f"[Info] Starting render for {url}")
    print(f"[Info] Uploading results to {BUCKET_NAME}")
    print(f"[Info] Playwright goto timeout: {PLAYWRIGHT_GOTO_TIMEOUT_MS}ms")
    print(f"[Info] File save enabled: {SAVE_TO_FILE}")
    if SAVE_TO_FILE:
        print(f"[Info] Saving rendered HTML to {RENDERED_HTML_DIR}")
        print(f"[Info] Saving preprocessor results to {PREPROCESSOR_RESULT_DIR}")
        print(f"[Info] Saving LLM prompts to {LLM_PROMPT_DIR}")
        print(f"[Info] Saving LLM results to {LLM_RESULT_DIR}")
    print(f"[Info] LLM API endpoint: {LLM_API_URL}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        
        try:
            print('[Step][Start] Render page with Playwright')
            html = await fetch_and_render_html(
                page,
                url,
                timeout_ms=PLAYWRIGHT_GOTO_TIMEOUT_MS,
            )
            print(f"[Step][End] Render page with Playwright: {'success' if html else 'failed'}")
            
            if html:
                print('[Step][Start] Save rendered HTML locally')
                rendered_html_path = save_rendered_html(url, html)
                print(
                    f"[Step][End] Save rendered HTML locally: "
                    f"{'saved' if rendered_html_path else 'skipped_or_failed'}"
                )

                print('[Step][Start] Upload rendered HTML to object storage')
                public_url = upload_to_object_storage(url, html)
                print(
                    f"[Step][End] Upload rendered HTML to object storage: "
                    f"{'success' if public_url else 'failed'}"
                )

                preprocessor_response_path = None
                llm_result_path = None
                enriched_html_path = None
                if public_url:
                    print('[Step][Start] Call preprocessor')
                    preprocessed_html = await asyncio.to_thread(
                        fetch_preprocessor_html,
                        public_url,
                    )
                    print(
                        f"[Step][End] Call preprocessor: "
                        f"{'success' if preprocessed_html else 'failed'}"
                    )

                    if preprocessed_html:
                        print('[Step][Start] Save preprocessor response locally')
                        preprocessor_response_path = save_preprocessor_result(
                            url,
                            preprocessed_html,
                        )
                        print(
                            f"[Step][End] Save preprocessor response locally: "
                            f"{'saved' if preprocessor_response_path else 'skipped_or_failed'}"
                        )

                        print('[Step][Start] Call LLM for GEO optimization')
                        llm_response = await asyncio.to_thread(
                            call_llm_for_geo_optimization,
                            url,
                            preprocessed_html,
                        )
                        print(
                            f"[Step][End] Call LLM for GEO optimization: "
                            f"{'success' if llm_response else 'failed'}"
                        )

                        if llm_response:
                            print('[Step][Start] Parse GEO JSON from LLM response')
                            geo_result_json = extract_geo_json_from_llm_response(
                                llm_response,
                                source_url=url,
                            )
                            print(
                                f"[Step][End] Parse GEO JSON from LLM response: "
                                f"{'success' if geo_result_json else 'failed'}"
                            )

                            if geo_result_json:
                                print('[Step][Start] Save GEO result JSON locally')
                                llm_result_path = save_llm_response_json(
                                    url,
                                    geo_result_json,
                                )
                                print(
                                    f"[Step][End] Save GEO result JSON locally: "
                                    f"{'saved' if llm_result_path else 'skipped_or_failed'}"
                                )

                                print('[Step][Start] Inject GEO result into HTML')
                                enriched_html, injection_report = inject_llm_results_to_html(
                                    url,
                                    preprocessed_html,
                                    geo_result_json,
                                    debug=DEBUG_ENABLED,
                                )
                                print('[Step][End] Inject GEO result into HTML: success')

                                print('[Step][Start] Save injection report locally')
                                domain_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                                injection_report_path = save_injection_report(
                                    injection_report,
                                    INJECTION_REPORT_DIR,
                                    domain=f"{urlparse(url).netloc.replace('.', '_')}_{domain_hash}",
                                    timestamp=timestamp,
                                )
                                print(
                                    f"[Step][End] Save injection report locally: "
                                    f"{'saved' if injection_report_path else 'skipped_or_failed'}"
                                )

                                print('[Step][Start] Save enriched HTML locally')
                                enriched_html_path = save_enriched_html(
                                    url,
                                    enriched_html,
                                )
                                print(
                                    f"[Step][End] Save enriched HTML locally: "
                                    f"{'saved' if enriched_html_path else 'skipped_or_failed'}"
                                )

                print("\n" + "="*60)
                print("Processing Complete:")
                print("="*60)
                print(f"✓ {url}")
                print(f"  Rendered HTML Path: {rendered_html_path}")
                print(f"  Preprocessor Response Path: {preprocessor_response_path}")
                print(f"  LLM Result JSON Path: {llm_result_path}")
                print(f"  Enriched HTML Path: {enriched_html_path}")
                print(f"  Size: {len(html):,} bytes")
                print("="*60)
            else:
                print("\n" + "="*60)
                print("Processing Failed:")
                print("="*60)
                print(f"✗ {url}")
                print("="*60)
        finally:
            await browser.close()

# 실행 진입점
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Run GEO analyzer pipeline with full or partial modes.',
    )
    parser.add_argument(
        'target_url',
        nargs='?',
        help='Target URL for full pipeline mode (e.g. https://www.samsung.com/uk/)',
    )
    parser.add_argument(
        '--rendered-html-file',
        help='Rendered HTML file name/path to resume from post-render steps.',
    )
    parser.add_argument(
        '--preprocessed-file',
        help='Preprocessed HTML file name/path for injection-only mode.',
    )
    parser.add_argument(
        '--llm-response-file',
        help='LLM response JSON file name/path for injection-only mode.',
    )
    parser.add_argument(
        '--source-url',
        help='Optional source URL override for file-based modes.',
    )
    args = parser.parse_args()

    # Mode 1: full pipeline by target URL
    if args.target_url and not args.rendered_html_file and not args.preprocessed_file and not args.llm_response_file:
        asyncio.run(process_url(args.target_url))
    # Mode 2: resume from rendered HTML file
    elif args.rendered_html_file and not args.preprocessed_file and not args.llm_response_file:
        rendered_path = resolve_input_file(args.rendered_html_file, RENDERED_HTML_DIR)
        run_pipeline_from_rendered_file(rendered_path, args.source_url)
    # Mode 3: injection-only from preprocessed + llm files
    elif args.preprocessed_file and args.llm_response_file and not args.rendered_html_file:
        preprocessed_path = resolve_input_file(args.preprocessed_file, PREPROCESSOR_RESULT_DIR)
        llm_path = resolve_input_file(args.llm_response_file, LLM_RESULT_DIR)
        run_pipeline_from_preprocessed_and_llm_files(
            preprocessed_path,
            llm_path,
            args.source_url,
        )
    else:
        parser.error(
            'Invalid arguments. Use one of: '\
            '(1) target_url, '\
            '(2) --rendered-html-file <file>, '\
            '(3) --preprocessed-file <file> --llm-response-file <file>'
        )
 
