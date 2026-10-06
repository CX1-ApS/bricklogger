# Contributing

This repository is where Bricklogger is **released**: each commit is a
released version, published from the project's own development repository,
where the work and its history live. A pull request here therefore cannot be
merged as it stands.

What helps most:

- **A bug report** in the [issues](https://github.com/CX1-ApS/bricklogger/issues):
  the version (`bricklogger --version`), what you did, what you expected, and
  what happened instead — with `bricklogger status warnings` and the relevant
  lines of the log where they say something.
- **A feature request** in the issues, described by what you need rather than
  by how it should be built. Bricklogger is developed documentation first:
  a feature is described and decided in the documentation before it is
  written.
- **A plugin.** A source for another protocol or system needs no change to
  Bricklogger at all: it is a package of its own, written against the SDK and
  installed with `bricklogger plugins add`, as
  [plugins](https://cx1-aps.github.io/bricklogger/features/plugins/) describes.

A vulnerability is reported privately, as [SECURITY.md](SECURITY.md)
describes.
