FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bookbot ./bookbot

RUN useradd --system bookbot
COPY entrypoint.sh /entrypoint.sh

# Starts as root only so the entrypoint can hand a freshly mounted Fly volume (which is
# owned by root) to the bot's user; the bot itself never runs as root.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "bookbot"]
