"""Analyzer registry."""

from .base import find_by_type, find_duplicates, find_junk_dirs, find_large_old

__all__ = [
    "find_by_type",
    "find_duplicates",
    "find_junk_dirs",
    "find_large_old",
]
