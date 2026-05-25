FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

COPY src/analysis/pyproject.toml src/analysis/uv.lock ./
RUN uv sync --frozen --no-dev

# Base analysis source, then overlay stop-loss specific files on top
COPY src/analysis/src/ ./src/
COPY src/analysis_sl/src/ ./src/
COPY src/shared/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "src/main.py"]
