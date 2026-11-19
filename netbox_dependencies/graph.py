"""Pure algorithm for the impact of an outage — no dependency on Django.

Edge = (upstream_id, downstream_id).

Rule: a device is DOWN as soon as any of its upstream devices is DOWN (it needs all of them),
and the outage propagates further down.
"""
from collections import defaultdict, deque
from dataclasses import dataclass

DOWN = 'down'


@dataclass
class Impact:
    state: str
    depth: int


def would_create_cycle(edges, upstream, downstream):
    """True if an upstream -> downstream edge would create a cycle
    (upstream is already reachable downwards from downstream)."""
    if upstream == downstream:
        return True
    children = defaultdict(list)
    for up, down in edges:
        children[up].append(down)
    seen = {downstream}
    queue = deque([downstream])
    while queue:
        node = queue.popleft()
        for child in children[node]:
            if child == upstream:
                return True
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return False


def compute_impact(edges, failed):
    """Returns {device_id: Impact} for devices affected by the outage of `failed`.

    The `failed` devices themselves are not in the result. `depth` = number of edges
    from the nearest failed device."""
    failed = set(failed)
    children = defaultdict(set)
    for up, down in edges:
        children[up].add(down)

    result = {}
    depth = {node: 0 for node in failed}
    queue = deque(failed)
    while queue:
        parent = queue.popleft()
        for child in children[parent]:
            if child in failed or child in result:
                continue
            depth[child] = depth[parent] + 1
            result[child] = Impact(DOWN, depth[child])
            queue.append(child)
    return result
