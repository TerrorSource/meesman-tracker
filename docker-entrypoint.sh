#!/bin/sh
# Start als root, zet de app-gebruiker op PUID/PGID (standaard 1000/1000),
# maak /data van die gebruiker en laat de privileges vallen (gosu).
# Zo werkt een data-map die nog van root is (vorige versies) gewoon door.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
  if [ "$(id -g app)" != "$PGID" ]; then groupmod -o -g "$PGID" app; fi
  if [ "$(id -u app)" != "$PUID" ]; then usermod  -o -u "$PUID" app; fi
  chown -R app:app /data /home/app 2>/dev/null || echo "⚠️  chown /data mislukt — controleer PUID/PGID en de rechten van de data-map" >&2
  export HOME=/home/app   # gosu laat HOME op /root staan; Chromium wil een schrijfbare thuismap
  exec gosu app "$@"
fi

exec "$@"
