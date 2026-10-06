#!/bin/sh
# The container's entrypoint: the configuration on the first start, the plugins
# in the volume after an upgrade, then the command it was given.
#
# Neither step stops the container. A daemon with an empty configuration runs
# and reports `idle`, and a plugin that cannot be installed shows as failed in
# the catalogue — see docs/docker.md.

set -eu

CONFIG_DIR="${BRICKLOGGER_CONFIG_DIR:-/etc/bricklogger}"

say() { printf 'bricklogger: %s\n' "$*" >&2; }

# The configuration, written once, while the volume is still empty. `init`
# refuses to overwrite a file that exists, so nothing written later is touched.
if [ -d "$CONFIG_DIR" ] && [ -z "$(ls -A "$CONFIG_DIR" 2>/dev/null)" ]; then
	say "$CONFIG_DIR is empty; writing the four files with commented examples"
	if bricklogger init --non-interactive; then
		# The instances it writes are examples — another building's BACnet
		# address, a database with a password nobody has set — and the daemon
		# refuses a configuration it cannot resolve. They go in commented out,
		# so the daemon starts idle and the examples are there to uncomment.
		for file in sources destinations; do
			sed -i '/^[[:space:]]*#/!{/^[[:space:]]*$/!s/^/# /;}' \
				"$CONFIG_DIR/$file.yaml"
		done
	else
		say "init could not write the configuration; the daemon starts with none"
	fi
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
