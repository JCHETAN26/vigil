# Vigil

Agent reliability and evaluation platform. Traces, tests, and scores AI agents
and RAG pipelines, detects quality regressions with statistical confidence, and
includes an investigation agent that diagnoses regressions and proposes fixes.

See [CLAUDE.md](./CLAUDE.md) for architecture and conventions.

## Local infrastructure

The backing services run in Docker: **Redpanda** (Kafka-compatible queue),
**ClickHouse** (traces), **Postgres** (metadata, eval results), and **Redis**
(cache, job queue). All published ports are bound to `127.0.0.1`, so nothing is
exposed to your local network.

### Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version`)

### Start the stack

```bash
# One-time: create your local env file and set passwords
cp .env.example .env
$EDITOR .env

# Bring everything up (run from the repo root)
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

### Check health

```bash
docker compose --env-file .env -f deploy/docker-compose.yml ps
```

Wait until every service shows `(healthy)`. Follow logs with:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml logs -f
```

### Endpoints (host)

| Service    | Endpoint                     | Notes                              |
|------------|------------------------------|------------------------------------|
| Redpanda   | `localhost:19092`            | Kafka API (external listener)      |
| Redpanda   | `localhost:18081`            | Schema Registry                    |
| Redpanda   | `localhost:18082`            | HTTP Proxy (pandaproxy)            |
| Redpanda   | `localhost:9644`             | Admin API                          |
| ClickHouse | `localhost:8123` / `:9000`   | HTTP / native TCP                  |
| Postgres   | `localhost:5432`             | user/db from `.env`                |
| Redis      | `localhost:6379`             | password from `.env`               |

Inside the Compose network, containers reach Redpanda at `redpanda:9092`.

### Stop the stack

```bash
# Stop and remove containers, keep data volumes
docker compose --env-file .env -f deploy/docker-compose.yml down

# Also delete all data (destructive: wipes traces, metadata, cache)
docker compose --env-file .env -f deploy/docker-compose.yml down -v
```
