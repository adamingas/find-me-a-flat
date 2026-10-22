FROM ghcr.io/astral-sh/uv:0.12.23-python3.14-trixie-slim

ENV UV_NO_SYNC=true \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY schedule.sh criteria.txt ./
RUN uv sync --locked --no-dev --no-editable

ENTRYPOINT ["bash", "/app/schedule.sh"]
