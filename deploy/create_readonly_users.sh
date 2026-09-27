#!/usr/bin/env bash
# Create (idempotently) the READ-ONLY database roles the dashboard API uses, in Postgres and
# ClickHouse, so the API physically cannot write even if a bug tried to. Run once after the
# stack is up; safe to re-run (it also resets the RO passwords to the current env values).
#
# Reads admin credentials and the RO credentials from the environment (source the repo .env):
#   POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB   (admin, to create the role)
#   POSTGRES_RO_USER / POSTGRES_RO_PASSWORD           (the read-only role to create)
#   CLICKHOUSE_USER / CLICKHOUSE_PASSWORD             (admin)
#   CLICKHOUSE_RO_USER / CLICKHOUSE_RO_PASSWORD       (the read-only user to create)
# The RO password is a controlled local secret (not request data); it is interpolated into DDL.
set -euo pipefail

: "${POSTGRES_USER:?}"; : "${POSTGRES_PASSWORD:?}"; : "${POSTGRES_DB:?}"
: "${POSTGRES_RO_USER:?}"; : "${POSTGRES_RO_PASSWORD:?}"
: "${CLICKHOUSE_USER:?}"; : "${CLICKHOUSE_PASSWORD:?}"
: "${CLICKHOUSE_RO_USER:?}"; : "${CLICKHOUSE_RO_PASSWORD:?}"

echo "==> Postgres: read-only role ${POSTGRES_RO_USER}"
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" -i vigil-postgres \
  psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${POSTGRES_RO_USER}') THEN
    CREATE ROLE ${POSTGRES_RO_USER} LOGIN PASSWORD '${POSTGRES_RO_PASSWORD}';
  ELSE
    ALTER ROLE ${POSTGRES_RO_USER} LOGIN PASSWORD '${POSTGRES_RO_PASSWORD}';
  END IF;
END
\$\$;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${POSTGRES_RO_USER};
GRANT USAGE ON SCHEMA public TO ${POSTGRES_RO_USER};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${POSTGRES_RO_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ${POSTGRES_RO_USER};
REVOKE CREATE ON SCHEMA public FROM ${POSTGRES_RO_USER};
SQL

echo "==> ClickHouse: read-only user ${CLICKHOUSE_RO_USER}"
docker exec -i vigil-clickhouse clickhouse-client \
  -u "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" --multiquery <<SQL
CREATE USER IF NOT EXISTS ${CLICKHOUSE_RO_USER} IDENTIFIED BY '${CLICKHOUSE_RO_PASSWORD}' SETTINGS readonly = 1;
ALTER USER ${CLICKHOUSE_RO_USER} IDENTIFIED BY '${CLICKHOUSE_RO_PASSWORD}' SETTINGS readonly = 1;
GRANT SELECT ON ${CLICKHOUSE_DB:-vigil}.* TO ${CLICKHOUSE_RO_USER};
SQL

echo "==> done. The dashboard API uses these RO credentials (engine.config.pg_ro_dsn + ClickHouseClient)."
