FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AVD_DATA_DIR=/data \
    AVD_WORKFLOW_DIR=/app/workflows

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 avd \
    && useradd --uid 10001 --gid avd --create-home avd

WORKDIR /app
# Copy only source inputs: do not bake .env, local data or model weights into layers.
COPY pyproject.toml ./
COPY src ./src
COPY workflows ./workflows
RUN python -m pip install '.[all]' \
    && mkdir -p /data \
    && chown avd:avd /data

USER avd
EXPOSE 8000
# Loopback and host networking also let workers use an SSH tunnel on the host.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"
CMD ["avd", "serve", "--host", "127.0.0.1", "--port", "8000"]
