#!/bin/sh
# The container's entrypoint: the plugins in the volume after an upgrade, the
# configuration and the plugins on the daemon's first start, then the command
# it was given.
#
# Neither step stops the container. A daemon with an empty configuration runs
# and reports `idle`, and a plugin that cannot be installed shows as failed in
# `bricklogger plugins` — see docs/docker.md.

set -eu

CONFIG_DIR="${BRICKLOGGER_CONFIG_DIR:-/etc/bricklogger}"

say() { printf 'bricklogger: %s\n' "$*" >&2; }

# The first start is the daemon's container's alone, so two containers never
# write the same files: an empty configuration volume gets the four files — a
# working destination when the database's variables are set, the examples
# commented out — and a plugin volume never asked for anything gets the
# packages in BRICKLOGGER_PLUGINS. Each happens once; see docs/docker.md. It
# runs before the env file below exists, which would make the volume look used.
if [ "$#" -ge 3 ] && [ "$1" = "bricklogger" ] && [ "$2" = "daemon" ] && [ "$3" = "run" ]; then
	python -m bricklogger.ops.first_start || true
fi

# The env file, for the secrets the YAML interpolates as ${NAME}. `touch` and
# not `: >`: a redirection that fails on a special built-in takes the shell down
# with it, and a directory the user cannot write then ended the start here,
# before the daemon was ever reached.
if [ -d "$CONFIG_DIR" ] && [ ! -e "$CONFIG_DIR/env" ]; then
	if touch "$CONFIG_DIR/env" 2>/dev/null; then
		chmod 600 "$CONFIG_DIR/env"
	else
		say "could not create $CONFIG_DIR/env; put the secrets there by hand"
	fi
fi

# The plugin volume, laid down again when this image is not the one it was
# built against. Says what it does, and never stops the start.
python -m bricklogger.ops.plugin_volume || true

exec "$@"
