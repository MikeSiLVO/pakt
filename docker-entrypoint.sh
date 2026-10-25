#!/bin/sh
# Refuses a config mounted under /root, else hands it to the pakt user and runs the command as it
set -e
config=/home/pakt/.config/pakt
if [ -f /root/.config/pakt/config.json ] && [ ! -f "$config/config.json" ]; then
    echo "The config volume is mounted at /root/.config/pakt. Mount it at $config instead." >&2
    exit 1
fi
if [ "$(id -u)" = 0 ]; then
    [ "$(stat -c %u "$config")" = "$(id -u pakt)" ] || chown -R pakt:pakt "$config"
    exec setpriv --reuid=pakt --regid=pakt --init-groups "$@"
fi
exec "$@"
