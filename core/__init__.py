"""AutoFind core: scanner, extractors, chunking, embeddings, storage, search."""

from .__version__ import __version__

# The loopback server's default port, shared by the launcher, CLI and app.
DEFAULT_PORT = 8765

__all__ = ["__version__", "DEFAULT_PORT"]
