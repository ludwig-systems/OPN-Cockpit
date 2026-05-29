# OPN-Cockpit Container fuer Linux (Debian 12) + WSL2-Docker-Desktop unter Windows.
#
# Build: docker build -t opn-cockpit .
# Run:   docker compose up -d   (siehe docker-compose.yml)
#
# Konfiguration:
#   OPNCOCKPIT_DATA_DIR=/data    - alle Tresor- + Audit-Daten liegen in /data
#   OPNCOCKPIT_HOST=0.0.0.0      - im Container immer auf alle Interfaces
#   OPNCOCKPIT_PORT=9876         - default
#   OPNCOCKPIT_NO_BROWSER=1      - im Container natuerlich kein Browser-Auto-Open
#
# Volumes:
#   /data  - persistente App-Daten (Vault, Audit, Plans, Profile, Settings)

# ---------------------------------------------------------------------------
# Stage 1: Build (venv + dependencies)
# ---------------------------------------------------------------------------
FROM debian:12-slim AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-dev \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN python3 -m venv /opt/opncockpit-venv \
    && /opt/opncockpit-venv/bin/pip install --upgrade pip \
    && /opt/opncockpit-venv/bin/pip install --no-cache-dir -e .

# ---------------------------------------------------------------------------
# Stage 2: Runtime (slim image ohne build-deps)
# ---------------------------------------------------------------------------
FROM debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /usr/sbin/nologin opncockpit \
    && mkdir -p /data \
    && chown -R opncockpit:opncockpit /data

WORKDIR /app
COPY --from=build /opt/opncockpit-venv /opt/opncockpit-venv
COPY --from=build /app/src ./src
COPY pyproject.toml ./

# Damit `python -m opn_cockpit` aus dem editable-install greift, brauchen wir
# das Modul auf sys.path — pip install -e hat das via pth-File geregelt.
ENV PATH="/opt/opncockpit-venv/bin:${PATH}" \
    OPNCOCKPIT_DATA_DIR=/data \
    OPNCOCKPIT_HOST=0.0.0.0 \
    OPNCOCKPIT_PORT=9876 \
    OPNCOCKPIT_NO_BROWSER=1 \
    PYTHONUNBUFFERED=1

USER opncockpit

EXPOSE 9876
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9876/health', timeout=3).read() == b'ok' else 1)"

CMD ["python3", "-m", "opn_cockpit"]
