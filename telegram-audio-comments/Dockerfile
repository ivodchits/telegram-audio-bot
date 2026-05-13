# syntax=docker/dockerfile:1.7
# Two-stage build:
#   1. `builder` installs Python dependencies into a wheelhouse so the final
#      image doesn't ship build toolchains.
#   2. `runtime` is the slim final image with just ffmpeg + the installed
#      wheels + the source tree.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim AS runtime

# ffmpeg is a hard runtime dependency (pydub shells out to it).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Run as a non-root user so a compromised handler can't escalate inside the
# container. The data/ volume is chowned to this user at build time.
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

COPY --chown=app:app . .

# Persisted state (session JSON + audio files). Override DATA_DIR if you want
# the volume mounted elsewhere.
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data && chown -R app:app /app/data
VOLUME ["/app/data"]

USER app

EXPOSE 8080

# Liveness probe — hits /healthz on the same loopback port. python+urllib
# is in the base image so we avoid pulling in curl/wget just for this.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)" \
    || exit 1

# stop_grace_period in docker-compose / --time on `docker stop` should be at
# least SHUTDOWN_DRAIN_SECONDS (default 30s) so in-flight stitches finish
# instead of getting SIGKILL'd mid-export.
STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
