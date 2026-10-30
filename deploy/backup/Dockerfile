# Nightly pg_dump backups for the production stack (docker-compose.prod.yml, service `backup`).
# postgres:17 for pg_dump/pg_restore matching the server's major version, plus age
# (encryption), rclone (off-site copy) and curl (failure alerts).
FROM postgres:17-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends age rclone curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY backup.sh /usr/local/bin/thymble-backup
COPY restore.sh /usr/local/bin/thymble-restore
RUN chmod 0755 /usr/local/bin/thymble-backup /usr/local/bin/thymble-restore
ENTRYPOINT ["thymble-backup"]
CMD ["schedule"]
