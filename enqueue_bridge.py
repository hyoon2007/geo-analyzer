import argparse
import hmac
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
from redis.exceptions import RedisError

from redis_queue import RedisGeoQueue, create_queue_from_config, get_setting, load_properties

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = BASE_DIR / 'logs' / 'enqueue_bridge.log'
LOGGER_NAME = 'enqueue_bridge'
logger = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class EnqueueBridgeSettings:
    host: str
    port: int
    auth_token: str
    log_file: str

    @classmethod
    def from_env_and_properties(cls) -> 'EnqueueBridgeSettings':
        properties = load_properties()
        return cls(
            host=get_setting(
                properties,
                'GEO_ENQUEUE_BRIDGE_HOST',
                'enqueue_bridge.host',
                '0.0.0.0',
            ),
            port=int(
                get_setting(
                    properties,
                    'GEO_ENQUEUE_BRIDGE_PORT',
                    'enqueue_bridge.port',
                    '8080',
                )
            ),
            auth_token=get_setting(
                properties,
                'GEO_ENQUEUE_BRIDGE_AUTH_TOKEN',
                'enqueue_bridge.auth_token',
                '',
            ),
            log_file=get_setting(
                properties,
                'GEO_ENQUEUE_BRIDGE_LOG_FILE',
                'enqueue_bridge.log_file',
                str(DEFAULT_LOG_FILE),
            ),
        )


def configure_logging(log_file: str) -> None:
    log_path = Path(log_file).expanduser()
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.setLevel(logging.INFO)
    logger.propagate = False

    resolved_log_path = log_path.resolve()
    for handler in logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == resolved_log_path
        ):
            return

    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()

    handler = logging.FileHandler(resolved_log_path, encoding='utf-8')
    handler.setFormatter(
        logging.Formatter('%(asctime)s %(levelname)s [%(name)s] %(message)s')
    )
    logger.addHandler(handler)


def response_json_payload(response: web.StreamResponse) -> dict[str, Any]:
    if not isinstance(response, web.Response):
        return {}
    if response.content_type != 'application/json' or not response.text:
        return {}
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def response_error_message(response: web.StreamResponse) -> str:
    payload = response_json_payload(response)
    for key in ('message', 'status', 'detail'):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return response.reason or ''


@web.middleware
async def request_logging_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    started_at = time.monotonic()
    method = request.method
    path = request.rel_url.path_qs
    remote = request.remote or '-'
    user_agent = request.headers.get('User-Agent', '-')

    try:
        response = await handler(request)
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        logger.exception(
            "error method=%s path=%r status=500 remote=%s duration_ms=%.2f "
            "message=%r",
            method,
            path,
            remote,
            elapsed_ms,
            str(exc),
        )
        raise

    elapsed_ms = (time.monotonic() - started_at) * 1000
    status = response.status
    logger.info(
        "access method=%s path=%r status=%s remote=%s duration_ms=%.2f "
        "user_agent=%r",
        method,
        path,
        status,
        remote,
        elapsed_ms,
        user_agent,
    )

    if status >= 400:
        logger.error(
            "error method=%s path=%r status=%s remote=%s duration_ms=%.2f "
            "message=%r",
            method,
            path,
            status,
            remote,
            elapsed_ms,
            response_error_message(response),
        )

    return response


def json_response(payload: dict[str, Any], status: int) -> web.Response:
    return web.json_response(payload, status=status)


def extract_bearer_token(request: web.Request) -> str:
    header = request.headers.get('Authorization', '')
    prefix = 'Bearer '
    if not header.startswith(prefix):
        return ''
    return header[len(prefix):].strip()


def is_authorized(request: web.Request, expected_token: str) -> bool:
    if not expected_token:
        return False
    return hmac.compare_digest(extract_bearer_token(request), expected_token)


def validate_enqueue_payload(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, 'request body must be a JSON object'

    url = payload.get('url')
    if not isinstance(url, str) or not url.strip():
        return None, 'url is required'

    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None, 'url must be an absolute http or https URL'

    mode = payload.get('mode', 'full')
    if mode != 'full':
        return None, "mode must be 'full'"

    priority = payload.get('priority', 0)
    if not isinstance(priority, int):
        return None, 'priority must be an integer'

    request_id = payload.get('request_id')
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip()
    ):
        return None, 'request_id must be a non-empty string when provided'

    metadata = payload.get('metadata', {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        return None, 'metadata must be an object when provided'

    return {
        'url': url,
        'mode': mode,
        'priority': priority,
        'request_id': request_id.strip() if isinstance(request_id, str) else None,
        'metadata': metadata,
    }, None


def get_queue(request: web.Request) -> RedisGeoQueue:
    return request.app['queue']


def get_settings(request: web.Request) -> EnqueueBridgeSettings:
    return request.app['settings']


async def handle_healthz(_: web.Request) -> web.Response:
    return json_response({'status': 'ok'}, status=200)


async def handle_readyz(request: web.Request) -> web.Response:
    queue = get_queue(request)
    try:
        await queue.client.ping()
        return json_response({'status': 'ready'}, status=200)
    except RedisError as exc:
        return json_response(
            {
                'status': 'not_ready',
                'message': str(exc),
            },
            status=503,
        )


async def handle_enqueue(request: web.Request) -> web.Response:
    settings = get_settings(request)
    if not is_authorized(request, settings.auth_token):
        return json_response({'status': 'unauthorized'}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return json_response(
            {
                'status': 'invalid_request',
                'message': 'request body must be valid JSON',
            },
            status=400,
        )

    normalized, error = validate_enqueue_payload(payload)
    if error or normalized is None:
        return json_response(
            {
                'status': 'invalid_request',
                'message': error or 'invalid request',
            },
            status=400,
        )

    queue = get_queue(request)
    url = normalized['url']
    try:
        task = await queue.enqueue(
            url,
            mode=normalized['mode'],
            priority=normalized['priority'],
            request_id=normalized['request_id'],
            extra={'metadata': normalized['metadata']},
        )
    except RedisError as exc:
        return json_response(
            {
                'status': 'unavailable',
                'message': 'failed to enqueue task',
                'detail': str(exc),
            },
            status=503,
        )

    lock_key = queue.lock_key(url)
    if task is None:
        return json_response(
            {
                'status': 'duplicate',
                'message': 'lock already exists',
                'url': url,
                'lock_key': lock_key,
            },
            status=409,
        )

    return json_response(
        {
            'status': 'enqueued',
            'queue': queue.settings.queue_name,
            'request_id': task['request_id'],
            'url': url,
            'lock_key': lock_key,
            'task': task,
        },
        status=202,
    )


async def on_startup(app: web.Application) -> None:
    queue = create_queue_from_config()
    await queue.initialize()
    app['queue'] = queue
    settings = app['settings']
    logger.info(
        'startup host=%s port=%s queue=%s log_file=%s',
        settings.host,
        settings.port,
        queue.settings.queue_name,
        settings.log_file,
    )
    print(
        f"[Bridge][Init] Listening on {settings.host}:{settings.port}; "
        f"queue={queue.settings.queue_name}"
    )


async def on_cleanup(app: web.Application) -> None:
    queue = app.get('queue')
    if queue is not None:
        await queue.close()
        logger.info('shutdown Redis connection closed')
        print('[Bridge][Shutdown] Redis connection closed.')


def create_app(settings: EnqueueBridgeSettings | None = None) -> web.Application:
    app_settings = settings or EnqueueBridgeSettings.from_env_and_properties()
    configure_logging(app_settings.log_file)
    app = web.Application(middlewares=[request_logging_middleware])
    app['settings'] = app_settings
    app.router.add_get('/healthz', handle_healthz)
    app.router.add_get('/readyz', handle_readyz)
    app.router.add_post('/geo/offline/enqueue', handle_enqueue)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run GEO HTTP enqueue bridge.')
    parser.add_argument('--host', help='Host to bind. Overrides config/env.')
    parser.add_argument('--port', type=int, help='Port to bind. Overrides config/env.')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = EnqueueBridgeSettings.from_env_and_properties()
    if args.host:
        settings = EnqueueBridgeSettings(
            host=args.host,
            port=settings.port,
            auth_token=settings.auth_token,
            log_file=settings.log_file,
        )
    if args.port:
        settings = EnqueueBridgeSettings(
            host=settings.host,
            port=args.port,
            auth_token=settings.auth_token,
            log_file=settings.log_file,
        )

    if not settings.auth_token:
        raise SystemExit(
            'enqueue_bridge.auth_token or GEO_ENQUEUE_BRIDGE_AUTH_TOKEN is required.'
        )

    web.run_app(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=None,
    )


if __name__ == '__main__':
    main()
