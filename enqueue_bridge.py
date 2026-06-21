import argparse
import hmac
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
from redis.exceptions import RedisError

from redis_queue import RedisGeoQueue, create_queue_from_config, get_setting, load_properties


@dataclass(frozen=True)
class EnqueueBridgeSettings:
    host: str
    port: int
    auth_token: str

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
        )


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
    print(
        f"[Bridge][Init] Listening on {settings.host}:{settings.port}; "
        f"queue={queue.settings.queue_name}"
    )


async def on_cleanup(app: web.Application) -> None:
    queue = app.get('queue')
    if queue is not None:
        await queue.close()
        print('[Bridge][Shutdown] Redis connection closed.')


def create_app(settings: EnqueueBridgeSettings | None = None) -> web.Application:
    app = web.Application()
    app['settings'] = settings or EnqueueBridgeSettings.from_env_and_properties()
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
        )
    if args.port:
        settings = EnqueueBridgeSettings(
            host=settings.host,
            port=args.port,
            auth_token=settings.auth_token,
        )

    if not settings.auth_token:
        raise SystemExit(
            'enqueue_bridge.auth_token or GEO_ENQUEUE_BRIDGE_AUTH_TOKEN is required.'
        )

    web.run_app(create_app(settings), host=settings.host, port=settings.port)


if __name__ == '__main__':
    main()
