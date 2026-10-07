# Bricklogger as a container: the program in /opt/bricklogger, the configuration
# in /etc/bricklogger and the data in /var/lib/bricklogger, with the plugins in a
# volume instead of in the image. See docs/docker.md.
#
#   docker build -t bricklogger .
#   docker run -d --network host -v bricklogger-config:/etc/bricklogger \
#     -v bricklogger-data:/var/lib/bricklogger bricklogger

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.9.17

# The wheel ---------------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# The wheel, and the locked dependencies to install it against, so an image
# built today and one built tomorrow hold the same versions.
RUN set -eu; \
	uv build --wheel --out-dir /wheel; \
	uv export --frozen --no-dev --no-emit-project --format requirements.txt \
		-o /wheel/requirements.txt

# The image ---------------------------------------------------------------------

FROM python:${PYTHON_VERSION}-slim
ARG PYTHON_VERSION

LABEL org.opencontainers.image.title="Bricklogger" \
      org.opencontainers.image.description="Data bridge for building automation: collects data based on a Brick model and writes it to time-series databases" \
      org.opencontainers.image.source="https://github.com/CX1-ApS/bricklogger" \
      org.opencontainers.image.licenses="MIT"

COPY --from=uv /uv /opt/bricklogger/bin/uv

# The environment under /opt/bricklogger, the configuration in /etc/bricklogger,
# the data in /var/lib/bricklogger, and an unprivileged user that owns the two
# directories the volumes mount over.
RUN set -eu; \
	groupadd --system bricklogger; \
	useradd --system --gid bricklogger --home-dir /var/lib/bricklogger \
		--shell /usr/sbin/nologin --comment Bricklogger bricklogger; \
	/opt/bricklogger/bin/uv venv --python /usr/local/bin/python3 /opt/bricklogger/venv

COPY --from=build /wheel /tmp/wheel
RUN set -eu; \
	/opt/bricklogger/bin/uv pip install --python /opt/bricklogger/venv/bin/python \
		--no-cache --requirement /tmp/wheel/requirements.txt /tmp/wheel/*.whl; \
	rm -rf /tmp/wheel

# The plugin volume, on the path **after** the image's own packages, so a
# shared dependency is the image's. A .pth file is appended to sys.path, where
# PYTHONPATH would come first.
RUN set -eu; \
	printf '%s\n' /var/lib/bricklogger/plugins \
		> "/opt/bricklogger/venv/lib/python${PYTHON_VERSION}/site-packages/bricklogger-plugins.pth"; \
	mkdir -p /etc/bricklogger /var/lib/bricklogger/plugins; \
	chown -R bricklogger:bricklogger /etc/bricklogger /var/lib/bricklogger; \
	chmod 750 /etc/bricklogger /var/lib/bricklogger

COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/bricklogger-entrypoint

ENV PATH="/opt/bricklogger/venv/bin:${PATH}" \
	BRICKLOGGER_CONFIG_DIR=/etc/bricklogger \
	BRICKLOGGER_PLUGIN_DIR=/var/lib/bricklogger/plugins \
	UV_CACHE_DIR=/tmp/uv-cache \
	PYTHONUNBUFFERED=1

USER bricklogger
WORKDIR /var/lib/bricklogger

# The daemon's API, the web interface and the MCP server over HTTP. On the host
# network — what BACnet/IP needs — these are the host's own ports, and what is
# reachable is decided by the bindings in daemon.yaml.
EXPOSE 8420 8421 8422

ENTRYPOINT ["/usr/local/bin/bricklogger-entrypoint"]
CMD ["bricklogger", "daemon", "run"]
