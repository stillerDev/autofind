"""Single source of truth for the application version.

Every consumer (core.__init__, pyproject.toml via setuptools dynamic attr,
doctor, updater) reads this value; do not duplicate a literal elsewhere.
"""

__version__ = "1.0.0"
