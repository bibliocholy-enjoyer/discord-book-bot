#!/bin/sh
set -e
if [ -n "$USAGE_DB" ]; then
    # If this fails (say the volume isn't mounted) the bot still starts, and tells its
    # owner that it can't open the usage log.
    chown bookbot "$(dirname "$USAGE_DB")" || echo "Could not chown $(dirname "$USAGE_DB")" >&2
fi
exec setpriv --reuid=bookbot --regid=bookbot --init-groups "$@"
