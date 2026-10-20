"""Composing the layout of a parent map (region / "All") from the layouts of its sub-maps.

Every map keeps its own saved positions, all of them around the same origin, so simply merging them would pile the
devices of different sub-maps on top of each other. Instead each sub-map keeps its internal arrangement and the blocks are
placed side by side (wrapping into rows); devices that no sub-map positioned go into a grid underneath.
Pure functions over plain dicts, so they are easy to test; graph_data.py feeds them from the database."""

BLOCK_GAP = 160          # space between the blocks
ROW_WIDTH = 2800         # a row of blocks wraps after this width
GRID_COLUMNS = 8
GRID_STEP = (210, 100)   # distance between leftover devices in the grid


def _bounds(positions):
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    return min(xs), min(ys), max(xs), max(ys)


def tile_blocks(blocks, gap=BLOCK_GAP, row_width=ROW_WIDTH):
    """blocks = [{node: (x, y)}, ...] in the order they should be placed. Returns one merged {node: [x, y]} where each block
    keeps its internal arrangement and the blocks follow each other from left to right, wrapping into new rows."""
    merged = {}
    cursor_x = cursor_y = row_height = 0.0
    for block in blocks:
        if not block:
            continue
        x1, y1, x2, y2 = _bounds(block)
        width, height = x2 - x1, y2 - y1
        if cursor_x and cursor_x + width > row_width:           # wrap into a new row
            cursor_x, cursor_y, row_height = 0.0, cursor_y + row_height + gap, 0.0
        for node, (x, y) in block.items():
            merged[node] = [round(cursor_x + (x - x1), 1), round(cursor_y + (y - y1), 1)]
        cursor_x += width + gap
        row_height = max(row_height, height)
    return merged


def place_leftovers(positions, node_ids, columns=GRID_COLUMNS, step=GRID_STEP, gap=BLOCK_GAP):
    """Adds the nodes that have no position yet to a grid below the existing ones (devices first, then the clouds)."""
    missing = sorted((n for n in node_ids if n not in positions), key=lambda n: (not n.startswith('d'), n))
    if not missing:
        return positions
    top = (_bounds(positions)[3] + gap + step[1]) if positions else 0
    left = _bounds(positions)[0] if positions else 0
    for i, node in enumerate(missing):
        positions[node] = [round(left + (i % columns) * step[0], 1), round(top + (i // columns) * step[1], 1)]
    return positions


def compose(child_layouts, node_ids, leftovers=True):
    """child_layouts = [{node: (x, y)}, ...] (already in the order to place them). Only the nodes present in `node_ids` are
    used. Returns {} when no child has a layout to take over, otherwise the tiled layout - complete, with the nodes that
    nobody positioned in a grid below (`leftovers=False` leaves them out: used for the middle levels of a hierarchy)."""
    wanted = set(node_ids)
    blocks = [{n: p for n, p in layout.items() if n in wanted} for layout in child_layouts]
    blocks = [b for b in blocks if b]
    if not blocks:
        return {}
    merged = tile_blocks(blocks)
    return place_leftovers(merged, wanted) if leftovers else merged
