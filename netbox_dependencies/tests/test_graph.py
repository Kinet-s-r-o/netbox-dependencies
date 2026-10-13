"""Tests of the pure graph algorithm (no Django): python -m unittest."""
import unittest

from netbox_dependencies.graph import DEGRADED, DOWN, compute_impact, would_create_cycle

# core(1) -> sw1(2) -> ap(4)
#         -> sw2(3) -> ap(4)  (redundant uplinks "up")
#            sw1(2) -> srv(5)
EDGES = [
    (1, 2, ''),
    (1, 3, ''),
    (2, 4, 'up'),
    (3, 4, 'up'),
    (2, 5, ''),
]


class ComputeImpactTests(unittest.TestCase):
    def test_leaf_failure_affects_nobody(self):
        self.assertEqual(compute_impact(EDGES, {5}), {})

    def test_tree_failure_propagates_transitively(self):
        result = compute_impact([(1, 2, ''), (2, 3, ''), (3, 4, '')], {1})
        self.assertEqual({k: (v.state, v.depth) for k, v in result.items()},
                         {2: (DOWN, 1), 3: (DOWN, 2), 4: (DOWN, 3)})

    def test_redundant_group_degrades_when_one_parent_down(self):
        result = compute_impact(EDGES, {2})
        self.assertEqual(result[5].state, DOWN)
        self.assertEqual(result[4].state, DEGRADED)
        self.assertNotIn(3, result)

    def test_redundant_group_down_when_all_parents_down(self):
        result = compute_impact(EDGES, {2, 3})
        self.assertEqual(result[4].state, DOWN)

    def test_core_failure_takes_everything_down(self):
        result = compute_impact(EDGES, {1})
        self.assertEqual({k: v.state for k, v in result.items()},
                         {2: DOWN, 3: DOWN, 4: DOWN, 5: DOWN})
        self.assertEqual(result[4].depth, 2)

    def test_degraded_upgraded_to_down_keeps_correct_state(self):
        # srv(9) depends on both A(1) and B(2) in one group, B depends on A
        edges = [(1, 2, ''), (1, 9, 'g'), (2, 9, 'g')]
        self.assertEqual(compute_impact(edges, {1})[9].state, DOWN)

    def test_different_groups_are_independent(self):
        # downstream 3 needs both 1 (power) and 2 (network) - both are mandatory
        edges = [(1, 3, 'power'), (2, 3, 'net')]
        self.assertEqual(compute_impact(edges, {1})[3].state, DOWN)

    def test_ungrouped_edges_are_not_redundant(self):
        edges = [(1, 3, ''), (2, 3, '')]
        self.assertEqual(compute_impact(edges, {1})[3].state, DOWN)


class CycleTests(unittest.TestCase):
    def test_self_loop(self):
        self.assertTrue(would_create_cycle([], 1, 1))

    def test_indirect_cycle(self):
        # 1 -> 2 -> 3 exists; an edge 3 -> 1 would create a cycle
        self.assertTrue(would_create_cycle([(1, 2, ''), (2, 3, '')], 3, 1))

    def test_no_cycle(self):
        self.assertFalse(would_create_cycle([(1, 2, ''), (2, 3, '')], 1, 3))


if __name__ == '__main__':
    unittest.main()
