"""Rollback-wave preview for a stored release plan.

Given the subset of a stored plan's services that are currently live, the
preview computes the minimum number of ordered rollback waves for exactly
those services. A service may only be rolled back after **every live service
that depends on it**, so the rollback order is the release order reversed:
on the induced subgraph of live services the edge ``service -> depends_on``
is reversed and the standard wave model (per-wave concurrency cap and
fault-domain anti-affinity, both inherited unchanged from the stored plan)
is solved as usual.

The live set must be dependency-closed: if a live service has a prerequisite
that is *not* live, the set cannot have been produced by following the stored
plan, and a structured 422 is returned that names the live service together
with each missing prerequisite, so the client can locate the gap. Unknown
service references and duplicate entries are rejected the same way.

The stored plan is never modified: the operation is read-only.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .errors import PlanningError
from .planner import schedule_waves
from .schemas import Issue, RollbackPreviewRequest


@dataclass(frozen=True)
class RollbackOutput:
    live: list[str]  # deployed services, sorted and de-duplicated
    waves: list[list[str]]  # services of each rollback wave, sorted
    wave_count: int
    optimal: bool


def _validate_live_set(
    services: list[str],
    adj: dict[str, list[str]],
    payload: RollbackPreviewRequest,
) -> list[str]:
    """Validate the reported live set and return its sorted, de-duplicated form.

    Rejects duplicates, unknown services and violations of dependency
    closure (a live service whose prerequisite is not live).
    """
    issues: list[Issue] = []

    seen: set[str] = set()
    for s in payload.deployed_services:
        if s in seen:
            issues.append(
                Issue(
                    code="duplicate_service",
                    message=f"Service '{s}' is listed more than once as deployed.",
                    constraint="deployed_services",
                    service=s,
                )
            )
            continue
        seen.add(s)

    known = set(services)
    for s in sorted(seen - known):
        issues.append(
            Issue(
                code="unknown_service",
                message=(
                    f"Service '{s}' is not part of this plan and cannot be "
                    "reported as deployed."
                ),
                constraint="deployed_services",
                service=s,
            )
        )

    if issues:
        raise PlanningError(
            "The rollback request references unknown or duplicate services.",
            issues,
        )

    live = sorted(seen)
    closure_issues: list[Issue] = []
    for s in live:
        missing = sorted(dep for dep in adj[s] if dep not in seen)
        if missing:
            closure_issues.append(
                Issue(
                    code="rollback_prerequisite_not_deployed",
                    message=(
                        f"Deployed service '{s}' requires prerequisite "
                        f"service(s) {_format_list(missing)} to also be "
                        "deployed, but they are not reported as live; the "
                        "deployed set is not dependency-closed."
                    ),
                    constraint="deployed_services",
                    service=s,
                    related_services=missing,
                    context={"missing_prerequisites": missing},
                )
            )

    if closure_issues:
        raise PlanningError(
            "The deployed services violate dependency closure: rollback "
            "waves can only be generated when every prerequisite of a "
            "deployed service is deployed as well.",
            closure_issues,
        )

    return live


def _format_list(values: list[str]) -> str:
    if len(values) == 1:
        return f"'{values[0]}'"
    head = ", ".join(f"'{v}'" for v in values[:-1])
    return f"{head} and '{values[-1]}'"


def preview_rollback(
    services: list[str],
    edges: list[tuple[str, str]],
    domains: list[dict],
    concurrency: int,
    payload: RollbackPreviewRequest,
    settings: Settings,
) -> RollbackOutput:
    """Compute minimum, deterministic rollback waves for the live subset.

    ``services`` lists every service of the stored plan (sorted), ``edges``
    its dependency edges as ``(service, depends_on)`` and ``domains`` the
    stored fault domains ``[{"domain", "services"}]``. Nothing is persisted.
    """
    adj = {s: [] for s in services}
    for service, depends_on in edges:
        adj[service].append(depends_on)

    live = _validate_live_set(services, adj, payload)
    live_set = set(live)

    # Rollback reverses the release order: a service must roll back strictly
    # after every live service that depends on it, i.e. edge
    # depends_on -> service on the induced live subgraph.
    rollback_edges = [
        (dep, service)
        for service, dep in edges
        if service in live_set and dep in live_set
    ]

    # Anti-affinity inherited from the stored plan, restricted to live members.
    fault_cliques = [
        sorted(m for m in fd["services"] if m in live_set)
        for fd in domains
    ]
    fault_cliques = [clique for clique in fault_cliques if len(clique) >= 2]

    output = schedule_waves(
        live, rollback_edges, fault_cliques, concurrency, settings
    )
    return RollbackOutput(
        live=live,
        waves=output.waves,
        wave_count=output.wave_count,
        optimal=output.optimal,
    )
