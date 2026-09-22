"""Runtime configuration loaded from environment variables.

All settings have sensible defaults so the service starts with zero
configuration; the Docker image only overrides the database path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    # SQLite database file (directory is created on startup).
    database_path: str
    # Per-solve wall-clock budget (seconds). CP-SAT has two solve phases.
    solver_time_limit_seconds: float
    # Search workers. 1 + fixed seed => deterministic plans.
    solver_num_search_workers: int
    # Cap on how many cycles are reported in one error response.
    max_reported_cycles: int = 20

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"


def load_settings() -> Settings:
    return Settings(
        database_path=os.getenv("PLANNER_DB_PATH", "/data/plans.db"),
        solver_time_limit_seconds=_get_float("PLANNER_SOLVER_TIME_LIMIT", 10.0),
        solver_num_search_workers=_get_int("PLANNER_SOLVER_WORKERS", 1),
    )
