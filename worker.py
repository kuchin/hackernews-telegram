import os

from dotenv import load_dotenv

from celery import Celery

from utils import configure_file_logging, logger


load_dotenv()


log_path = configure_file_logging("log_worker.log")
logger.info("Worker logging configured", log_path=str(log_path))

BROKER_URL = os.getenv("CELERY_BROKER_URL")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)

# Single Celery instance handles beat + worker; all tasks live in tasks.py.
celery_app = Celery("tgnews", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.imports = ("tasks",)
celery_app.conf.timezone = os.getenv("CELERY_TIMEZONE", "UTC")
celery_app.conf.beat_schedule = {
    # Single-beat schedule: let the staging loop decide which articles to
    # ship each tick instead of queuing per-story tasks.
    "poll-hn": {
        "task": "tasks.publish_latest",
        "schedule": int(os.getenv("CELERY_POLL_INTERVAL", 600)),
    }
}
