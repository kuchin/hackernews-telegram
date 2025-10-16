# Engineering Notes for Contributors

Read [README.md](README.md) for the product overview and setup checklist. This document dives into the implementation details that matter when you change behaviour, add features, or run the system in production.

## Architecture Overview
- **Bot process (`bot.py`)** – Long-polls Telegram, routes commands and callbacks through `publish.handlers`.
- **Worker process (`worker.py` + `tasks.py`)** – Celery worker with beat scheduler that stages Hacker News stories, refreshes metrics, and publishes graduates.
- **Redis** – Three logical databases:  
  - DB 0 for Celery broker/result backend.  
  - DB 1 for pending channel posts awaiting auto-forward (`tgnews:pending:*`).  
  - DB 2 for staged articles, staging index, and the `tgnews:published` guard set.

Exa enriches stories with summaries and media, Hacker News Firebase provides raw story metadata, and Telegram Bot API handles delivery.

## Codebase Layout
| Path | Purpose |
| --- | --- |
| `bot.py` | Entry point for the interactive bot; configures logging and dispatches updates. |
| `worker.py` | Celery application definition and beat schedule. |
| `tasks.py` | Worker orchestration (`publish_latest`) and helper routines. |
| `clients/` | HTTP clients for Telegram, Hacker News Firebase, and Exa enrichment. |
| `content/reader.py` | Story selection, caching, and enrichment logic shared by bot/worker. |
| `publish/formatter.py` | Caption/comment templating and keyboard builders. |
| `publish/handlers.py` | Message, callback, and auto-forward handlers. |
| `publish/writer.py` | Telegram send utilities with retry/backoff logic. |
| `publish/staging_store.py` | Redis-backed staging persistence. |
| `publish/pending_store.py` | Redis store linking channel posts to discussion targets. |
| `config.py` | Environment variable parsing and tunable constants. |
| `utils.py` | Logging configuration and error formatting helpers. |

## Runtime Flows
### Interactive Bot
1. `bot.py` long-polls `getUpdates` and forwards each update to `publish.handlers`.
2. Commands resolve to `content.reader` calls (`get_articles`, `get_random_articles`, `get_articles_by_ids`).
3. `_publish_articles` posts headlines to the configured channel (if available) and caches the message id in the pending store.
4. When Telegram delivers the auto-forward, `handle_automatic_forward` looks up the pending entry, posts the threaded comment bundle, and removes the mapping.
5. Callback queries render dynamic replies via `publish.formatter` and react to the original message.

### Worker Graduation Loop (`tasks.publish_latest`)
1. Collect top Hacker News ids and drop any already staged/published.
2. Enrich unseen ids via `content.reader.get_articles_by_ids` and persist them in the staging store.
3. Refresh metrics for staged articles that have aged past `ARTICLE_METRIC_REFRESH_SECONDS`.
4. Decide graduation via `_should_graduate` (score, comments, or age fallback).
5. Publish graduates with `publish.writer.send_article_bundle`, create pending records, and move the article id into `tgnews:published`.
6. Discard aged low-engagement articles by marking them published without posting.

## Redis Schema
| Key / Set | Description | TTL |
| --- | --- | --- |
| `tgnews:pending:<channel_msg_id>` | Pending channel post payload with article + discussion chat id. | ~15 minutes |
| `tgnews:staged:<article_id>` | JSON blob with article payload, `staged_at`, `last_checked_ts`. | Persistent |
| `tgnews:staged:index` | Set of staged article ids. | Persistent |
| `tgnews:published` | Set of article ids that have been posted or intentionally discarded. | Persistent |

## Local Development Workflow
1. Install dependencies with `uv sync`.
2. Copy `.env-example` to `.env` and populate credentials.
3. Run the bot with `make run` and the worker with `make worker` in separate terminals.
4. Use `make up` / `make down` for the Docker stack if you prefer containerised services.
5. Logs land in `.data/log_bot.log` and `.data/log_worker.log`; tail them with `jq` or `less`.

Pending/published sets live in Redis; it is safe to flush DB 1 or DB 2 in local development when you need a clean slate.

## Coding Guidelines
- Keep modules small and focused; prefer using existing helpers (`publish.writer`, `publish.handlers`, `content.reader`) instead of duplicating logic.
- Use `logger.info/debug/warning/error` with structured kwargs—Loguru handles JSON output automatically.
- Functions should be fully type-hinted; avoid `from __future__ import annotations` (Python 3.12 already treats annotations as strings).
- Use `uv add/remove` for dependency changes; never hand-edit `pyproject.toml`.
- Favor idempotent Redis operations and handle exceptions defensively—Telegram and third-party APIs can return partial results.

## Extension Points
- **New commands** – Add a handler in `publish.handlers`, register it in `handle_message`, and reuse `_publish_articles` for consistent routing.
- **New callback actions** – Extend `publish.formatter` to format the response and update `handle_callback_query`.
- **New content sources** – Create a client in `clients/`, plug it into `content.reader`, and update the `Article` dataclass if required.
- **Graduation policy tweaks** – Adjust constants in `config.py` or expose new env vars; keep defaults conservative to avoid channel spam.

## Operational Notes
- `make deploy` rsyncs the repository (including `.env.prod`) to the remote host and runs `docker compose up` with rebuilds.
- If the bot misses auto-forwards, inspect `o3news:pending:*`—lack of entries indicates posting failures, lingering entries signal missing forwards.
- For ad-hoc staging inspection, use `publish.staging_store.list_staged_articles()` or `get_staged_article(article_id)` in a Python shell.
- Telegram can flip a discussion between forum and non-forum; the send helpers already retry in both modes, but keep an eye on logs for repeated routing warnings.

## Handy Commands
```bash
uv run python -m publish.staging_store   # drop into a REPL (add your own helper code)
uv run celery -A worker.celery_app shell # Celery control shell for debugging workers
redis-cli -u "$PENDING_REDIS_URL" keys 'o3news:*' # inspect redis state
```
