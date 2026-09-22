"""Actual execution-timeline audit for a stored release plan.

Given one ``(started_at, finished_at)`` pair per service of a stored plan,
the audit verifies the reported timeline against two kinds of ordering
constraints:

* **previous wave barrier** — a service must not start before *every* service
  of the previous stored wave has finished;
* **direct dependency** — a service must not start before any service it
  directly depends on has finished.

Each violation names the offending service, the constraint object (the
previous wave index plus the still-running members, or the unfinished
dependency) and the positive overlap in milliseconds by which the service
started too early.

For a structurally valid report (exactly one known, non-inverted entry per
service) the audit also computes the actual wall-clock duration of each
stored wave and the **critical chain**: the dependency-DAG path whose
services' summed durations are the longest, weighted by the reported
per-service durations.

Timestamps without an explicit UTC offset are interpreted as UTC; all
timestamps in the response are UTC-normalized. The stored plan is never
modified: the operation is read-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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

_MS_PER_DAY = 86_400_000
_MS_PER_SECOND = 1_000


@dataclass(frozen=True)
class _Violation:
    service: str
    service_wave: int
    constraint: str  # "previous_wave" | "dependencies"
    started_at: datetime
    required_after: datetime
    overlap_ms: int
    constraint_wave: int | None = None
    constraint_service: str | None = None
    blocking_services: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimelineAuditOutput:
    compliant: bool
    violations: list[TimelineViolation]
    service_timings: list[ServiceTiming]
    wave_timings: list[WaveTiming]
    started_at: datetime
    finished_at: datetime
    total_duration_ms: int
    critical_chain: CriticalChain = field(default_factory=lambda: CriticalChain(
        services=[], total_duration_ms=0
    ))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Interpret naive timestamps as UTC; convert aware ones to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _delta_ms(later: datetime, earlier: datetime) -> int:
    """Whole milliseconds of ``later - earlier`` (truncated toward zero)."""
    delta = later - earlier
    return (
        delta.days * _MS_PER_DAY
        + delta.seconds * _MS_PER_SECOND
        + delta.microseconds // _MS_PER_SECOND
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_entries(
    services: list[str],
    payload: TimelineAuditRequest,
) -> dict[str, tuple[datetime, datetime]]:
    """Return ``service -> (started_at, finished_at)`` in UTC, or raise 422.

    Rejects duplicate entries, unknown services, missing services and
    inverted (finished-before-start) execution windows.
    """
    known = set(services)
    issues: list[Issue] = []

    timings: dict[str, tuple[datetime, datetime]] = {}
    reported: set[str] = set()
    for position, entry in enumerate(payload.services):
        reported.add(entry.service)
        if entry.service in timings:
            issues.append(
                Issue(
                    code="duplicate_timeline_service",
                    message=(
                        f"Service '{entry.service}' has more than one timeline "
                        "entry; each service must appear exactly once."
                    ),
                    constraint="services",
                    service=entry.service,
                    context={"position": position},
                )
            )
            continue
        started = _as_utc(entry.started_at)
        finished = _as_utc(entry.finished_at)
        if finished < started:
            issues.append(
                Issue(
                    code="timeline_service_time_inverted",
                    message=(
                        f"Service '{entry.service}' has finished_at "
                        f"({finished.isoformat()}) earlier than started_at "
                        f"({started.isoformat()})."
                    ),
                    constraint="services",
                    service=entry.service,
                    context={
                        "started_at": started.isoformat(),
                        "finished_at": finished.isoformat(),
                    },
                )
            )
            continue
        timings[entry.service] = (started, finished)

    for unknown in sorted(set(timings) - known):
        issues.append(
            Issue(
                code="unknown_service",
                message=(
                    f"Service '{unknown}' is not part of this plan and cannot "
                    "appear in its timeline audit."
                ),
                constraint="services",
                service=unknown,
            )
        )

    for missing in sorted(known - reported):
        issues.append(
            Issue(
                code="missing_timeline_service",
                message=(
                    f"Service '{missing}' is part of this plan but has no "
                    "timeline entry; every service must report started_at "
                    "and finished_at exactly once."
                ),
                constraint="services",
                service=missing,
            )
        )

    if issues:
        raise PlanningError(
            "The timeline audit references unknown, duplicate or missing "
            "services or reports an inverted execution window.",
            issues,
        )

    return timings


# ---------------------------------------------------------------------------
# Constraint violations
# ---------------------------------------------------------------------------


def _detect_violations(
    services: list[str],
    wave_of: dict[str, int],
    adj: dict[str, list[str]],
    wave_members: dict[int, list[str]],
    timings: dict[str, tuple[datetime, datetime]],
) -> list[_Violation]:
    violations: list[_Violation] = []

    for service in sorted(services, key=lambda s: (wave_of[s], s)):
        started, _finished = timings[service]
        wave = wave_of[service]

        # Barrier 1: every service of the immediately previous wave must be
        # finished before this service starts.
        if wave > 1:
            previous_wave = wave - 1
            prev_members = sorted(wave_members[previous_wave])
            required_after = max(timings[m][1] for m in prev_members)
            if started < required_after:
                blocking = sorted(
                    m for m in prev_members if timings[m][1] > started
                )
                violations.append(
                    _Violation(
                        service=service,
                        service_wave=wave,
                        constraint="previous_wave",
                        constraint_wave=previous_wave,
                        blocking_services=tuple(blocking),
                        started_at=started,
                        required_after=required_after,
                        overlap_ms=_delta_ms(required_after, started),
                    )
                )

        # Barrier 2: every direct dependency must be finished first.
        for dependency in sorted(adj[service]):
            required_after = timings[dependency][1]
            if started < required_after:
                violations.append(
                    _Violation(
                        service=service,
                        service_wave=wave,
                        constraint="dependencies",
                        constraint_service=dependency,
                        started_at=started,
                        required_after=required_after,
                        overlap_ms=_delta_ms(required_after, started),
                    )
                )

    violations.sort(
        key=lambda v: (
            v.service_wave,
            v.service,
            0 if v.constraint == "previous_wave" else 1,
            v.constraint_wave or 0,
            v.constraint_service or "",
        )
    )
    return violations


# ---------------------------------------------------------------------------
# Critical chain on the weighted dependency DAG
# ---------------------------------------------------------------------------


def _choose_candidate(
    current: tuple[int, list[str]] | None,
    candidate: tuple[int, list[str]],
) -> tuple[int, list[str]]:
    """Pick the candidate with greatest weight, then most nodes, then the
    lexicographically smallest prerequisite-first chain."""
    if current is None:
        return candidate
    cur_len, cur_chain = current
    new_len, new_chain = candidate
    if new_len > cur_len:
        return candidate
    if new_len < cur_len:
        return current
    if len(new_chain) > len(cur_chain):
        return candidate
    if len(new_chain) < len(cur_chain):
        return current
    return candidate if new_chain < cur_chain else current


def _critical_chain(
    services: list[str],
    adj: dict[str, list[str]],
    duration_ms: dict[str, int],
) -> CriticalChain:
    """Longest dependency path weighted by per-service duration.

    Edge direction is ``service -> depends_on``. Chains are emitted
    prerequisite-first (dependencies first, dependent last). Ties are broken
    deterministically: greatest weighted length, then greatest node count,
    then lexicographically smallest prerequisite-first chain.
    """
    best_length: dict[str, int] = {}
    best_chain: dict[str, list[str]] = {}

    def visit(node: str) -> tuple[int, list[str]]:
        if node in best_length:
            return best_length[node], best_chain[node]

        chosen: tuple[int, list[str]] | None = None
        for dependency in adj[node]:
            dep_length, dep_chain = visit(dependency)
            chosen = _choose_candidate(chosen, (dep_length, dep_chain))

        if chosen is None:
            chosen_length, chosen_chain = duration_ms[node], [node]
        else:
            chosen_length, chosen_chain = (
                duration_ms[node] + chosen[0],
                chosen[1] + [node],
            )

        best_length[node] = chosen_length
        best_chain[node] = chosen_chain
        return chosen_length, chosen_chain

    overall: tuple[int, list[str]] | None = None
    for service in sorted(services):
        overall = _choose_candidate(overall, visit(service))
    assert overall is not None
    return CriticalChain(
        services=overall[1], total_duration_ms=overall[0]
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def audit_timeline(
    services: list[str],
    wave_of: dict[str, int],
    edges: list[tuple[str, str]],
    payload: TimelineAuditRequest,
) -> TimelineAuditOutput:
    """Audit one reported execution timeline against the stored plan.

    ``services`` lists every service of the stored plan, ``wave_of`` its
    1-based stored wave and ``edges`` the dependency edges as
    ``(service, depends_on)``. The stored plan is treated as immutable.
    """
    timings = _validate_entries(services, payload)

    adj: dict[str, list[str]] = {s: [] for s in services}
    for service, depends_on in edges:
        adj[service].append(depends_on)

    wave_members: dict[int, list[str]] = {}
    for service in services:
        wave_members.setdefault(wave_of[service], []).append(service)

    violations = _detect_violations(
        services, wave_of, adj, wave_members, timings
    )

    duration_ms = {
        service: _delta_ms(timings[service][1], timings[service][0])
        for service in services
    }
    service_timings = [
        ServiceTiming(
            service=service,
            wave=wave_of[service],
            started_at=timings[service][0],
            finished_at=timings[service][1],
            duration_ms=duration_ms[service],
        )
        for service in sorted(services)
    ]

    wave_timings: list[WaveTiming] = []
    for wave in sorted(wave_members):
        members = sorted(wave_members[wave])
        started = min(timings[s][0] for s in members)
        finished = max(timings[s][1] for s in members)
        wave_timings.append(
            WaveTiming(
                index=wave,
                services=members,
                started_at=started,
                finished_at=finished,
                duration_ms=_delta_ms(finished, started),
            )
        )

    global_started = min(timings[s][0] for s in services)
    global_finished = max(timings[s][1] for s in services)

    return TimelineAuditOutput(
        compliant=not violations,
        violations=[
            TimelineViolation(
                service=v.service,
                service_wave=v.service_wave,
                constraint=v.constraint,  # type: ignore[arg-type]
                constraint_wave=v.constraint_wave,
                constraint_service=v.constraint_service,
                blocking_services=list(v.blocking_services),
                started_at=v.started_at,
                required_after=v.required_after,
                overlap_ms=v.overlap_ms,
            )
            for v in violations
        ],
        service_timings=service_timings,
        wave_timings=wave_timings,
        started_at=global_started,
        finished_at=global_finished,
        total_duration_ms=_delta_ms(global_finished, global_started),
        critical_chain=_critical_chain(services, adj, duration_ms),
    )
