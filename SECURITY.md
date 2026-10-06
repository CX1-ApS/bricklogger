# Security policy

Bricklogger runs beside a building's automation systems, so a vulnerability
is taken seriously even where it seems small.

## Reporting a vulnerability

Report it **privately**, through GitHub's
[private vulnerability reporting](https://github.com/CX1-ApS/bricklogger/security/advisories/new)
on this repository, and not in a public issue. Say what is affected, how it
can be reproduced, and which version you ran. You will get an answer within a
few working days, and a fix is released as a new patch version with the
report credited unless you ask otherwise.

## Supported versions

Fixes are made for the newest minor version. An installation is upgraded by
running the install script again, or by pulling a new image.

## What Bricklogger does and does not do

- It **never writes** to a building automation system: the BACnet/IP source
  has no write path. It is itself a BACnet device on the network, as every
  BACnet client must be, with a device object and a network port object and
  no commandable points: another device can write the logger's own
  properties, and nothing in the building can be driven through it.
- The API, the web interface and the MCP server listen on `127.0.0.1` by
  default. On any other address the configuration requires a token or a
  password, as the [configuration](https://cx1-aps.github.io/bricklogger/features/configuration/)
  describes.
- Secrets are kept in the `env` file in the configuration directory and are
  referred to as `${NAME}` in the YAML; the AI assistant never takes a
  secret's value.
