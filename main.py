import asyncio
import argparse
import http.client
import hashlib
import json
import re
import socket
import time
import uuid
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


def parse_csv_property(value: str) -> list[str]:
    """
    Comma-separated properties 값을 리스트로 파싱합니다.
    """
    return [item.strip().lower() for item in value.split(',') if item.strip()]


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
PLAYWRIGHT_POST_LOAD_WAIT_MS = int(PROPERTIES.get('playwright.post_load_wait_ms', '0'))
BROWSER_REQUEST_PROFILE = PROPERTIES.get('browser.request_profile', 'default').lower()
BROWSER_DIAGNOSTICS_ENABLED = parse_bool(
    PROPERTIES.get('browser.diagnostics_enabled', 'false'),
)
BROWSER_BLOCK_THIRD_PARTY_DOMAINS = parse_bool(
    PROPERTIES.get('browser.block_third_party_domains', 'false'),
)
BROWSER_PRIMARY_DOMAIN_OVERRIDE = PROPERTIES.get(
    'browser.primary_domain_override',
    '',
).strip().lower()
BROWSER_ALLOWED_EXTRA_DOMAINS = parse_csv_property(
    PROPERTIES.get('browser.allowed_extra_domains', ''),
)
BROWSER_SERVICE_WORKERS = PROPERTIES.get('browser.service_workers', 'allow').lower()
BROWSER_SAVE_RENDER_DIAGNOSTICS = parse_bool(
    PROPERTIES.get('browser.save_render_diagnostics', 'false'),
)
BROWSER_CAPTURE_SHADOW_DOM = parse_bool(
    PROPERTIES.get('browser.capture_shadow_dom', 'false'),
)
BROWSER_CAPTURE_FRAMES_HTML = parse_bool(
    PROPERTIES.get('browser.capture_frames_html', 'false'),
)
BROWSER_AUTO_SCROLL_ENABLED = parse_bool(
    PROPERTIES.get('browser.auto_scroll_enabled', 'false'),
)
BROWSER_AUTO_SCROLL_STEP_PX = int(PROPERTIES.get('browser.auto_scroll_step_px', '800'))
BROWSER_AUTO_SCROLL_WAIT_MS = int(PROPERTIES.get('browser.auto_scroll_wait_ms', '300'))
BROWSER_AUTO_SCROLL_MAX_STEPS = int(PROPERTIES.get('browser.auto_scroll_max_steps', '20'))

LLM_MODEL = require_property(PROPERTIES, 'llm.model')
LLM_TEMPERATURE = float(require_property(PROPERTIES, 'llm.temperature'))
LLM_MAX_PREPROCESSOR_CHARS = int(PROPERTIES.get('llm.max_preprocessor_chars', '30000'))
LLM_INPUT_OVERFLOW_POLICY = PROPERTIES.get('llm.input_overflow_policy', 'error').lower()
LLM_RETRY_MAX_TOKENS = json.loads(require_property(PROPERTIES, 'llm.retry_max_tokens'))
LLM_TIMEOUT_SECONDS = int(PROPERTIES.get('llm.timeout_seconds', '120'))
LLM_CONNECT_TIMEOUT_SECONDS = float(
    PROPERTIES.get('llm.connect_timeout_seconds', '15'),
)
LLM_READ_TIMEOUT_SECONDS = float(
    PROPERTIES.get('llm.read_timeout_seconds', str(LLM_TIMEOUT_SECONDS)),
)
LLM_STREAM_ENABLED = parse_bool(PROPERTIES.get('llm.stream_enabled', 'false'))
SEMANTIC_ANALYSIS_ENABLED = parse_bool(PROPERTIES.get('semantic.analysis_enabled', 'false'))
RAG_INJECTION_ENABLED = parse_bool(PROPERTIES.get('rag.injection_enabled', 'false'))
RAG_INJECTION_TARGET = PROPERTIES.get('rag.injection_target', 'main')
RAG_TLDR_MAX_CHARS = int(PROPERTIES.get('rag.tldr_max_chars', '700'))

if LLM_INPUT_OVERFLOW_POLICY not in {'error', 'truncate'}:
    raise ValueError(
        "Invalid llm.input_overflow_policy. Use 'error' or 'truncate'."
    )

if BROWSER_REQUEST_PROFILE not in {'default', 'android_chrome_mobile', 'desktop_chrome'}:
    raise ValueError(
        "Invalid browser.request_profile. Use 'default', 'android_chrome_mobile', or 'desktop_chrome'."
    )

if BROWSER_SERVICE_WORKERS not in {'allow', 'block'}:
    raise ValueError("Invalid browser.service_workers. Use 'allow' or 'block'.")

if LLM_CONNECT_TIMEOUT_SECONDS <= 0:
    raise ValueError('llm.connect_timeout_seconds must be > 0')

if LLM_READ_TIMEOUT_SECONDS <= 0:
    raise ValueError('llm.read_timeout_seconds must be > 0')

if PLAYWRIGHT_POST_LOAD_WAIT_MS < 0:
    raise ValueError('playwright.post_load_wait_ms must be >= 0')

if BROWSER_AUTO_SCROLL_STEP_PX <= 0:
    raise ValueError('browser.auto_scroll_step_px must be > 0')

if BROWSER_AUTO_SCROLL_WAIT_MS < 0:
    raise ValueError('browser.auto_scroll_wait_ms must be >= 0')

if BROWSER_AUTO_SCROLL_MAX_STEPS < 0:
    raise ValueError('browser.auto_scroll_max_steps must be >= 0')

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
ANDROID_CHROME_MOBILE_USER_AGENT = (
    'Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36'
)
ANDROID_CHROME_MOBILE_HEADERS = {
    'accept-language': 'en-US,en;q=0.9,ko;q=0.8',
    'sec-ch-ua': '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    'sec-ch-ua-mobile': '?1',
    'sec-ch-ua-platform': '"Android"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'same-origin',
    'sec-fetch-user': '?1',
}
DESKTOP_CHROME_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36'
)
DESKTOP_CHROME_HEADERS = {
    **ANDROID_CHROME_MOBILE_HEADERS,
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Desktop"',
}
MAX_RETRIES = 3
RETRY_DELAY = 2  # 초


def build_browser_context_options() -> dict[str, Any]:
    """
    Build Playwright browser context options from config.properties.
    """
    if BROWSER_REQUEST_PROFILE == 'android_chrome_mobile':
        return {
            'user_agent': ANDROID_CHROME_MOBILE_USER_AGENT,
            'extra_http_headers': ANDROID_CHROME_MOBILE_HEADERS,
            'is_mobile': True,
            'has_touch': True,
            'viewport': {
                'width': 412,
                'height': 915,
            },
            'device_scale_factor': 2.625,
            'service_workers': BROWSER_SERVICE_WORKERS,
        }

    if BROWSER_REQUEST_PROFILE == 'desktop_chrome':
        return {
            'user_agent': DESKTOP_CHROME_USER_AGENT,
            'extra_http_headers': DESKTOP_CHROME_HEADERS,
            'viewport': {
                'width': 1440,
                'height': 900,
            },
            'device_scale_factor': 1,
            'service_workers': BROWSER_SERVICE_WORKERS,
        }

    return {
        'user_agent': USER_AGENT,
        'service_workers': BROWSER_SERVICE_WORKERS,
    }


def attach_browser_diagnostics(page) -> None:
    """
    Print focused browser diagnostics while rendering.
    """
    if not BROWSER_DIAGNOSTICS_ENABLED:
        return

    def log_console_message(message) -> None:
        print(f"[Browser][Console][{message.type}] {message.text}")

    def log_page_error(error) -> None:
        print(f"[Browser][PageError] {error}")

    def log_request_failed(request) -> None:
        failure = request.failure or 'unknown'
        print(
            f"[Browser][RequestFailed] {request.method} {request.url} "
            f"failure={failure}"
        )

    def log_response(response) -> None:
        if response.status >= 400:
            print(
                f"[Browser][HTTP {response.status}] "
                f"{response.request.method} {response.url}"
            )

    def log_frame_navigated(frame) -> None:
        if frame == page.main_frame:
            print(f"[Browser][Navigation] main_frame_url={frame.url}")

    page.on('console', log_console_message)
    page.on('pageerror', log_page_error)
    page.on('requestfailed', log_request_failed)
    page.on('response', log_response)
    page.on('framenavigated', log_frame_navigated)


def get_site_domain(hostname: str) -> str:
    """
    Return a simple site-domain approximation for request filtering.
    """
    hostname = hostname.lower().strip('.')
    parts = hostname.split('.')
    if len(parts) <= 2:
        return hostname
    return '.'.join(parts[-2:])


def domain_matches(hostname: str, allowed_domain: str) -> bool:
    """
    True when hostname is allowed_domain or one of its subdomains.
    """
    hostname = hostname.lower().strip('.')
    allowed_domain = allowed_domain.lower().strip('.')
    return hostname == allowed_domain or hostname.endswith(f'.{allowed_domain}')


def resolve_primary_domain(url: str) -> str:
    """
    Resolve the primary domain used by browser third-party request filtering.
    """
    if BROWSER_PRIMARY_DOMAIN_OVERRIDE:
        return BROWSER_PRIMARY_DOMAIN_OVERRIDE

    hostname = urlparse(url).hostname or ''
    return get_site_domain(hostname)


async def attach_domain_request_filter(context, source_url: str) -> None:
    """
    Optionally block browser requests outside the source site's primary domain.
    """
    if not BROWSER_BLOCK_THIRD_PARTY_DOMAINS:
        return

    primary_domain = resolve_primary_domain(source_url)
    allowed_domains = [primary_domain, *BROWSER_ALLOWED_EXTRA_DOMAINS]

    async def route_request(route, request) -> None:
        parsed = urlparse(request.url)
        if parsed.scheme not in {'http', 'https'}:
            await route.continue_()
            return

        hostname = parsed.hostname or ''
        if any(domain_matches(hostname, domain) for domain in allowed_domains):
            await route.continue_()
            return

        if BROWSER_DIAGNOSTICS_ENABLED:
            print(
                f"[Browser][BlockedThirdParty] {request.method} {request.url}"
            )
        await route.abort()

    print(
        "[Info] Browser third-party domain blocking enabled: "
        f"primary={primary_domain}, extra={BROWSER_ALLOWED_EXTRA_DOMAINS or []}"
    )
    await context.route('**/*', route_request)


async def get_rendered_html(page) -> str:
    """
    Return rendered HTML, optionally embedding open shadow roots for analysis.
    """
    if not BROWSER_CAPTURE_SHADOW_DOM:
        return await page.content()

    return await page.evaluate(
        """
        () => {
          const copyShadowRoots = (source, target) => {
            if (!source || !target || source.nodeType !== Node.ELEMENT_NODE) {
              return;
            }

            if (source.shadowRoot) {
              const template = document.createElement('template');
              template.setAttribute('shadowrootmode', source.shadowRoot.mode || 'open');
              template.innerHTML = source.shadowRoot.innerHTML;
              target.appendChild(template);
            }

            const sourceChildren = Array.from(source.children || []);
            const targetChildren = Array.from(target.children || []).filter(
              (child) => !(child.tagName === 'TEMPLATE' && child.hasAttribute('shadowrootmode'))
            );

            for (let i = 0; i < sourceChildren.length; i += 1) {
              copyShadowRoots(sourceChildren[i], targetChildren[i]);
            }
          };

          const clone = document.documentElement.cloneNode(true);
          copyShadowRoots(document.documentElement, clone);
          return '<!DOCTYPE html>\\n' + clone.outerHTML;
        }
        """
    )


async def auto_scroll_page(page) -> None:
    """
    Scroll through the page to trigger lazy rendering before capture.
    """
    if not BROWSER_AUTO_SCROLL_ENABLED:
        return

    print(
        "[Info] Auto-scrolling page "
        f"(step={BROWSER_AUTO_SCROLL_STEP_PX}px, "
        f"wait={BROWSER_AUTO_SCROLL_WAIT_MS}ms, "
        f"max_steps={BROWSER_AUTO_SCROLL_MAX_STEPS})"
    )
    result = await page.evaluate(
        """
        async ({ stepPx, waitMs, maxSteps }) => {
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          let steps = 0;
          let lastY = -1;

          while (steps < maxSteps) {
            const currentY = window.scrollY || document.documentElement.scrollTop || 0;
            const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
            const scrollHeight = Math.max(
              document.body ? document.body.scrollHeight : 0,
              document.documentElement ? document.documentElement.scrollHeight : 0
            );

            if (currentY + viewportHeight >= scrollHeight - 2) {
              break;
            }

            window.scrollBy(0, stepPx);
            await sleep(waitMs);

            const nextY = window.scrollY || document.documentElement.scrollTop || 0;
            if (nextY === lastY || nextY === currentY) {
              break;
            }

            lastY = nextY;
            steps += 1;
          }

          await sleep(waitMs);
          window.scrollTo(0, 0);
          await sleep(waitMs);

          return {
            steps,
            scrollHeight: Math.max(
              document.body ? document.body.scrollHeight : 0,
              document.documentElement ? document.documentElement.scrollHeight : 0
            )
          };
        }
        """,
        {
            'stepPx': BROWSER_AUTO_SCROLL_STEP_PX,
            'waitMs': BROWSER_AUTO_SCROLL_WAIT_MS,
            'maxSteps': BROWSER_AUTO_SCROLL_MAX_STEPS,
        },
    )
    print(
        f"[Info] Auto-scroll complete: steps={result.get('steps')}, "
        f"scrollHeight={result.get('scrollHeight')}"
    )


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
            if PLAYWRIGHT_POST_LOAD_WAIT_MS > 0:
                print(f"[Info] Waiting {PLAYWRIGHT_POST_LOAD_WAIT_MS}ms after load")
                await page.wait_for_timeout(PLAYWRIGHT_POST_LOAD_WAIT_MS)
            await auto_scroll_page(page)
            html_content = await get_rendered_html(page)
            print(f"[Success] Rendered {url} (Attempt {attempt}/{MAX_RETRIES})")
            return html_content
        except PlaywrightTimeoutError:
            print(f"[Warning] Timeout fetching {url} (Attempt {attempt}/{MAX_RETRIES}). "
                  f"Fallback to current DOM.")
            try:
                return await get_rendered_html(page)
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


async def save_render_diagnostics(page, rendered_html_path: Path | None) -> None:
    """
    Save screenshot and visible body text next to the rendered HTML artifact.
    """
    if not BROWSER_SAVE_RENDER_DIAGNOSTICS:
        return

    if not SAVE_TO_FILE or rendered_html_path is None:
        print('[Info] Render diagnostics save skipped because rendered HTML was not saved.')
        return

    screenshot_path = rendered_html_path.with_suffix('.png')
    inner_text_path = rendered_html_path.with_name(
        f"{rendered_html_path.stem}_inner_text.txt"
    )

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[Success] Render screenshot saved to {screenshot_path}")
    except (OSError, RuntimeError) as e:
        print(f"[Warning] Failed to save render screenshot: {e}")

    try:
        inner_text = await page.locator('body').inner_text(timeout=5000)
        inner_text_path.write_text(inner_text, encoding='utf-8')
        print(f"[Success] Render innerText saved to {inner_text_path}")
    except (OSError, RuntimeError, PlaywrightTimeoutError) as e:
        print(f"[Warning] Failed to save render innerText: {e}")


async def save_frames_html(page, rendered_html_path: Path | None) -> None:
    """
    Save non-main frame HTML as a separate diagnostic artifact.
    """
    if not BROWSER_CAPTURE_FRAMES_HTML:
        return

    if not SAVE_TO_FILE or rendered_html_path is None:
        print('[Info] Frame HTML capture skipped because rendered HTML was not saved.')
        return

    frames = [frame for frame in page.frames if frame != page.main_frame]
    frames_path = rendered_html_path.with_name(
        f"{rendered_html_path.stem}_frames.html"
    )

    sections: list[str] = [
        '<!DOCTYPE html>',
        '<html>',
        '<head><meta charset="utf-8"><title>Captured Frames</title></head>',
        '<body>',
        f'<h1>Captured Frames for {rendered_html_path.name}</h1>',
    ]

    for index, frame in enumerate(frames, start=1):
        frame_url = frame.url
        frame_name = frame.name
        sections.append(
            f'<section data-frame-index="{index}">'
            f'<h2>Frame {index}</h2>'
            f'<p><strong>Name:</strong> {frame_name}</p>'
            f'<p><strong>URL:</strong> {frame_url}</p>'
        )
        try:
            frame_html = await frame.content()
            sections.append('<pre>')
            sections.append(
                frame_html
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
            )
            sections.append('</pre>')
        except (OSError, RuntimeError, PlaywrightTimeoutError) as e:
            sections.append(f'<p><strong>Error:</strong> {e}</p>')
        sections.append('</section>')

    sections.extend(['</body>', '</html>'])

    try:
        frames_path.write_text('\n'.join(sections), encoding='utf-8')
        print(
            f"[Success] Captured {len(frames)} frame(s) HTML to {frames_path}"
        )
    except OSError as e:
        print(f"[Warning] Failed to save frame HTML: {e}")


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
    prompt_template = GEO_PROMPT_TEMPLATE
    if not SEMANTIC_ANALYSIS_ENABLED:
        prompt_template = prune_semantic_analysis_from_prompt(prompt_template)
    return prompt_template.replace('{{CLEAN_HTML_CONTENT}}', clean_html_content)


def prune_semantic_analysis_from_prompt(prompt_template: str) -> str:
    """
    semantic_analysis 비활성화 시 출력 JSON 스키마 블록만 제거합니다.
    """
    semantic_schema_pattern = (
        r'\n  "semantic_analysis": \{.*?\n  \},\n'
        r'  "rag_optimization": \{'
    )
    prompt_without_semantic_schema = re.sub(
        semantic_schema_pattern,
        '\n  "rag_optimization": {',
        prompt_template,
        flags=re.DOTALL,
    )

    return prompt_without_semantic_schema


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


def save_llm_error_artifact(
    source_url: str,
    *,
    trace_id: str,
    attempt: int,
    max_tokens: int,
    request_id: str,
    reason: str,
    error_message: str,
    timing: dict[str, Any] | None = None,
) -> Path | None:
    """
    Save timeout/network diagnostics for LLM calls.
    """
    payload = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'source_url': source_url,
        'trace_id': trace_id,
        'attempt': attempt,
        'max_tokens': max_tokens,
        'request_id': request_id,
        'reason': reason,
        'error': error_message,
        'timing': timing or {},
    }
    file_name = f"llm_error_{trace_id}_a{attempt}.json"
    return save_debug_json(LLM_RAW_RESPONSE_DIR, file_name, payload)


def post_chat_completion_with_diagnostics(
    payload: dict[str, Any],
    request_id: str,
    stream: bool = False,
) -> tuple[int, str, dict[str, float]]:
    """
    Call chat completions endpoint with split connect/read timeouts and timings.
    """
    parsed = urlparse(LLM_API_URL)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError(f'Invalid llm.api_url: {LLM_API_URL}')

    path = parsed.path or '/'
    if parsed.query:
        path = f"{path}?{parsed.query}"

    body_bytes = json.dumps(payload).encode('utf-8')
    connection_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == 'https'
        else http.client.HTTPConnection
    )

    t0 = time.perf_counter()
    conn = connection_cls(parsed.netloc, timeout=LLM_CONNECT_TIMEOUT_SECONDS)
    try:
        conn.request(
            'POST',
            path,
            body=body_bytes,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream' if stream else 'application/json',
                'X-Request-Id': request_id,
            },
        )
        t1 = time.perf_counter()

        if conn.sock is not None:
            conn.sock.settimeout(LLM_READ_TIMEOUT_SECONDS)

        response = conn.getresponse()
        t2 = time.perf_counter()

        content_type = (response.getheader('Content-Type') or '').lower()
        raw_body: bytes
        first_chunk_seconds: float | None = None

        if stream and 'text/event-stream' in content_type:
            stream_events: list[dict[str, Any]] = []
            while True:
                line_bytes = response.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode('utf-8', errors='replace').strip()
                if not line or not line.startswith('data:'):
                    continue

                data_text = line[5:].strip()
                if data_text == '[DONE]':
                    break

                if first_chunk_seconds is None:
                    first_chunk_seconds = time.perf_counter() - t1

                try:
                    stream_events.append(json.loads(data_text))
                except json.JSONDecodeError:
                    continue

            raw_body = json.dumps(
                build_chat_completion_from_stream(stream_events),
                ensure_ascii=False,
            ).encode('utf-8')
        else:
            raw_body = response.read()

        t3 = time.perf_counter()

        timing = {
            'connect_and_send_seconds': round(t1 - t0, 3),
            'ttfb_seconds': round(t2 - t1, 3),
            'read_body_seconds': round(t3 - t2, 3),
            'total_seconds': round(t3 - t0, 3),
        }
        if first_chunk_seconds is not None:
            timing['first_stream_chunk_seconds'] = round(first_chunk_seconds, 3)
        return response.status, raw_body.decode('utf-8', errors='replace'), timing
    finally:
        conn.close()


def build_chat_completion_from_stream(
    stream_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Convert OpenAI-compatible stream chunks into a single chat completion payload.
    """
    content_parts: list[str] = []
    finish_reason: str | None = None
    response_id: str = ''
    model_name: str = ''

    for event in stream_events:
        if not isinstance(event, dict):
            continue

        if not response_id and isinstance(event.get('id'), str):
            response_id = event['id']
        if not model_name and isinstance(event.get('model'), str):
            model_name = event['model']

        choices = event.get('choices')
        if not isinstance(choices, list) or not choices:
            continue

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            continue

        delta = first_choice.get('delta')
        if isinstance(delta, dict):
            delta_content = delta.get('content')
            if isinstance(delta_content, str):
                content_parts.append(delta_content)

        choice_finish_reason = first_choice.get('finish_reason')
        if isinstance(choice_finish_reason, str) and choice_finish_reason:
            finish_reason = choice_finish_reason

    return {
        'id': response_id,
        'object': 'chat.completion',
        'model': model_name,
        'choices': [
            {
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': ''.join(content_parts),
                },
                'finish_reason': finish_reason or 'stop',
            }
        ],
    }


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
        request_id = f"{trace_id}-a{idx}-t{max_tokens}-{uuid.uuid4().hex[:8]}"

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
            'stream': LLM_STREAM_ENABLED,
        }

        try:
            status, body, timing = post_chat_completion_with_diagnostics(
                payload,
                request_id,
                stream=LLM_STREAM_ENABLED,
            )
            if DEBUG_ENABLED:
                print(
                    "[Debug] LLM timing "
                    f"(attempt={idx}, request_id={request_id}): "
                    f"connect+send={timing['connect_and_send_seconds']}s, "
                    f"ttfb={timing['ttfb_seconds']}s, "
                    f"read={timing['read_body_seconds']}s, "
                    f"total={timing['total_seconds']}s"
                )

            if status >= 400:
                save_llm_repair_text_artifact(
                    source_url,
                    f'http_error_a{idx}',
                    body,
                    trace_id=trace_id,
                )
                save_llm_error_artifact(
                    source_url,
                    trace_id=trace_id,
                    attempt=idx,
                    max_tokens=max_tokens,
                    request_id=request_id,
                    reason=f'http_status_{status}',
                    error_message=body[:2000],
                    timing=timing,
                )
                print(
                    f"[Warning] LLM API HTTP {status} "
                    f"(attempt={idx}, request_id={request_id})"
                )
                continue

            response_json = json.loads(body)
            if isinstance(response_json, dict):
                response_json['_trace'] = {
                    'trace_id': trace_id,
                    'attempt': idx,
                    'max_tokens': max_tokens,
                    'request_id': request_id,
                    'timing': timing,
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
                    f"(attempt={idx}, max_tokens={max_tokens}, request_id={request_id}). Retrying."
                )
                continue

            print(
                f"[Success] LLM called: {LLM_API_URL} "
                f"(attempt={idx}, max_tokens={max_tokens}, request_id={request_id})"
            )
            return response_json
        except json.JSONDecodeError as e:
            save_llm_repair_text_artifact(
                source_url,
                f'invalid_json_a{idx}',
                body if 'body' in locals() else str(e),
                trace_id=trace_id,
            )
            save_llm_error_artifact(
                source_url,
                trace_id=trace_id,
                attempt=idx,
                max_tokens=max_tokens,
                request_id=request_id,
                reason='invalid_json',
                error_message=str(e),
            )
            print(
                f"[Warning] LLM call returned invalid JSON "
                f"(attempt={idx}, request_id={request_id}): {e}"
            )
            continue
        except socket.timeout as e:
            save_llm_error_artifact(
                source_url,
                trace_id=trace_id,
                attempt=idx,
                max_tokens=max_tokens,
                request_id=request_id,
                reason='socket_timeout',
                error_message=str(e),
            )
            print(
                f"[Warning] LLM call timed out "
                f"(attempt={idx}, request_id={request_id}, "
                f"connect_timeout={LLM_CONNECT_TIMEOUT_SECONDS}s, "
                f"read_timeout={LLM_READ_TIMEOUT_SECONDS}s): {e}"
            )
            continue
        except OSError as e:
            save_llm_error_artifact(
                source_url,
                trace_id=trace_id,
                attempt=idx,
                max_tokens=max_tokens,
                request_id=request_id,
                reason='os_error',
                error_message=str(e),
            )
            print(
                f"[Warning] LLM call failed "
                f"(attempt={idx}, request_id={request_id}): {e}"
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
        return filter_geo_result_json_by_config(llm_json)

    if 'choices' in llm_json:
        extracted = extract_geo_json_from_llm_response(llm_json, source_url=source_url)
        return filter_geo_result_json_by_config(extracted) if extracted else None

    return None


def filter_geo_result_json_by_config(geo_result_json: dict[str, Any]) -> dict[str, Any]:
    """
    설정 기반으로 GEO 결과 JSON을 필터링합니다.
    """
    filtered = dict(geo_result_json)
    if not SEMANTIC_ANALYSIS_ENABLED:
        filtered.pop('semantic_analysis', None)
    return filtered


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
        rag_injection_enabled=RAG_INJECTION_ENABLED,
        rag_injection_target=RAG_INJECTION_TARGET,
        rag_tldr_max_chars=RAG_TLDR_MAX_CHARS,
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

    geo_result_json = filter_geo_result_json_by_config(geo_result_json)

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


def run_pipeline_from_preprocessed_file(
    preprocessed_file: Path,
    source_url: str | None = None,
) -> None:
    """
    preprocessed HTML 파일을 입력받아 LLM 이후 단계를 실행합니다.
    (llm -> parse -> save -> inject)
    """
    source_url = source_url or infer_source_url_from_filename(preprocessed_file)
    print(f"[Info] Start preprocessed-to-llm mode for {source_url}")

    preprocessed_html = load_text_file(preprocessed_file)
    if preprocessed_html is None:
        return

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

    geo_result_json = filter_geo_result_json_by_config(geo_result_json)

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
    print('Preprocessed-to-LLM Complete:')
    print("=" * 60)
    print(f"✓ Source URL: {source_url}")
    print(f"  Preprocessed File: {preprocessed_file}")
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
    print(f"[Info] Playwright post-load wait: {PLAYWRIGHT_POST_LOAD_WAIT_MS}ms")
    print(f"[Info] Browser request profile: {BROWSER_REQUEST_PROFILE}")
    print(f"[Info] Browser diagnostics enabled: {BROWSER_DIAGNOSTICS_ENABLED}")
    print(f"[Info] Browser render diagnostics save: {BROWSER_SAVE_RENDER_DIAGNOSTICS}")
    print(f"[Info] Browser capture shadow DOM: {BROWSER_CAPTURE_SHADOW_DOM}")
    print(f"[Info] Browser capture frames HTML: {BROWSER_CAPTURE_FRAMES_HTML}")
    print(f"[Info] Browser auto-scroll enabled: {BROWSER_AUTO_SCROLL_ENABLED}")
    print(f"[Info] Browser service workers: {BROWSER_SERVICE_WORKERS}")
    print(f"[Info] Browser block third-party domains: {BROWSER_BLOCK_THIRD_PARTY_DOMAINS}")
    print(f"[Info] File save enabled: {SAVE_TO_FILE}")
    if SAVE_TO_FILE:
        print(f"[Info] Saving rendered HTML to {RENDERED_HTML_DIR}")
        print(f"[Info] Saving preprocessor results to {PREPROCESSOR_RESULT_DIR}")
        print(f"[Info] Saving LLM prompts to {LLM_PROMPT_DIR}")
        print(f"[Info] Saving LLM results to {LLM_RESULT_DIR}")
    print(f"[Info] LLM API endpoint: {LLM_API_URL}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(**build_browser_context_options())
        await attach_domain_request_filter(context, url)
        page = await context.new_page()
        attach_browser_diagnostics(page)
        
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
                await save_render_diagnostics(page, rendered_html_path)
                await save_frames_html(page, rendered_html_path)

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
                                geo_result_json = filter_geo_result_json_by_config(geo_result_json)
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
                                    rag_injection_enabled=RAG_INJECTION_ENABLED,
                                    rag_injection_target=RAG_INJECTION_TARGET,
                                    rag_tldr_max_chars=RAG_TLDR_MAX_CHARS,
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


async def process_url_render_upload_only(url: str):
    """
    Render a URL with Playwright, save the rendered HTML locally, upload it to
    object storage, then stop before preprocessor/LLM/injection steps.
    """
    print(f"[Info] Starting render/upload-only mode for {url}")
    print(f"[Info] Uploading results to {BUCKET_NAME}")
    print(f"[Info] Playwright goto timeout: {PLAYWRIGHT_GOTO_TIMEOUT_MS}ms")
    print(f"[Info] Playwright post-load wait: {PLAYWRIGHT_POST_LOAD_WAIT_MS}ms")
    print(f"[Info] Browser request profile: {BROWSER_REQUEST_PROFILE}")
    print(f"[Info] Browser diagnostics enabled: {BROWSER_DIAGNOSTICS_ENABLED}")
    print(f"[Info] Browser render diagnostics save: {BROWSER_SAVE_RENDER_DIAGNOSTICS}")
    print(f"[Info] Browser capture shadow DOM: {BROWSER_CAPTURE_SHADOW_DOM}")
    print(f"[Info] Browser capture frames HTML: {BROWSER_CAPTURE_FRAMES_HTML}")
    print(f"[Info] Browser auto-scroll enabled: {BROWSER_AUTO_SCROLL_ENABLED}")
    print(f"[Info] Browser service workers: {BROWSER_SERVICE_WORKERS}")
    print(f"[Info] Browser block third-party domains: {BROWSER_BLOCK_THIRD_PARTY_DOMAINS}")
    print(f"[Info] File save enabled: {SAVE_TO_FILE}")
    if SAVE_TO_FILE:
        print(f"[Info] Saving rendered HTML to {RENDERED_HTML_DIR}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(**build_browser_context_options())
        await attach_domain_request_filter(context, url)
        page = await context.new_page()
        attach_browser_diagnostics(page)

        try:
            print('[Step][Start] Render page with Playwright')
            html = await fetch_and_render_html(
                page,
                url,
                timeout_ms=PLAYWRIGHT_GOTO_TIMEOUT_MS,
            )
            print(f"[Step][End] Render page with Playwright: {'success' if html else 'failed'}")

            if not html:
                print("\n" + "=" * 60)
                print("Render/Upload-Only Failed:")
                print("=" * 60)
                print(f"✗ {url}")
                print("=" * 60)
                return

            print('[Step][Start] Save rendered HTML locally')
            rendered_html_path = save_rendered_html(url, html)
            print(
                f"[Step][End] Save rendered HTML locally: "
                f"{'saved' if rendered_html_path else 'skipped_or_failed'}"
            )
            await save_render_diagnostics(page, rendered_html_path)
            await save_frames_html(page, rendered_html_path)

            print('[Step][Start] Upload rendered HTML to object storage')
            public_url = upload_to_object_storage(url, html)
            print(
                f"[Step][End] Upload rendered HTML to object storage: "
                f"{'success' if public_url else 'failed'}"
            )

            print("\n" + "=" * 60)
            print("Render/Upload-Only Complete:")
            print("=" * 60)
            print(f"✓ {url}")
            print(f"  Rendered HTML Path: {rendered_html_path}")
            print(f"  Public URL: {public_url}")
            print(f"  Size: {len(html):,} bytes")
            print("=" * 60)
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
        '--render-upload-only',
        action='store_true',
        help='Render target URL, upload HTML to object storage, then stop.',
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
        '--preprocessed-for-llm-file',
        help='Preprocessed HTML file name/path to run from LLM stage.',
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

    # Mode 1: render and upload only by target URL
    if (
        args.target_url
        and args.render_upload_only
        and not args.rendered_html_file
        and not args.preprocessed_file
        and not args.preprocessed_for_llm_file
        and not args.llm_response_file
    ):
        asyncio.run(process_url_render_upload_only(args.target_url))
    # Mode 2: full pipeline by target URL
    elif (
        args.target_url
        and not args.render_upload_only
        and not args.rendered_html_file
        and not args.preprocessed_file
        and not args.preprocessed_for_llm_file
        and not args.llm_response_file
    ):
        asyncio.run(process_url(args.target_url))
    # Mode 3: resume from rendered HTML file
    elif (
        args.rendered_html_file
        and not args.render_upload_only
        and not args.preprocessed_file
        and not args.preprocessed_for_llm_file
        and not args.llm_response_file
        and not args.target_url
    ):
        rendered_path = resolve_input_file(args.rendered_html_file, RENDERED_HTML_DIR)
        run_pipeline_from_rendered_file(rendered_path, args.source_url)
    # Mode 4: run from preprocessed HTML at LLM stage
    elif (
        args.preprocessed_for_llm_file
        and not args.render_upload_only
        and not args.rendered_html_file
        and not args.preprocessed_file
        and not args.llm_response_file
        and not args.target_url
    ):
        preprocessed_path = resolve_input_file(
            args.preprocessed_for_llm_file,
            PREPROCESSOR_RESULT_DIR,
        )
        run_pipeline_from_preprocessed_file(preprocessed_path, args.source_url)
    # Mode 5: injection-only from preprocessed + llm files
    elif (
        args.preprocessed_file
        and args.llm_response_file
        and not args.render_upload_only
        and not args.rendered_html_file
        and not args.preprocessed_for_llm_file
        and not args.target_url
    ):
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
            '(1) --render-upload-only target_url, '\
            '(2) target_url, '\
            '(3) --rendered-html-file <file>, '\
            '(4) --preprocessed-for-llm-file <file>, '\
            '(5) --preprocessed-file <file> --llm-response-file <file>'
        )
 
