"""Pure algorithm for the impact of an outage — no dependency on Django.

Hrana = (upstream_id, downstream_id, redundancy_group).

Rules:
* A device without redundancy (empty group): every upstream device is a group of its own,
  so the outage of a single upstream device takes the device out (DOWN).
* Edges with the same `redundancy_group` for the same downstream device form
  one group (e.g. 2 uplinks). A group is out only when ALL of its upstream devices are DOWN.
  If only some are DOWN, the downstream device is DEGRADED.
* A device is DOWN when at least one group is out. A DEGRADED device does not
  propagate the outage further (it still works).
"""
from collections import defaultdict, deque
from dataclasses import dataclass

DOWN = 'down'
DEGRADED = 'degraded'


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
    for up, down, _group in edges:
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
    groups = defaultdict(lambda: defaultdict(set))  # node -> group key -> parents
    for up, down, group in edges:
        children[up].add(down)
        groups[down][group or f'#{up}'].add(up)

    down_nodes = set(failed)
    depth = {node: 0 for node in failed}
    result = {}
    queue = deque(failed)

    while queue:
        parent = queue.popleft()
        for child in children[parent]:
            if child in failed:
                continue
            node_groups = groups[child]
            group_down = [all(p in down_nodes for p in members) for members in node_groups.values()]
            if any(group_down):
                if child not in down_nodes:
                    down_nodes.add(child)
                    depth[child] = depth[parent] + 1
                    result[child] = Impact(DOWN, depth[child])
                    queue.append(child)
            elif child not in result:
                result[child] = Impact(DEGRADED, depth[parent] + 1)
    return result
