#!/bin/sh
# Container start: prepares the persistent data directory, then starts the app.
#  - DATA_DIR on a mounted disk (Render/Railway): the demo data from the image (data/aris, data/drive) is copied there
#    on every start; zerogap.sqlite and outbox/ on the disk are never touched.
#  - Secret Files (Render puts them in /etc/secrets) are copied to $DATA_DIR/secrets once. The app refreshes the
#    tokens there, so an existing file is never overwritten. After a new login: delete it on the disk, then restart.
set -e
D="${DATA_DIR:-/app/data}"
mkdir -p "$D/secrets"
if [ "$(cd "$D" && pwd -P)" != "$(cd /app/data && pwd -P)" ]; then
  cp -r /app/data/aris /app/data/drive "$D"/
fi
if [ -d /etc/secrets ]; then
  for f in /etc/secrets/*; do
    if [ -f "$f" ] && [ ! -e "$D/secrets/$(basename "$f")" ]; then
      cp "$f" "$D/secrets/"
      echo "secret file copied: $(basename "$f")"
    fi
  done
fi
exec python app.py
