#!/bin/sh
# Install Bricklogger on Linux, in one of two modes.
#
#   curl -fsSL https://github.com/CX1-ApS/bricklogger/releases/latest/download/install.sh | sudo sh
#   curl -fsSL https://github.com/CX1-ApS/bricklogger/releases/latest/download/install.sh | sh
#
# With sudo as a service; without it in your home directory.
#
# Run as root it installs a machine-wide service: the environment in
# /opt/bricklogger, the command in /usr/local/bin, the system user bricklogger,
# /etc/bricklogger and /var/lib/bricklogger owned by it, and three systemd units
# left stopped. Run as an ordinary login it touches nothing outside the home
# directory: the environment and the data under ~/.local/share/bricklogger, the
# command in ~/.local/bin, the configuration in ~/.config/bricklogger, and three
# systemd --user units with lingering enabled.
#
# Options: --version V, --wheel FILE, --uninstall. A plugin is added afterwards
# with `bricklogger plugins add`.
# Running it again upgrades in place and leaves configuration and data alone.

set -eu

PYTHON_VERSION=3.12
VERSION=""
WHEEL=""
UNINSTALL=no

usage() {
	cat <<'USAGE'
Usage: install.sh [--version V] [--wheel FILE] [--uninstall]

  --version V     install this version of bricklogger instead of the newest
  --wheel FILE    install a wheel from disk instead of the released package
  --uninstall     remove the environment, the command and the units, and keep
                  the configuration and the data
USAGE
}

while [ $# -gt 0 ]; do
	case "$1" in
	--version)
		VERSION="${2:?--version needs a version}"
		shift 2
		;;
	--version=*)
		VERSION="${1#*=}"
		shift
		;;
	--wheel)
		WHEEL="${2:?--wheel needs a file}"
		shift 2
		;;
	--wheel=*)
		WHEEL="${1#*=}"
		shift
		;;
	--uninstall)
		UNINSTALL=yes
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "install.sh: unknown option $1" >&2
		usage >&2
		exit 2
		;;
	esac
done

say() { printf '%s\n' "$*"; }
die() {
	printf 'install.sh: %s\n' "$*" >&2
	exit 1
}

[ "$(uname -s)" = Linux ] || die "Bricklogger runs on Linux"

if [ "$(id -u)" = 0 ]; then
	MODE=system
	PREFIX=/opt/bricklogger
	CONFIG_DIR=/etc/bricklogger
	DATA_DIR=/var/lib/bricklogger
	BIN_DIR=/usr/local/bin
	UNIT_DIR=/etc/systemd/system
	SERVICE_USER=bricklogger
	SYSTEMCTL="systemctl"
	WANTED_BY=multi-user.target
else
	MODE=user
	PREFIX="$HOME/.local/share/bricklogger"
	CONFIG_DIR="$HOME/.config/bricklogger"
	DATA_DIR="$HOME/.local/share/bricklogger"
	BIN_DIR="$HOME/.local/bin"
	UNIT_DIR="$HOME/.config/systemd/user"
	SERVICE_USER=""
	SYSTEMCTL="systemctl --user"
	WANTED_BY=default.target
fi

VENV="$PREFIX/venv"
UV="$PREFIX/bin/uv"
COMMAND="$BIN_DIR/bricklogger"

if [ "$UNINSTALL" = yes ]; then
	for unit in bricklogger.service bricklogger-web.service bricklogger-mcp.service; do
		if [ -f "$UNIT_DIR/$unit" ]; then
			$SYSTEMCTL disable --now "$unit" >/dev/null 2>&1 || true
			rm -f "$UNIT_DIR/$unit"
		fi
	done
	$SYSTEMCTL daemon-reload >/dev/null 2>&1 || true
	rm -f "$COMMAND"
	rm -rf "$VENV" "$PREFIX/bin" "$PREFIX/python" "$PREFIX/cache"
	say "removed the environment, the command and the units."
	say "the configuration in $CONFIG_DIR and the data in $DATA_DIR are kept;"
	say "remove them by hand when you no longer want them."
	exit 0
fi

command -v curl >/dev/null 2>&1 || die "curl is needed"
[ -z "$WHEEL" ] || [ -f "$WHEEL" ] || die "no such wheel: $WHEEL"

if [ "$MODE" = user ] && [ -e "$HOME/.local/share/uv/tools/bricklogger" ]; then
	die "an older installation made with 'uv tool install bricklogger' is in the
way; remove it with 'uv tool uninstall bricklogger' and run this script again"
fi

# The user and the directories -------------------------------------------------

if [ "$MODE" = system ] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
	useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin \
		--comment "Bricklogger" "$SERVICE_USER"
	say "created the system user $SERVICE_USER"
fi

mkdir -p "$PREFIX/bin" "$CONFIG_DIR" "$DATA_DIR" "$BIN_DIR"
if [ ! -e "$CONFIG_DIR/env" ]; then
	: >"$CONFIG_DIR/env"
	say "created $CONFIG_DIR/env for the secrets"
fi
chmod 600 "$CONFIG_DIR/env"
if [ "$MODE" = system ]; then
	chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR"
	chmod 750 "$CONFIG_DIR" "$DATA_DIR"
fi

# The environment --------------------------------------------------------------

if [ ! -x "$UV" ]; then
	say "fetching uv..."
	curl -fsSL https://astral.sh/uv/install.sh |
		UV_INSTALL_DIR="$PREFIX/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
[ -x "$UV" ] || die "uv was not installed at $UV"

UV_PYTHON_INSTALL_DIR="$PREFIX/python"
UV_CACHE_DIR="$PREFIX/cache"
export UV_PYTHON_INSTALL_DIR UV_CACHE_DIR

if [ ! -x "$VENV/bin/python" ]; then
	say "creating the environment in $VENV..."
	"$UV" venv --quiet --python "$PYTHON_VERSION" "$VENV"
fi

# What was there before, so an upgrade is told what an upgrade needs.
PREVIOUS=""
if [ -x "$VENV/bin/bricklogger" ]; then
	PREVIOUS="$("$VENV/bin/bricklogger" --version 2>/dev/null || true)"
fi

if [ -n "$WHEEL" ]; then
	PACKAGE="$WHEEL"
elif [ -n "$VERSION" ]; then
	PACKAGE="bricklogger==$VERSION"
else
	PACKAGE="bricklogger"
fi

say "installing $PACKAGE..."
# Two builds of the same development wheel carry the same version, and
# --upgrade alone would then leave the installed code in place, so the package
# itself is always reinstalled; its dependencies are only audited.
"$UV" pip install --quiet --python "$VENV/bin/python" --upgrade \
	--reinstall-package bricklogger "$PACKAGE"
"$UV" cache clean --quiet >/dev/null 2>&1 || true

ln -sf "$VENV/bin/bricklogger" "$COMMAND"

# The units --------------------------------------------------------------------

# The units come from templates in the package, written by the version just
# installed, so the script and `bricklogger update` write them alike.
write_units() {
	if [ "$MODE" = system ]; then
		set -- --user "$SERVICE_USER"
	else
		set -- --config-dir "$CONFIG_DIR"
	fi
	"$VENV/bin/python" -m bricklogger.ops.units write --dir "$UNIT_DIR" \
		--command "$COMMAND" --wanted-by "$WANTED_BY" "$@" >/dev/null
}

UNITS=no
if command -v systemctl >/dev/null 2>&1; then
	write_units
	UNITS=yes
	$SYSTEMCTL daemon-reload >/dev/null 2>&1 ||
		say "note: systemd did not reload; the units are written but unknown to it yet"
	if [ "$MODE" = user ]; then
		loginctl enable-linger "$(id -un)" >/dev/null 2>&1 ||
			say "note: lingering could not be enabled; the service stops at logout"
	fi
fi

# What now ---------------------------------------------------------------------

INSTALLED="$("$VENV/bin/bricklogger" --version)"
say ""
if [ -n "$PREVIOUS" ]; then
	# An upgrade: the configuration exists, and what runs keeps the old code
	# until it is restarted.
	say "$INSTALLED installed, upgraded from $PREVIOUS"
	say ""
	SUDO=""
	[ "$MODE" = user ] || SUDO="sudo "
	RESTART=""
	if [ "$UNITS" = yes ]; then
		for unit in bricklogger.service bricklogger-web.service bricklogger-mcp.service; do
			if $SYSTEMCTL is-active --quiet "$unit" >/dev/null 2>&1; then
				RESTART="$RESTART $unit"
			fi
		done
	fi
	if [ -n "$RESTART" ]; then
		say "What runs keeps the old version until it is restarted:"
		for unit in $RESTART; do
			say "  ${SUDO}$SYSTEMCTL restart $unit"
		done
	else
		say "A daemon started by hand keeps the old version until it is restarted:"
		say "  bricklogger daemon restart"
	fi
	say ""
	say "Then \`bricklogger plugins\` shows whether every plugin still loads. From"
	say "here on, \`bricklogger update\` upgrades in place:"
	say "  ${SUDO}bricklogger update all"
	exit 0
fi
say "$INSTALLED installed"
say "  command:       $COMMAND"
say "  configuration: $CONFIG_DIR"
say "  data:          $DATA_DIR"
[ "$UNITS" = no ] || say "  units:         $UNIT_DIR/bricklogger{,-web,-mcp}.service (stopped)"
say ""
if [ "$MODE" = user ] && [ -d /etc/bricklogger ]; then
	say "note: /etc/bricklogger exists, and commands read it before $CONFIG_DIR."
	say "      Set BRICKLOGGER_CONFIG_DIR=$CONFIG_DIR to work on this install."
	say ""
fi
case ":$PATH:" in
*":$BIN_DIR:"*) ;;
*)
	say "note: $BIN_DIR is not in your PATH; add it to use the command by name."
	say ""
	;;
esac
say "Next: create the configuration, then start the daemon."
say "  bricklogger init"
if [ "$UNITS" = yes ]; then
	if [ "$MODE" = system ]; then
		say "  sudo systemctl enable --now bricklogger"
	else
		say "  systemctl --user enable --now bricklogger"
	fi
else
	say "  bricklogger daemon start"
fi
say "Then upload the model: bricklogger model upload <model.ttl>"
say ""
say "init asks which sources and destinations to configure, keeps the secrets"
say "in $CONFIG_DIR/env, and validates what it wrote. Add"
say "--non-interactive to write commented examples and edit the files by hand."
