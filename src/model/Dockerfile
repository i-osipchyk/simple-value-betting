FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Dependencies layer — cached unless pyproject.toml or uv.lock change
COPY src/model/pyproject.toml src/model/uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/model/src/ ./src/
COPY src/shared/ ./src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "src/main.py"]
