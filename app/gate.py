"""Execution gate for a stored release plan.

Given a stored (immutable) wave assignment and a progress report listing the
services that have completed and the ones that have failed, the gate decides,
**wave by wave following the stored plan**, which of the remaining services
can still be executed and which are blocked.

Classification of a not-yet-reported service ``s``:

* executable when every prerequisite is reported completed;
* blocked otherwise, with one localizable reason per bad prerequisite:
    - ``dependency_failed``      – a direct prerequisite is reported failed;
    - ``upstream_failure``       – a direct prerequisite is itself blocked by
      a failure further upstream (failed roots are listed in the context);
    - ``dependency_incomplete``  – a direct prerequisite is not completed and
      is not affected by any failure.

Failure propagation is computed over the transitive closure of "depends-on"
edges, so the whole downstream of a failed service is blocked recursively.
The stored plan is never modified.

The progress report itself is validated first: unknown services, duplicate
entries, a service reported both completed and failed, and a service reported
completed while one of its prerequisites is failed or still unfinished all
raise a structured :class:`~app.errors.PlanningError` (HTTP 422).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import PlanningError
from .graph import build_adjacency
from .schemas import GateRequest, Issue


@dataclass(frozen=True)
class BlockedItem:
    service: str
    wave: int  # 1-based wave in the stored plan
    reasons: list[Issue]


@dataclass(frozen=True)
class WaveGate:
    index: int
    executable: list[str]
    blocked: list[str]
    completed: list[str]
    failed: list[str]


@dataclass(frozen=True)
class GateOutput:
    current_wave: int | None
    executable: list[str]  # grouped by wave, then sorted by service id
    blocked: list[BlockedItem]
    completed: list[str]
    failed: list[str]
    waves: list[WaveGate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _duplicate_issues(field_name: str, values: list[str]) -> list[Issue]:
    seen: set[str] = set()
    issues: list[Issue] = []
    for s in values:
        if s in seen:
            issues.append(
                Issue(
                    code="duplicate_service_status",
                    message=f"Service '{s}' is listed more than once in {field_name}.",
                    constraint=field_name,
                    service=s,
                )
            )
            continue
        seen.add(s)
    return issues


def _validate_report(
    services: list[str],
    adj: dict[str, list[str]],
    payload: GateRequest,
) -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Validate the progress report.

    Return the completed set, the failed set and the memoized failure-root
    reachability table (service -> failed services on its prerequisite chain).
    """
    known = set(services)
    issues: list[Issue] = []

    # Duplicate entries within either list.
    issues += _duplicate_issues("completed_services", payload.completed_services)
    issues += _duplicate_issues("failed_services", payload.failed_services)

    completed = set(payload.completed_services)
    failed = set(payload.failed_services)

    # References to services that are not part of the plan.
    for s in sorted((completed | failed) - known):
        field_name = (
            "completed_services" if s in completed else "failed_services"
        )
        issues.append(
            Issue(
                code="unknown_service",
                message=(
                    f"Service '{s}' is not part of this plan and cannot be "
                    "reported in the gate request."
                ),
                constraint=field_name,
                service=s,
            )
        )

    # A service cannot be both completed and failed.
    for s in sorted(completed & failed):
        issues.append(
            Issue(
                code="service_status_conflict",
                message=(
                    f"Service '{s}' is reported both as completed and as "
                    "failed; its status is ambiguous."
                ),
                constraint="completed_services",
                service=s,
                related_services=[s],
            )
        )

    if issues:
        raise PlanningError(
            "The gate request references unknown services or reports "
            "inconsistent service states.",
            issues,
        )

    # Failure reachability is needed both for consistency validation below and
    # for blocking the remaining services (the stored graph is a DAG, so
    # memoized recursion terminates).
    roots_memo: dict[str, set[str]] = {}

    def failed_roots_of(service: str) -> set[str]:
        return {
            root
            for dep in adj[service]
            for root in _failure_roots(dep, adj, failed, roots_memo)
        }

    # Internal consistency of a "completed" report: no failed service may lie
    # anywhere on its prerequisite chain (the failure propagates transitively,
    # even through services not yet reported), and every direct prerequisite
    # must itself be completed — a plan never releases a service before its
    # dependencies, so such a report contradicts the stored scheme.
    for s in sorted(completed):
        failed_deps = sorted(failed_roots_of(s))
        if failed_deps:
            issues.append(
                Issue(
                    code="completed_with_failed_dependency",
                    message=(
                        f"Service '{s}' is reported completed, but its "
                        f"upstream dependency {_format_list(failed_deps)} "
                        "failed; it could not have been released."
                    ),
                    constraint="completed_services",
                    service=s,
                    related_services=failed_deps,
                    context={"failed_dependencies": failed_deps},
                )
            )
            continue
        unfinished_deps = sorted(d for d in adj[s] if d not in completed)
        if unfinished_deps:
            issues.append(
                Issue(
                    code="completed_before_dependencies",
                    message=(
                        f"Service '{s}' is reported completed, but its "
                        f"dependency {_format_list(unfinished_deps)} is not "
                        "completed yet."
                    ),
                    constraint="completed_services",
                    service=s,
                    related_services=unfinished_deps,
                    context={"unfinished_dependencies": unfinished_deps},
                )
            )

    if issues:
        raise PlanningError(
            "The reported progress contradicts the stored release order.",
            issues,
        )

    return completed, failed, roots_memo


def _format_list(values: list[str]) -> str:
    if len(values) == 1:
        return f"'{values[0]}'"
    head = ", ".join(f"'{v}'" for v in values[:-1])
    return f"{head} and '{values[-1]}'"


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------


def _failure_roots(
    service: str,
    adj: dict[str, list[str]],
    failed: set[str],
    memo: dict[str, set[str]],
) -> set[str]:
    """Failed services reachable through ``service``'s prerequisite chain."""
    if service in memo:
        return memo[service]
    roots: set[str] = set()
    if service in failed:
        roots.add(service)
    for dep in adj[service]:
        roots |= _failure_roots(dep, adj, failed, memo)
    memo[service] = roots
    return roots


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_gate(
    services: list[str],
    wave_of: dict[str, int],
    edges: list[tuple[str, str]],
    payload: GateRequest,
) -> GateOutput:
    """Evaluate which remaining services are executable or blocked.

    ``services`` lists every service of the stored plan, ``wave_of`` its
    1-based stored wave, ``edges`` the dependency edges as
    ``(service, depends_on)``. The stored plan is treated as immutable.
    """
    adj = build_adjacency(services, edges)
    completed, failed, roots_memo = _validate_report(services, adj, payload)

    # Transitive failure reachability, computed once during validation and
    # reused here: any remaining service with a failed prerequisite anywhere
    # on its chain is blocked recursively.
    tainted = {
        s
        for s in services
        if s not in failed and _failure_roots(s, adj, failed, roots_memo)
    }

    remaining = [s for s in services if s not in completed and s not in failed]
    executable: list[str] = []
    blocked_items: list[BlockedItem] = []
    by_wave_executable: dict[int, list[str]] = {}
    by_wave_blocked: dict[int, list[str]] = {}

    for s in remaining:
        reasons: list[Issue] = []
        wave = wave_of[s]

        # Direct prerequisite failures take precedence ...
        direct_failed = sorted(d for d in adj[s] if d in failed)
        for dep in direct_failed:
            reasons.append(
                Issue(
                    code="dependency_failed",
                    message=(
                        f"Service '{s}' is blocked because its dependency "
                        f"'{dep}' failed."
                    ),
                    constraint="dependencies",
                    service=s,
                    related_services=[dep],
                    context={
                        "dependency": dep,
                        "dependency_status": "failed",
                        "wave": wave,
                    },
                )
            )

        # ... then dependencies blocked by an upstream failure ...
        for dep in sorted(d for d in adj[s] if d in tainted and d not in failed):
            roots = sorted(roots_memo[dep])
            reasons.append(
                Issue(
                    code="upstream_failure",
                    message=(
                        f"Service '{s}' is blocked because its dependency "
                        f"'{dep}' is blocked by the failed upstream "
                        f"service{_plural(roots)} {_format_list(roots)}."
                    ),
                    constraint="dependencies",
                    service=s,
                    related_services=[dep] + roots,
                    context={
                        "dependency": dep,
                        "dependency_status": "blocked",
                        "failed_roots": roots,
                        "wave": wave,
                    },
                )
            )

        # ... then dependencies simply not completed yet.
        for dep in sorted(
            d
            for d in adj[s]
            if d not in failed and d not in tainted and d not in completed
        ):
            reasons.append(
                Issue(
                    code="dependency_incomplete",
                    message=(
                        f"Service '{s}' is blocked because its dependency "
                        f"'{dep}' is not completed yet."
                    ),
                    constraint="dependencies",
                    service=s,
                    related_services=[dep],
                    context={
                        "dependency": dep,
                        "dependency_status": "pending",
                        "wave": wave,
                    },
                )
            )

        if reasons:
            blocked_items.append(BlockedItem(service=s, wave=wave, reasons=reasons))
            by_wave_blocked.setdefault(wave, []).append(s)
        else:
            executable.append(s)
            by_wave_executable.setdefault(wave, []).append(s)

    # Group executable services by the stored waves, sort within each wave.
    executable = [
        s
        for wave in sorted(by_wave_executable)
        for s in sorted(by_wave_executable[wave])
    ]
    blocked_items.sort(key=lambda item: item.service)

    current_wave = min(by_wave_executable) if by_wave_executable else None

    completed_sorted = sorted(completed)
    failed_sorted = sorted(failed)

    by_wave_completed: dict[int, list[str]] = {}
    by_wave_failed: dict[int, list[str]] = {}
    for s in completed_sorted:
        by_wave_completed.setdefault(wave_of[s], []).append(s)
    for s in failed_sorted:
        by_wave_failed.setdefault(wave_of[s], []).append(s)

    waves = [
        WaveGate(
            index=wave,
            executable=sorted(by_wave_executable.get(wave, [])),
            blocked=sorted(by_wave_blocked.get(wave, [])),
            completed=sorted(by_wave_completed.get(wave, [])),
            failed=sorted(by_wave_failed.get(wave, [])),
        )
        for wave in sorted({wave_of[s] for s in services})
    ]

    return GateOutput(
        current_wave=current_wave,
        executable=executable,
        blocked=blocked_items,
        completed=completed_sorted,
        failed=failed_sorted,
        waves=waves,
    )


def _plural(values: list[str]) -> str:
    return "" if len(values) == 1 else "s"
