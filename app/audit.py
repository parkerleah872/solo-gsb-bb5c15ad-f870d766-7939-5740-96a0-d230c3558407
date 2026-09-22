"""Actual execution-timeline audit for a stored release plan.

Given one reported execution window (``started_at`` / ``finished_at``, ISO
8601) for every service of a stored plan, the audit checks two ordering
guarantees implied by the stored waves and dependency DAG:

* **previous-wave barrier** — a service may only start once *every* service of
  the immediately preceding stored wave has finished (wave N waits for the
  whole of wave N-1);
* **direct dependencies** — a service may only start once every service it
  directly depends on has finished.

Each broken constraint is reported separately with the offending service, the
constraint object (the preceding wave or the prerequisite service), the
specific blocking service and the overlap in milliseconds: a service that
starts ``overlap_ms`` milliseconds before the constraint's deadline violated
that constraint by exactly that amount. Exact coincidence (start at the
deadline) is allowed.

When the request itself is well-formed — every plan service named exactly
once, no unknown services, no finished-before-start windows — the actual
per-service, per-wave and total durations are computed, together with the
**critical chain**: the dependency-DAG path with the largest sum of actual
service durations. Durations are integer milliseconds (sub-millisecond
fractions truncated). Malformed requests raise a structured
:class:`~app.errors.PlanningError` (HTTP 422). The stored plan is never
modified.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import PlanningError
from .schemas import (
    CriticalChain,
    Issue,
    ServiceTiming,
    TimelineAuditRequest,
    TimelineViolation,
    WaveTiming,
)


@dataclass(frozen=True)
class Window:
    started_at: datetime
    finished_at: datetime


def _as_utc(value: datetime) -> datetime:
    """Normalize an ISO 8601 instant to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ms_between(start: datetime, end: datetime) -> int:
    """Whole milliseconds from ``start`` to ``end`` (truncated toward zero)."""
    delta_ms = (end - start).total_seconds() * 1000
    return int(delta_ms)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_timelines(
    services: list[str], payload: TimelineAuditRequest
) -> dict[str, Window]:
    """Validate coverage and internal consistency of the reported windows."""
    known = set(services)
    issues: list[Issue] = []

    windows: dict[str, Window] = {}
    seen: set[str] = set()
    for entry in payload.service_timelines:
        if entry.service in seen:
            issues.append(
                Issue(
                    code="duplicate_service_timeline",
                    message=(
                        f"Service '{entry.service}' has more than one reported "
                        "execution window."
                    ),
                    constraint="service_timelines",
                    service=entry.service,
                )
            )
            continue
        seen.add(entry.service)
        started = _as_utc(entry.started_at)
        finished = _as_utc(entry.finished_at)
        windows[entry.service] = Window(started_at=started, finished_at=finished)
        if finished < started:
            issues.append(
                Issue(
                    code="timeline_inverted",
                    message=(
                        f"Service '{entry.service}' finished before it started "
                        f"({finished.isoformat()} < {started.isoformat()})."
                    ),
                    constraint="service_timelines",
                    service=entry.service,
                    context={
                        "started_at": started.isoformat(),
                        "finished_at": finished.isoformat(),
                    },
                )
            )

    for s in sorted(seen - known):
        issues.append(
            Issue(
                code="unknown_service",
                message=(
                    f"Service '{s}' is not part of this plan and cannot appear "
                    "in its timeline audit."
                ),
                constraint="service_timelines",
                service=s,
            )
        )

    for s in sorted(known - seen):
        issues.append(
            Issue(
                code="missing_service_timeline",
                message=(
                    f"Service '{s}' belongs to the plan but has no reported "
                    "started_at/finished_at window."
                ),
                constraint="service_timelines",
                service=s,
            )
        )

    if issues:
        raise PlanningError(
            "The timeline audit does not cover exactly the services of the "
            "plan or contains inverted time windows.",
            issues,
        )

    return windows


# ---------------------------------------------------------------------------
# Constraint violations
# ---------------------------------------------------------------------------


def _wave_violations(
    waves: list[list[str]], windows: dict[str, Window]
) -> list[TimelineViolation]:
    """Each service must start at or after the previous wave's last finish."""
    violations: list[TimelineViolation] = []
    for index in range(1, len(waves)):
        previous = waves[index - 1]
        constraint = f"wave {index}"
        # The wave barrier is the latest finish among the previous wave's
        # members; that member is the single blocking service to name. On
        # equal finish instants the lexicographically largest id is chosen
        # deterministically.
        blocker = max(previous, key=lambda s: (windows[s].finished_at, s))
        deadline = windows[blocker].finished_at
        for service in waves[index]:
            started = windows[service].started_at
            if started < deadline:
                violations.append(
                    TimelineViolation(
                        service=service,
                        constraint_type="previous_wave",
                        constraint=constraint,
                        blocking_service=blocker,
                        overlap_ms=_ms_between(started, deadline),
                    )
                )
    return violations


def _dependency_violations(
    edges: list[tuple[str, str]], windows: dict[str, Window]
) -> list[TimelineViolation]:
    """Each service must start at or after every direct dependency's finish."""
    violations: list[TimelineViolation] = []
    for service, depends_on in edges:
        started = windows[service].started_at
        deadline = windows[depends_on].finished_at
        if started < deadline:
            violations.append(
                TimelineViolation(
                    service=service,
                    constraint_type="dependency",
                    constraint=depends_on,
                    blocking_service=depends_on,
                    overlap_ms=_ms_between(started, deadline),
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Durations and the weighted critical chain
# ---------------------------------------------------------------------------


def _critical_chain(
    services: list[str],
    edges: list[tuple[str, str]],
    durations_ms: dict[str, int],
) -> CriticalChain:
    """Longest path weighted by actual per-service duration.

    The edge direction is ``service -> depends_on``; a chain is reported
    prerequisite-first. Node weight equals the service duration, so the
    chain's total is the summed service time along it. Ties resolve to the
    lexicographically smallest prerequisite-first chain, keeping the result
    deterministic. The stored graph is a DAG, so memoized recursion ends.
    """
    adj: dict[str, list[str]] = {s: [] for s in services}
    for service, depends_on in edges:
        adj[service].append(depends_on)

    # Memoized chains are already prerequisite-first: a chain through ``node``
    # is a prerequisite chain of one of its dependencies followed by ``node``,
    # so the lexicographic tie-break composes greedily from the best sub-chain.
    memo_len: dict[str, int] = {}
    memo_chain: dict[str, list[str]] = {}

    def best(node: str) -> tuple[int, list[str]]:
        if node in memo_len:
            return memo_len[node], memo_chain[node]
        best_len = durations_ms[node]
        best_chain = [node]
        for dep in sorted(adj[node]):
            sub_len, sub_chain = best(dep)
            candidate_len = durations_ms[node] + sub_len
            candidate_chain = sub_chain + [node]
            if candidate_len > best_len or (
                candidate_len == best_len and candidate_chain < best_chain
            ):
                best_len = candidate_len
                best_chain = candidate_chain
        memo_len[node] = best_len
        memo_chain[node] = best_chain
        return best_len, best_chain

    overall_len = -1
    overall_chain: list[str] = []
    for s in sorted(services):
        length, chain = best(s)
        if length > overall_len or (length == overall_len and chain < overall_chain):
            overall_len = length
            overall_chain = chain
    return CriticalChain(services=overall_chain, duration_ms=overall_len)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineAuditOutput:
    valid: bool
    violations: list[TimelineViolation]
    service_timings: dict[str, ServiceTiming]
    waves: list[WaveTiming]
    critical_chain: CriticalChain
    total_duration_ms: int


def audit_timeline(
    services: list[str],
    wave_members: list[list[str]],
    edges: list[tuple[str, str]],
    payload: TimelineAuditRequest,
) -> TimelineAuditOutput:
    """Audit reported execution windows against the stored waves and DAG.

    ``services`` lists every service of the stored plan (sorted),
    ``wave_members`` the stored waves in order and ``edges`` the dependency
    edges as ``(service, depends_on)``. Nothing is persisted by the caller.
    """
    windows = _validate_timelines(services, payload)

    violations = _wave_violations(wave_members, windows) + _dependency_violations(
        edges, windows
    )
    # Deterministic order: offending service, constraint type (dependency
    # before previous_wave), then the constraint object.
    violations.sort(
        key=lambda v: (v.service, v.constraint_type, v.constraint)
    )

    service_timings = {
        s: ServiceTiming(
            started_at=windows[s].started_at,
            finished_at=windows[s].finished_at,
            duration_ms=_ms_between(windows[s].started_at, windows[s].finished_at),
        )
        for s in services
    }
    durations_ms = {s: service_timings[s].duration_ms for s in services}

    wave_timings = [
        WaveTiming(
            index=index + 1,
            started_at=min(windows[s].started_at for s in members),
            finished_at=max(windows[s].finished_at for s in members),
            duration_ms=_ms_between(
                min(windows[s].started_at for s in members),
                max(windows[s].finished_at for s in members),
            ),
            services=sorted(members),
        )
        for index, members in enumerate(wave_members)
    ]

    critical_chain = _critical_chain(services, edges, durations_ms)

    earliest = min(w.started_at for w in windows.values())
    latest = max(w.finished_at for w in windows.values())
    total_duration_ms = _ms_between(earliest, latest)

    return TimelineAuditOutput(
        valid=not violations,
        violations=violations,
        service_timings=service_timings,
        waves=wave_timings,
        critical_chain=critical_chain,
        total_duration_ms=total_duration_ms,
    )
