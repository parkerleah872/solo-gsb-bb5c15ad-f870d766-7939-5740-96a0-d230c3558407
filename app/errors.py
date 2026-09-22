"""Structured error types raised by the planning layer.

Every error carries a list of :class:`~app.schemas.Issue` objects, each
pointing at the offending service(s) and constraint so the client can fix
the input.
"""
from __future__ import annotations

from .schemas import Issue


class PlanningError(Exception):
    """Semantic planning error (dependency cycle, infeasibility, ...)."""

    error_code = "planning_failed"
    status_code = 422

    def __init__(self, message: str, issues: list[Issue]):
        super().__init__(message)
        self.message = message
        self.issues = issues
