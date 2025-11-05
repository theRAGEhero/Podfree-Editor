#!/bin/sh
set -e

# Ensure shared volumes have the expected permissions for the app user.
for dir in /app/data /app/logs /app/public; do
    if [ -d "$dir" ]; then
        chown -R 1000:1000 "$dir" 2>/dev/null || true
    fi
done

exec "$@"
