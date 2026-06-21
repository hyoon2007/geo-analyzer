import argparse
import asyncio
import json

from redis_queue import create_queue_from_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Enqueue a GEO analysis task.')
    parser.add_argument('url', help='Target URL to analyze.')
    parser.add_argument(
        '--mode',
        default='full',
        choices=['full'],
        help='Worker pipeline mode.',
    )
    parser.add_argument(
        '--priority',
        type=int,
        default=0,
        help='Task priority metadata.',
    )
    return parser.parse_args()


async def enqueue_task(url: str, mode: str, priority: int) -> int:
    queue = create_queue_from_config()
    await queue.initialize()
    try:
        task = await queue.enqueue(url, mode=mode, priority=priority)
        if task is None:
            print(f"Skipped: lock already exists for {url}")
            return 1

        print(
            json.dumps(
                {
                    'queue': queue.settings.queue_name,
                    'task': task,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        await queue.close()


def main() -> int:
    args = parse_args()
    return asyncio.run(enqueue_task(args.url, args.mode, args.priority))


if __name__ == '__main__':
    raise SystemExit(main())
