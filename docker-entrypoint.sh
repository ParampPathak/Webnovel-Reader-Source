#!/bin/sh
set -eu
# Gunicorn opens worker heartbeat files before loading the Flask factory.
# Prepare its disk-backed scratch first, including read-only staging containers.
TASK_TMPDIR=${TMPDIR:-${DATA_DIR:-/data}/tmp}
case "$TASK_TMPDIR" in
  /tmp|/tmp/*|/dev/shm|/dev/shm/*)
    echo 'Use disk-backed application scratch rather than /tmp or /dev/shm.' >&2
    exit 1
    ;;
esac
mkdir -p "$TASK_TMPDIR"
export TMPDIR="$TASK_TMPDIR"
exec "$@"
