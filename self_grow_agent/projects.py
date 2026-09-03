"""Validation shared by the logical project grouping features."""

from __future__ import annotations

import re

DEFAULT_PROJECT = "default"
_PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}")


def normalize_project(project: str) -> str:
    """Return a stable, URL-safe project identifier or raise ``ValueError``."""

    if not isinstance(project, str) or not project or project != project.strip():
        raise ValueError("project must be a non-empty string without outer whitespace")
    normalized = project.lower()
    if not _PROJECT_PATTERN.fullmatch(normalized):
        raise ValueError(
            "project must start with a letter and contain only lowercase letters, digits, or hyphens"
        )
    return normalized
