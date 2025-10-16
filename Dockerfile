# Use Python 3.12 with uv preinstalled
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Speed/behavior tweaks for uv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# 1) Install dependencies with lockfile for reproducible builds
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# 2) Copy the rest of the code, then install the project itself
COPY . /app
COPY .env.prod .env
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Ensure the venv is first on PATH
ENV PATH="/app/.venv/bin:$PATH"

# No default port exposure, no uv entrypoint
ENTRYPOINT []
CMD ["python", "bot.py"]
