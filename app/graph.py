"""Dependency-graph helpers: input validation, cycle detection, longest path.

Edges mean "service -> service it depends on" (dependency must go first).
"""
from __future__ import annotations

from collections import defaultdict


def build_adjacency(
    services: list[str], edges: list[tuple[str, str]]
) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {s: [] for s in services}
    for service, depends_on in edges:
        adj[service].append(depends_on)
    return adj


def find_cycles(
    services: list[str], adj: dict[str, list[str]], max_cycles: int = 20
) -> list[list[str]]:
    """Return up to ``max_cycles`` simple cycles in the dependency graph.

    Uses Tarjan's SCC to locate cyclic components, then a DFS within each
    component to extract a concrete simple cycle that can be reported to
    the client (node names only; the implicit closing edge repeats the
    first node).
    """
    index_counter = 0
    stack: list[str] = []
    on_stack: dict[str, bool] = defaultdict(bool)
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    import sys

    sys.setrecursionlimit(max(10000, len(services) * 4 + 1000))

    def strongconnect(v: str) -> None:
        nonlocal index_counter
        indices[v] = index_counter
        lowlink[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack[v] = True

        for w in adj.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack[w]:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == v:
                    break
            sccs.append(component)

    for s in services:
        if s not in indices:
            strongconnect(s)

    cycles: list[list[str]] = []
    seen_signatures: set[tuple[str, ...]] = set()

    for component in sccs:
        members = set(component)
        # Self-loop is the trivial one-node cycle.
        if len(component) == 1:
            node = component[0]
            if node in adj.get(node, []):
                sig = (node,)
                if sig not in seen_signatures:
                    seen_signatures.add(sig)
                    cycles.append([node])
            continue

        for start in sorted(component):
            if len(cycles) >= max_cycles:
                return cycles
            # DFS from start until we can reach start again.
            path: list[str] = []
            visited: set[str] = set()

            def dfs(node: str) -> bool:
                path.append(node)
                visited.add(node)
                for nxt in sorted(adj.get(node, [])):
                    if nxt not in members:
                        continue
                    if nxt == start and len(path) >= 1:
                        return True
                    if nxt not in visited:
                        if dfs(nxt):
                            return True
                path.pop()
                return False

            if dfs(start):
                sig = tuple(sorted(path))
                if sig not in seen_signatures:
                    seen_signatures.add(sig)
                    cycles.append(list(path))

    return cycles


def longest_path_length(
    services: list[str], edges: list[tuple[str, str]]
) -> tuple[int, list[str]]:
    """Longest dependency chain (node count) and one chain attaining it.

    Assumes the graph is acyclic. Runs on the graph ``service -> deps``,
    so depth counts nodes along a chain of prerequisites.
    """
    adj: dict[str, list[str]] = {s: [] for s in services}
    for u, v in edges:
        adj[u].append(v)

    memo_len: dict[str, int] = {}
    memo_chain: dict[str, list[str]] = {}

    def depth(node: str) -> tuple[int, list[str]]:
        if node in memo_len:
            return memo_len[node], memo_chain[node]
        best_len = 1
        best_chain = [node]
        for nxt in sorted(adj.get(node, [])):
            sub_len, sub_chain = depth(nxt)
            if sub_len + 1 > best_len:
                best_len = sub_len + 1
                best_chain = [node] + sub_chain
        memo_len[node] = best_len
        memo_chain[node] = best_chain
        return best_len, best_chain

    overall_len = 0
    overall_chain: list[str] = []
    for s in sorted(services):
        length, chain = depth(s)
        if length > overall_len:
            overall_len = length
            overall_chain = chain
    return overall_len, overall_chain
