# Telegram News Curator

Telegram News Curator keeps a Hacker News channel clean while giving the linked discussion group an on-demand deep dive. The bot posts a headline card to the channel, Telegram auto-forwards it to the discussion group, and the bot layers contextual replies under the forward when somebody taps a button.

## Overview
- Channel-first publishing: headlines stay in the broadcast channel with no inline buttons.
- Discussion-first interaction: highlights, top comments, and related links expand only in the group.
- Background curation: a Celery worker stages Hacker News stories, refreshes their metrics, and graduates only the interesting ones.

## How It Works
1. A user sends `/latest`, `/random`, or `/get <id>` in the discussion group.
2. The bot picks a story, posts the headline to the channel, and records the message id.
3. Telegram auto-forwards the channel post into the linked group.
4. The bot looks up the pending record and posts the first threaded comment with an inline keyboard.
5. Button taps reply in-place.  
   - Forum groups (topics on): replies target the thread via `message_thread_id`.  
   - Non-forum groups: replies fall back to replying to the auto-forward.

## Commands
- `/latest` – Fetch and post the most recent curated articles.
- `/random` – Fetch a random set of curated articles.
- `/get <id | url>` – Fetch a specific Hacker News story by numeric id or canonical URL.

## Getting Started

### Prerequisites
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Redis 6+

### Install & Configure
1. Clone the repository.
2. Copy `.env-example` to `.env` and fill in the required values. At minimum you need `TG_TOKEN`, `TG_CHANNEL_ID`, `TG_DISCUSSION_ID`, `EXA_API_KEY`, and Redis/Celery URLs.
3. Install dependencies: `uv sync`

### Run Locally
```bash
make run      # start the long-polling bot
make worker   # start the Celery worker + beat scheduler
```
Run each command in its own terminal so the bot and worker can operate concurrently.

### Docker Compose
```bash
make up       # build and start bot, worker, and redis with watch mode
make down     # stop the stack
```

## Configuration
The application reads configuration from environment variables (see `.env-example` for the full list). Key settings include:

| Variable | Purpose |
| --- | --- |
| `TG_TOKEN` | Telegram Bot API token. |
| `TG_CHANNEL_ID` / `TG_DISCUSSION_ID` | Numeric ids for the channel and linked discussion group. |
| `EXA_API_KEY` | API key used for story enrichment. |
| `PENDING_REDIS_URL` | Redis connection string used to map channel posts to discussion threads (DB 1). |
| `STAGING_REDIS_URL` | Redis connection string used for staged/published articles (DB 2). |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Redis URLs for Celery broker + backend (DB 0). |
| `ARTICLE_*` constants | Graduation thresholds and aging behaviour (override via env if needed). |
| `CELERY_POLL_INTERVAL` | How frequently the worker evaluates staged articles (seconds). |

## Operations
- Logs are written to `.data/log_bot.log` and `.data/log_worker.log` in JSON format; console output is human readable.
- Redis usage:
  - DB 0 – Celery broker/results.
  - DB 1 – Pending channel posts (`tgnews:pending:<message_id>` with ~15 minute TTL).
  - DB 2 – Staging (`tgnews:staged:*`, published set `tgnews:published`).
- The bot tolerates both forum and non-forum groups; watch for `message_thread_id` in updates to confirm routing.
- Use `make deploy` to rsync the project to the target host and restart the Docker stack (see Makefile comments).

## Troubleshooting
- **No discussion post:** verify the channel and group are linked and the bot is allowed to read auto-forwards (`is_automatic_forward=true` in logs). Privacy mode must be disabled or the bot must be an admin.
- **`message thread not found`:** indicates a non-forum group; ensure the bot is replying rather than using `message_thread_id`.
- **Buttons unresponsive:** callbacks sent from the channel are ignored. Confirm the user tapped the button in the discussion thread and check logs for callback handling.

## Further Reading
See [AGENTS.md](AGENTS.md) for engineering notes, module responsibilities, data flow, and extension points.
