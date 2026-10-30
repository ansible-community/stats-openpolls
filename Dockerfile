FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app.py polls.yml Ansible-Wordmark-Large-RGB-Black.png ./
RUN mkdir -p /etc/open-polls \
    && cp /app/polls.yml /etc/open-polls/polls.yml \
    && useradd --system --uid 10001 --no-create-home appuser \
    && chown -R appuser:appuser /app

USER 10001
EXPOSE 8080
ENV PORT=8080 \
    CONFIG_PATH=/etc/open-polls/polls.yml

CMD ["/app/.venv/bin/gunicorn", "--bind", "0.0.0.0:8080", "--worker-class", "gthread", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "app:app"]
