FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

COPY src/model/pyproject.toml src/model/uv.lock ./
RUN uv sync --frozen --no-dev

# Base model source, then overlay stop-loss specific files on top
COPY src/model/src/ ./src/
COPY src/model_sl/src/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "src/main.py"]
