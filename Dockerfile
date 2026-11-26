FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    tigervnc-standalone-server tigervnc-tools novnc websockify \
    && rm -rf /var/lib/apt/lists/*

ENV UV_NO_SYNC=true \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY vendor ./vendor
RUN uv sync --locked --no-dev --extra browser --no-install-project
COPY src ./src
COPY schedule.sh criteria.txt ./
RUN uv sync --locked --no-dev --extra browser --no-editable \
    && mkdir -p /state /home/openrent-browser \
    && chown 1000:1000 /state /home/openrent-browser

ENTRYPOINT ["bash", "/app/schedule.sh"]
