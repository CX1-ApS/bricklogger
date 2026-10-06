"""A plugin whose import fails, for the registry's failure path: an entry point
pointing here loads to nothing, as one with a missing dependency does."""

raise ImportError("No module named 'nothing_installed'")
