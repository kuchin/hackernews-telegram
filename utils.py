import sys
from pathlib import Path
from typing import Any, Dict

import yaml
from loguru import logger
from clients.telegram import TelegramAPIError

LOG_DIR = Path(".data")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger.remove()


def _console_format(record: Dict[str, Any]) -> str:
    """Pretty-print console logs while keeping structured extras intact."""
    timestamp = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    source = f"{record['name']}:{record['function']}".replace("<", "\\<").replace(">", "\\>")
    base = (
        f"<green>{timestamp}</green> | "
        f"<level>{record['level'].name:<8}</level> | "
        f"<cyan>{source}</cyan> - {record['message']}"
    )
    extras = record.get("extra") or {}
    if extras:
        extras_yaml = yaml.safe_dump(
            extras,
            sort_keys=False,
            default_flow_style=False,
        ).rstrip()
        padded_yaml = "\n".join(f"  {line}" for line in extras_yaml.splitlines())
        base = f"{base}\n{padded_yaml}"
    return f"{base}\n"


CONSOLE_SINK_ID = logger.add(
    sys.stderr,
    level="INFO",
    colorize=True,
    enqueue=True,
    format=_console_format,
)

_file_sink_id: int | None = None


def configure_file_logging(filename: str, *, rotation: str = "10 MB") -> Path:
    """Attach a file sink for this process and return its path."""
    global _file_sink_id
    # Each process owns a single JSON log; callers reuse the handle so we
    # avoid spraying multiple appenders when modules import utils.
    if _file_sink_id is not None:
        logger.remove(_file_sink_id)
    log_path = LOG_DIR / filename
    _file_sink_id = logger.add(
        log_path,
        rotation=rotation,
        serialize=True,
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )
    return log_path


def format_http_error(err: Exception) -> str:
    """Normalize HTTP-ish exceptions to readable strings for logging."""
    if isinstance(err, TelegramAPIError):
        return err.description or str(err)
    return str(err)
