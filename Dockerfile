# Образ NewsDigest. Ціль `dev` — для локальних команд і тестів (run.cmd / run.sh),
# ціль `runtime` — для майбутнього worker без dev-залежностей.
# TODO(етап 6): зафіксувати digest базового образу замість тега.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/

WORKDIR /app

FROM base AS runtime
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin newsdigest
USER 10001:10001
CMD ["digest", "--help"]

FROM base AS dev
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project
COPY src ./src
COPY tests ./tests
COPY config ./config
RUN uv sync --locked
CMD ["digest", "--help"]
