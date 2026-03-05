from rq import Queue
from rq.job import Job as RQJob
from rq.exceptions import InvalidJobOperation, NoSuchJobError

from app.core.config import get_settings
from app.core.redis_client import get_redis



def get_queue() -> Queue:
    settings = get_settings()
    return Queue(settings.queue_name, connection=get_redis())


def cancel_enqueued_job(job_id: str) -> bool:
    connection = get_redis()
    try:
        rq_job = RQJob.fetch(job_id, connection=connection)
    except NoSuchJobError:
        return False
    except Exception:  # noqa: BLE001
        return False

    try:
        status_value = str(rq_job.get_status(refresh=False) or "").lower()
    except Exception:  # noqa: BLE001
        status_value = ""

    if status_value not in {"queued", "deferred", "scheduled"}:
        return False

    try:
        rq_job.cancel()
        return True
    except InvalidJobOperation:
        return False
    except Exception:  # noqa: BLE001
        try:
            rq_job.delete()
            return True
        except Exception:  # noqa: BLE001
            return False
