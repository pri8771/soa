#!/usr/bin/env bash
# Verifies that local development services are up and healthy.
# Used locally after `make local-up` and by CI where Docker is supported.
set -euo pipefail

COMPOSE_FILE="$(dirname "$0")/docker-compose.yml"

fail=0
for service in postgres minio mailpit; do
  status="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Health}}' "$service" 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then
    echo "OK       $service"
  else
    echo "UNHEALTHY $service (status: ${status:-not running})"
    fail=1
  fi
done

exit "$fail"
