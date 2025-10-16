.PHONY: run
run:
	uv run python bot.py

.PHONY: worker
worker:
	uv run celery -A worker.celery_app worker --beat -s ./.data/celery --loglevel=info

.PHONY: up
up:
	docker compose up --watch --build app worker

.PHONY: down
down:
	docker compose down
