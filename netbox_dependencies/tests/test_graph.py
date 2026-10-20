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


class LayoutTests(unittest.TestCase):
    def test_blocks_keep_their_arrangement_and_do_not_overlap(self):
        from netbox_dependencies.layout import tile_blocks
        a = {'d1': (10, 10), 'd2': (110, 10), 'd3': (60, 90)}
        b = {'d4': (500, 500), 'd5': (560, 560)}
        merged = tile_blocks([a, b], gap=100)
        self.assertEqual(merged['d1'], [0, 0])                       # the first block starts at the origin
        self.assertEqual((merged['d2'][0] - merged['d1'][0], merged['d3'][1] - merged['d1'][1]), (100, 80))   # same shape
        self.assertEqual((merged['d5'][0] - merged['d4'][0], merged['d5'][1] - merged['d4'][1]), (60, 60))
        self.assertGreaterEqual(merged['d4'][0], max(merged['d1'][0], merged['d2'][0], merged['d3'][0]) + 100)   # to the right of block a

    def test_blocks_wrap_into_a_new_row(self):
        from netbox_dependencies.layout import tile_blocks
        blocks = [{f'd{i}': (0, 0), f'e{i}': (400, 50)} for i in range(4)]
        merged = tile_blocks(blocks, gap=100, row_width=1000)
        self.assertEqual(merged['d0'][1], merged['d1'][1])           # two blocks fit into the first row
        self.assertGreater(merged['d2'][1], merged['d1'][1] + 50)    # the third one starts a new row

    def test_unpositioned_nodes_go_below_and_foreign_nodes_are_ignored(self):
        from netbox_dependencies.layout import compose
        result = compose([{'d1': (0, 0), 'd9': (50, 50)}], ['d1', 'd2', 'x3'])      # d9 is not on this map
        self.assertNotIn('d9', result)
        self.assertEqual(result['d1'], [0, 0])
        self.assertGreater(result['d2'][1], 0)                       # devices first, then the clouds, in a grid below
        self.assertEqual(compose([{'d9': (1, 1)}], ['d1']), {})      # nothing to take over


if __name__ == '__main__':
    unittest.main()
