FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HFCD_DATA_DIR=/data \
    HFCD_PORT=8080

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runs unprivileged. The mounted data volume must be writable by this uid:
#   chown -R 10001:10001 /mnt/<pool>/<dataset>
# or override with `user:` in compose. The app refuses to start otherwise
# rather than writing runs into the container's ephemeral layer.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin hfcd
USER 10001

# Everything durable - the SQLite file and every screenshot - lives here.
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/healthz', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "app"]
