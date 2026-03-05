import multiprocessing
import os
import signal
import sys
import time

from redis import Redis
from rq import Connection, Queue, Worker

# Ensure /app/api is importable when running from monorepo root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_PATH = os.path.join(ROOT, "api")
if API_PATH not in sys.path:
    sys.path.insert(0, API_PATH)

from app.core.config import get_settings  # noqa: E402



def _worker_process(queue_name: str, redis_url: str, index: int) -> None:
    connection = Redis.from_url(redis_url)
    queue = Queue(queue_name, connection=connection)
    worker_name = f"flash-worker-{index}"
    with Connection(connection):
        worker = Worker([queue], name=worker_name)
        worker.work(with_scheduler=True)



def main() -> None:
    settings = get_settings()
    process_count = max(settings.max_concurrency, 1)

    workers: list[multiprocessing.Process] = []

    def _shutdown(*_: object) -> None:
        for proc in workers:
            if proc.is_alive():
                proc.terminate()
        for proc in workers:
            proc.join(timeout=5)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    for i in range(process_count):
        proc = multiprocessing.Process(
            target=_worker_process,
            args=(settings.queue_name, settings.redis_url, i + 1),
            daemon=False,
        )
        proc.start()
        workers.append(proc)

    while True:
        alive = [proc for proc in workers if proc.is_alive()]
        if not alive:
            break
        time.sleep(1)


if __name__ == "__main__":
    main()
