"""Errors raised while reading input files."""


class IngestionError(Exception):
    """The file could not be read (missing, empty, unsupported, corrupt...)."""
