import argparse
import asyncio
import os
import signal
import socket
import uuid
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from main import process_url_with_browser
from redis_queue import RedisGeoQueue, create_queue_from_config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_worker_id() -> str:
    hostname = os.getenv('HOSTNAME') or socket.gethostname()
    return os.getenv('GEO_WORKER_ID', f"geo-worker-{hostname}-{uuid.uuid4().hex[:8]}")


async def heartbeat_lock(
    queue: RedisGeoQueue,
    task: dict,
    stop_event: asyncio.Event,
    interval_seconds: int,
) -> None:
    url = task['url']
    token = task['token']
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            extended = await queue.extend_lock(url, token)
            if extended:
                print(f"[Worker][Heartbeat] Extended lock for {url}")
            else:
                print(f"[Worker][Heartbeat][Warning] Failed to extend lock for {url}")


async def run_task(
    *,
    browser,
    queue: RedisGeoQueue,
    task: dict,
    worker_id: str,
) -> None:
    url = task['url']
    token = task['token']
    mode = task.get('mode', 'full')
    request_id = task.get('request_id', '')
    heartbeat_interval = max(30, queue.settings.lock_ttl_seconds // 3)
    stop_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        heartbeat_lock(queue, task, stop_heartbeat, heartbeat_interval)
    )

    print(
        f"[Worker][Start] request_id={request_id} worker_id={worker_id} "
        f"mode={mode} url={url}"
    )

    try:
        if mode != 'full':
            raise ValueError(f"Unsupported worker task mode: {mode}")

        await process_url_with_browser(browser, url)
        await queue.store_result(
            task,
            {
                'status': 'success',
                'worker_id': worker_id,
            },
        )
        print(f"[Worker][Success] request_id={request_id} url={url}")
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        target_queue = await queue.record_failure(task, error_message)
        await queue.store_result(
            task,
            {
                'status': 'failed',
                'worker_id': worker_id,
                'failed_at': utc_now(),
                'error': error_message,
                'failure_queue': target_queue,
            },
        )
        print(
            f"[Worker][Failure] request_id={request_id} url={url} "
            f"failure_queue={target_queue} error={error_message}"
        )
    finally:
        stop_heartbeat.set()
        await heartbeat_task
        released = await queue.release_lock(url, token)
        if released:
            print(f"[Worker][Lock] Released lock for {url}")
        else:
            print(f"[Worker][Lock][Warning] Lock not released for {url}; token mismatch or expired")


async def worker_loop(*, once: bool = False) -> None:
    worker_id = build_worker_id()
    shutdown_event = asyncio.Event()

    def request_shutdown() -> None:
        print('[Worker][Shutdown] Signal received. Draining current task before exit.')
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_shutdown())

    queue = create_queue_from_config()
    await queue.initialize()
    print(
        f"[Worker][Init] worker_id={worker_id} queue={queue.settings.queue_name} "
        f"failed_queue={queue.settings.failed_queue_name} "
        f"dead_letter_queue={queue.settings.dead_letter_queue_name}"
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        print('[Worker][Init] Playwright Chromium launched and ready.')

        try:
            while not shutdown_event.is_set():
                task = await queue.dequeue()
                if task is None:
                    if once:
                        print('[Worker][Once] No task available.')
                        break
                    continue

                if task.get('lock_acquired') is False:
                    print(
                        f"[Worker][Skip] Could not acquire lock for {task.get('url')}. "
                        "Another worker may own it."
                    )
                    continue

                await run_task(
                    browser=browser,
                    queue=queue,
                    task=task,
                    worker_id=worker_id,
                )

                if once:
                    break
        finally:
            await browser.close()
            await queue.close()
            print('[Worker][Shutdown] Resources released.')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run GEO Redis resident worker.')
    parser.add_argument(
        '--once',
        action='store_true',
        help='Process at most one queued task, then exit.',
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    asyncio.run(worker_loop(once=args.once))
