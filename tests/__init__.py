"""The test suite.

Typer forces a terminal, colours and all, when GITHUB_ACTIONS, FORCE_COLOR or
PY_COLORS is set, and the escape codes then split the words the CLI tests look
for in help texts. It reads the switch below when ``typer.rich_utils`` is first
imported, so it is set here, before any test module imports the CLI.
"""

import os

os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
