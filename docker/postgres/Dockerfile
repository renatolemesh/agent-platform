# Postgres 17 + pgvector + wal-g (plan 12 §1, §3). Build context: docker/ (see compose.prod.yaml).
FROM pgvector/pgvector:pg17

ARG WALG_VERSION=v3.0.9
# sha256 of wal-g-pg-22.04-amd64.tar.gz from the GitHub release (bookworm glibc 2.36 >= 2.35 required by the build)
ARG WALG_SHA256=4f03ee4679db7f660bfd1b2b8291ac5df247cd2ec48074b60c112f4897299f24

RUN set -eux; \
    apt-get update; apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL -o /tmp/walg.tgz "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-22.04-amd64.tar.gz"; \
    echo "${WALG_SHA256}  /tmp/walg.tgz" | sha256sum -c -; \
    tar -xzf /tmp/walg.tgz -C /tmp; \
    install -m 0755 /tmp/wal-g-pg-22.04-amd64 /usr/local/bin/wal-g; \
    rm -rf /tmp/walg.tgz /tmp/wal-g-pg-22.04-amd64; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*; \
    wal-g --version

COPY walg/walg-env.sh /usr/local/bin/walg-env
COPY walg/backup-loop.sh /usr/local/bin/walg-backup-loop
COPY postgres/initdb/ /docker-entrypoint-initdb.d/
RUN chmod 0755 /usr/local/bin/walg-env /usr/local/bin/walg-backup-loop
