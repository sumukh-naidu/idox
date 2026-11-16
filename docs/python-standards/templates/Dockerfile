# One service's image (copy to services/<name>/Dockerfile; replace "extraction" and the port).
# Build from the repo root:  docker build -f services/extraction/Dockerfile .
#
# Pin both images when adopting this template: <PINNED-DIGEST> is the digest of the
# python:3.13-slim image you tested with, <PINNED-UV-VERSION> the uv version in use.

# ---------- build stage: install the service and its locked dependencies into a venv
FROM python:3.13-slim@sha256:<PINNED-DIGEST> AS build
COPY --from=ghcr.io/astral-sh/uv:<PINNED-UV-VERSION> /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY . .
# --locked: fail if uv.lock is out of date. --no-editable: the venv is self-contained.
RUN uv sync --locked --no-dev --no-editable --package extraction

# ---------- runtime stage: Python, the venv and nothing else
FROM python:3.13-slim@sha256:<PINNED-DIGEST>

# System packages this service needs, and only those (OCR engines here; LibreOffice only
# in the conversion image). Example:
# RUN apt-get update \
#  && apt-get install -y --no-install-recommends <package> \
#  && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app
COPY --from=build --chown=app:app /app/.venv /app/.venv
USER app
WORKDIR /home/app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "extraction.main:app", "--host", "0.0.0.0", "--port", "8000"]
