import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config.properties'

LUA_RELEASE_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

LUA_EXTEND_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


def load_properties(config_path: Path = CONFIG_PATH) -> dict[str, str]:
    if not config_path.exists():
        return {}

    properties: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        properties[key.strip()] = value.strip()
    return properties


def get_setting(
    properties: dict[str, str],
    env_key: str,
    property_key: str,
    default: str,
) -> str:
    value = os.getenv(env_key)
    if value is not None and value.strip():
        return value.strip()
    return properties.get(property_key, default).strip()


@dataclass(frozen=True)
class RedisQueueSettings:
    host: str
    port: int
    password: str
    db: int
    queue_name: str
    failed_queue_name: str
    dead_letter_queue_name: str
    lock_prefix: str
    lock_ttl_seconds: int
    brpop_timeout_seconds: int
    socket_timeout_seconds: int
    socket_connect_timeout_seconds: int
    health_check_interval_seconds: int
    max_attempts: int
    result_prefix: str

    @classmethod
    def from_env_and_properties(cls) -> 'RedisQueueSettings':
        properties = load_properties()
        return cls(
            host=get_setting(properties, 'GEO_REDIS_HOST', 'redis.host', '172.235.215.18'),
            port=int(get_setting(properties, 'GEO_REDIS_PORT', 'redis.port', '6379')),
            password=get_setting(
                properties,
                'GEO_REDIS_PASSWORD',
                'redis.password',
                'GEO_SYSTEM_STRONG_SECRET_AUTH_KEY',
            ),
            db=int(get_setting(properties, 'GEO_REDIS_DB', 'redis.db', '0')),
            queue_name=get_setting(
                properties,
                'GEO_REDIS_QUEUE_NAME',
                'redis.queue_name',
                'Queue:GEO:Tasks',
            ),
            failed_queue_name=get_setting(
                properties,
                'GEO_REDIS_FAILED_QUEUE_NAME',
                'redis.failed_queue_name',
                'Queue:GEO:Failed',
            ),
            dead_letter_queue_name=get_setting(
                properties,
                'GEO_REDIS_DEAD_LETTER_QUEUE_NAME',
                'redis.dead_letter_queue_name',
                'Queue:GEO:DeadLetter',
            ),
            lock_prefix=get_setting(
                properties,
                'GEO_REDIS_LOCK_PREFIX',
                'redis.lock_prefix',
                'Lock:GEO:Analysis',
            ),
            lock_ttl_seconds=int(
                get_setting(properties, 'GEO_REDIS_LOCK_TTL_SECONDS', 'redis.lock_ttl_seconds', '3600')
            ),
            brpop_timeout_seconds=int(
                get_setting(properties, 'GEO_REDIS_BRPOP_TIMEOUT_SECONDS', 'redis.brpop_timeout_seconds', '5')
            ),
            socket_timeout_seconds=int(
                get_setting(
                    properties,
                    'GEO_REDIS_SOCKET_TIMEOUT_SECONDS',
                    'redis.socket_timeout_seconds',
                    '30',
                )
            ),
            socket_connect_timeout_seconds=int(
                get_setting(
                    properties,
                    'GEO_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS',
                    'redis.socket_connect_timeout_seconds',
                    '10',
                )
            ),
            health_check_interval_seconds=int(
                get_setting(
                    properties,
                    'GEO_REDIS_HEALTH_CHECK_INTERVAL_SECONDS',
                    'redis.health_check_interval_seconds',
                    '30',
                )
            ),
            max_attempts=int(
                get_setting(properties, 'GEO_REDIS_MAX_ATTEMPTS', 'redis.max_attempts', '3')
            ),
            result_prefix=get_setting(
                properties,
                'GEO_REDIS_RESULT_PREFIX',
                'redis.result_prefix',
                'GEO:Result',
            ),
        )


class RedisGeoQueue:
    def __init__(self, settings: RedisQueueSettings):
        self.settings = settings
        self.client = aioredis.Redis(
            host=settings.host,
            port=settings.port,
            password=settings.password,
            db=settings.db,
            decode_responses=True,
            socket_timeout=max(
                settings.socket_timeout_seconds,
                settings.brpop_timeout_seconds + 10,
            ),
            socket_connect_timeout=settings.socket_connect_timeout_seconds,
            socket_keepalive=True,
            health_check_interval=settings.health_check_interval_seconds,
        )
        self._release_sha: str | None = None
        self._extend_sha: str | None = None

    async def initialize(self) -> None:
        await self.client.ping()
        self._release_sha = await self.client.script_load(LUA_RELEASE_LOCK)
        self._extend_sha = await self.client.script_load(LUA_EXTEND_LOCK)

    async def close(self) -> None:
        await self.client.aclose()

    def url_hash(self, url: str) -> str:
        return hashlib.sha256(url.encode('utf-8')).hexdigest()

    def lock_key(self, url: str) -> str:
        return f"{self.settings.lock_prefix}:{self.url_hash(url)}"

    def result_key(self, url: str) -> str:
        return f"{self.settings.result_prefix}:{self.url_hash(url)}"

    async def acquire_lock(self, url: str, token: str) -> bool:
        return bool(
            await self.client.set(
                self.lock_key(url),
                token,
                nx=True,
                ex=self.settings.lock_ttl_seconds,
            )
        )

    async def release_lock(self, url: str, token: str) -> bool:
        if not self._release_sha:
            raise RuntimeError('RedisGeoQueue.initialize() must be called first.')
        return bool(await self.client.evalsha(self._release_sha, 1, self.lock_key(url), token))

    async def extend_lock(self, url: str, token: str) -> bool:
        if not self._extend_sha:
            raise RuntimeError('RedisGeoQueue.initialize() must be called first.')
        return bool(
            await self.client.evalsha(
                self._extend_sha,
                1,
                self.lock_key(url),
                token,
                str(self.settings.lock_ttl_seconds),
            )
        )

    async def enqueue(
        self,
        url: str,
        *,
        mode: str = 'full',
        priority: int = 0,
        request_id: str | None = None,
        token: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        request_id = request_id or uuid.uuid4().hex
        token = token or uuid.uuid4().hex
        if not await self.acquire_lock(url, token):
            return None

        task: dict[str, Any] = {
            'request_id': request_id,
            'url': url,
            'mode': mode,
            'priority': priority,
            'token': token,
            'attempts': 0,
            'enqueued_at': datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            task.update(extra)

        try:
            await self.client.lpush(
                self.settings.queue_name,
                json.dumps(task, ensure_ascii=False),
            )
            return task
        except Exception:
            await self.release_lock(url, token)
            raise

    async def dequeue(self) -> dict[str, Any] | None:
        result = await self.client.brpop(
            self.settings.queue_name,
            timeout=self.settings.brpop_timeout_seconds,
        )
        if not result:
            return None

        _, raw_task = result
        task = json.loads(raw_task)
        if not isinstance(task, dict):
            raise ValueError('Queue payload must be a JSON object.')

        task.setdefault('request_id', uuid.uuid4().hex)
        task.setdefault('mode', 'full')
        task.setdefault('attempts', 0)
        task.setdefault('dequeued_at', datetime.now(timezone.utc).isoformat())

        url = task.get('url')
        if not isinstance(url, str) or not url:
            raise ValueError('Queue payload missing url.')

        token = task.get('token')
        if not isinstance(token, str) or not token:
            token = uuid.uuid4().hex
            task['token'] = token
            if not await self.acquire_lock(url, token):
                task['lock_acquired'] = False
                return task

        return task

    async def store_result(self, task: dict[str, Any], result: dict[str, Any]) -> None:
        url = task['url']
        payload = {
            'request_id': task.get('request_id'),
            'url': url,
            'mode': task.get('mode'),
            'completed_at': datetime.now(timezone.utc).isoformat(),
            **result,
        }
        await self.client.setex(
            self.result_key(url),
            86400,
            json.dumps(payload, ensure_ascii=False),
        )

    async def record_failure(self, task: dict[str, Any], error: str) -> str:
        attempts = int(task.get('attempts', 0)) + 1
        failure_payload = {
            **task,
            'attempts': attempts,
            'failed_at': datetime.now(timezone.utc).isoformat(),
            'last_error': error,
        }
        target_queue = (
            self.settings.dead_letter_queue_name
            if attempts >= self.settings.max_attempts
            else self.settings.failed_queue_name
        )
        await self.client.lpush(
            target_queue,
            json.dumps(failure_payload, ensure_ascii=False),
        )
        return target_queue


def create_queue_from_config() -> RedisGeoQueue:
    return RedisGeoQueue(RedisQueueSettings.from_env_and_properties())
