# The Furnace image: one container per host runs the API, nginx, keepalived and the timers.
#
#   docker build -f docker/Dockerfile -t furnace:VERSION .      (from the repository folder)
#
# Run it with docker/docker-compose.yml: host network (keepalived moves the shared IP between
# the two hosts), NET_ADMIN and NET_RAW, and two volumes. See README section "Docker".

# Stage 1: Flask, gunicorn and what they need, pinned and hash-checked (packaging/vendor.lock).
FROM debian:12-slim AS vendor
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends curl ca-certificates python3 >/dev/null \
 && rm -rf /var/lib/apt/lists/*
COPY packaging/vendor.sh packaging/vendor.lock /src/packaging/
RUN /src/packaging/vendor.sh /vendor \
 && find /vendor -type d -exec chmod 0755 {} + && find /vendor -type f -exec chmod 0644 {} +

# Stage 2: the image.
FROM debian:12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="Furnace" \
      org.opencontainers.image.description="Blocklist server that publishes IP and domain lists (EDLs, threat feeds)" \
      org.opencontainers.image.version="${VERSION}"
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 nginx keepalived openssl curl ca-certificates \
        sqlite3 iproute2 tini gzip tar tzdata procps >/dev/null \
 && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default \
 && useradd --system --user-group --home-dir /var/lib/furnace --no-create-home --shell /usr/sbin/nologin furnace \
 && ln -sf /dev/stdout /var/log/nginx/furnace-access.log \
 && ln -sf /dev/stderr /var/log/nginx/furnace-error.log \
 && mkdir -p /etc/keepalived

COPY --from=vendor /vendor /usr/lib/furnace/vendor
COPY furnace/*.py /usr/lib/furnace/furnace/
COPY deploy/templates/*.tmpl /usr/lib/furnace/templates/
COPY deploy/furnace-python deploy/scripts/furnace-notify deploy/scripts/furnace-health-check /usr/lib/furnace/bin/
COPY docker/furnace /usr/bin/furnace
RUN chmod 0755 /usr/lib/furnace/bin/* /usr/bin/furnace \
 && chmod -R go-w /usr/lib/furnace \
 && /usr/lib/furnace/bin/furnace-python -c 'import flask, gunicorn, furnace.container'

# The same folders as on a server. /etc/furnace and /var/lib/furnace are the volumes;
# the list files are written again from the database at every start.
ENV FURNACE_DATA_DIR=/var/lib/furnace \
    FURNACE_EDL_DIR=/var/lib/furnace-edl \
    FURNACE_AUTH_DIR=/var/lib/furnace-auth \
    FURNACE_ARCHIVE_DIR=/var/lib/furnace/archive \
    FURNACE_BACKUP_DIR=/var/lib/furnace/backups \
    FURNACE_ROLE_FILE=/run/furnace-state/role \
    FURNACE_SELFTEST_FLAG=/run/furnace-state/selftest \
    FURNACE_REPLICA_SECRET_FILE=/etc/furnace/secrets/replica-secret \
    FURNACE_CONFIG=/nonexistent
VOLUME ["/etc/furnace", "/var/lib/furnace"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS --max-time 4 -o /dev/null --cacert /etc/furnace/tls/furnace.crt https://127.0.0.1/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/lib/furnace/bin/furnace-python", "-m", "furnace.container"]
CMD ["start"]
