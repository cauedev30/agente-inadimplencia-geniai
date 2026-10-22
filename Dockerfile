# Imagem do agente. Sobe a migração e o serviço; a porta vem de PORT (padrão 8000).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" AGENTE_MODO=sombra
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn agente.api.principal:montar_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
