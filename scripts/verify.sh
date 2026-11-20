#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

mode=${1:-local}
case "$mode" in
  local)
    set -- -f compose.yaml
    export STUDIO_AUTH_REQUIRED=0
    : "${STUDIO_PORT:=8766}"
    ;;
  s3)
    set -- -f compose.yaml -f compose.test-s3.yaml
    export STUDIO_AUTH_REQUIRED=1
    : "${STUDIO_PORT:=8767}"
    ;;
  *) printf '%s\n' 'Usage: scripts/verify.sh [local|s3]' >&2; exit 2 ;;
esac

export STUDIO_PORT
export STUDIO_LATE_SECONDS=6 STUDIO_MEDIA_DEADLINE_SECONDS=38
project="creator-check-$mode-$$"

cleanup() {
  code=$?
  trap - EXIT
  if [ "$code" -ne 0 ]; then
    docker compose "$@" -p "$project" ps -a
    docker compose "$@" -p "$project" logs --tail 100
  fi
  docker compose "$@" -p "$project" down -v
  exit "$code"
}

trap 'cleanup "$@"' EXIT
docker compose "$@" -p "$project" up --build -d --wait --wait-timeout 90 --scale worker=2
docker compose "$@" -p "$project" exec -T api python -m unittest discover -s tests -v
