"""Unit tests for the Part 2 rules (task2.py). All limits are inclusive, so each
rule is tested exactly at its limit (passes) and just past it (fails).

Run from the repository root:  pytest -q
"""
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "task1-3"))
import task2  # pyright: ignore[reportMissingImports]  (path added to sys.path above)

EPS = 0.5  # mm: "just past the limit"


# ---------- tiny floors in floor.json format ----------

def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def floor(rooms, doors=(), columns=(), obstacles=(), comms=()):
    walls, seen = [], set()                       # one wall per room side, shared sides once
    for _, pts in rooms:
        for i in range(4):
            a, b = pts[i], pts[(i + 1) % 4]
            key = tuple(sorted([tuple(a), tuple(b)]))
            if key not in seen:
                seen.add(key)
                walls.append({"id": f"wall_{len(walls) + 1}", "start": a, "end": b})
    return {"walls": walls,
            "rooms": [{"id": r, "name": r, "points": p} for r, p in rooms],
            "doors": list(doors),
            "columns": [{"id": f"column_{i}", "points": p} for i, p in enumerate(columns)],
            "obstacles": [{"id": f"obstacle_{i}", "kind": "plant", "points": p} for i, p in enumerate(obstacles)],
            "comms_rooms": [{"id": f"comms_{i + 1}", "points": p} for i, p in enumerate(comms)],
            "aps_reference": [], "adjacency": []}


def ap(i, x, y, typ="ceiling", **extra):
    return {"id": f"ap_{i}", "position": [x, y], "type": typ, **extra}


# ---------- spacing: at least 6 m ----------

def test_spacing_limit():
    assert task2.check_ap_spacing([ap(1, 0, 0), ap(2, 6000, 0)]) == []
    assert len(task2.check_ap_spacing([ap(1, 0, 0), ap(2, 6000 - EPS, 0)])) == 1


# ---------- clearance: at least 0.6 m from walls and columns ----------

def test_wall_clearance_limit():
    f = floor([("r", rect(0, 0, 10000, 10000))])
    assert task2.check_clearance(f, [ap(1, 600, 5000)]) == []
    assert len(task2.check_clearance(f, [ap(1, 600 - EPS, 5000)])) == 1


def test_wall_mounted_ap_exempt_from_walls_but_not_columns():
    f = floor([("r", rect(0, 0, 10000, 10000))], columns=[rect(4800, 4800, 5200, 5200)])
    assert task2.check_clearance(f, [ap(1, 50, 2000, "wall")]) == []
    assert len(task2.check_clearance(f, [ap(1, 5000, 5800 - EPS, "wall")])) == 1


# ---------- obstacles ----------

BIG = [("r", rect(-20000, -20000, 30000, 30000))]


def test_ap_inside_obstacle_fails():
    f = floor(BIG, obstacles=[rect(0, 0, 2000, 2000)])
    assert len(task2.check_obstacles(f, [ap(1, 1000, 1000)])) == 1


def test_side_rule_only_above_3m():
    assert task2.get_obstacle_sides_needing_ap(rect(0, 0, 3000, 1000)) == []
    assert len(task2.get_obstacle_sides_needing_ap(rect(0, 0, 3001, 1000))) == 2   # two longest
    assert len(task2.get_obstacle_sides_needing_ap(rect(0, 0, 4000, 4000))) == 4   # square: all four


def test_long_sides_need_an_ap_within_5m():
    f = floor(BIG, obstacles=[rect(0, 0, 8000, 2000)])
    assert task2.check_obstacles(f, [ap(1, 4000, -5000), ap(2, 4000, 7000)]) == []
    assert len(task2.check_obstacles(f, [ap(1, 4000, -5000)])) == 1                # bottom side unserved


# ---------- capacity: at most 24 per comms room ----------

def test_capacity_limit():
    aps = [ap(i, 0, 0, comms_room="comms_1") for i in range(24)]
    assert task2.check_capacity(aps) == []
    assert len(task2.check_capacity(aps + [ap(99, 0, 0, comms_room="comms_1")])) == 1


# ---------- cables ----------

HALL = floor([("hall", rect(0, 0, 100000, 5000))], comms=[rect(0, 0, 3000, 3000)])


def test_cable_length_limit():
    ok = ap(1, 93000, 1500, comms_room="comms_1", cable=[[93000, 1500], [3000, 1500]])          # 90.0 m
    long = ap(1, 93000 + EPS, 1500, comms_room="comms_1", cable=[[93000 + EPS, 1500], [3000, 1500]])
    assert task2.check_cables(HALL, [ok]) == []
    assert len(task2.check_cables(HALL, [long])) == 1


def test_cable_cannot_pass_obstacle_even_along_the_outer_wall():
    # The plant touches the outer wall (y = 0): there is no gap to run a cable through.
    f = floor([("hall", rect(0, 0, 40000, 20000))], comms=[rect(0, 0, 3000, 3000)],
              obstacles=[rect(10000, 0, 12000, 10000)])
    squeeze = ap(1, 30000, 1500, comms_room="comms_1", cable=[[30000, 1500], [30000, 0], [3000, 0]])
    assert len(task2.check_cables(f, [squeeze])) == 1
    [routed] = task2.plan_cables(f, [ap(1, 30000, 1500)])
    assert task2.check_cables(f, [routed]) == []                  # router goes round it instead


def test_router_orthogonal_first_diagonal_only_if_needed():
    f = floor([("hall", rect(0, 0, 70000, 70000))], comms=[rect(0, 0, 3000, 3000)])
    straight = lambda r: all(a[0] == b[0] or a[1] == b[1] for a, b in zip(r, r[1:]))
    [near] = task2.plan_cables(f, [ap(1, 20000, 20000)])          # 34 m orthogonal: fits
    assert straight(near["cable"])
    [far] = task2.plan_cables(f, [ap(1, 66000, 66000)])           # 126 m orthogonal, 89 m diagonal
    assert not straight(far["cable"]) and task2.get_route_length(far["cable"]) <= 90000


# ---------- coverage: 12 m through the building ----------
# One-row rooms (grid dots at y = 250) keep path lengths exact.

def uncovered(f, aps):
    return {tuple(p) for v in task2.check_coverage(f, aps) for p in v.get("spots", [])}


def test_coverage_limit():
    assert uncovered(floor([("r", rect(0, 0, 12500, 500))]), [ap(1, 250, 250)]) == set()        # 12.0 m
    assert uncovered(floor([("r", rect(0, 0, 13000, 500))]), [ap(1, 250, 250)]) == {(12750.0, 250.0)}


def test_wall_adds_4m_and_door_adds_nothing():
    rooms = [("a", rect(0, 0, 6000, 500)), ("b", rect(6000, 0, 9000, 500))]
    # through the wall: 8 m + 4 m = 12 m covered, 8.5 m + 4 m not
    assert uncovered(floor(rooms), [ap(1, 250, 250)]) == {(8750.0, 250.0)}
    rooms = [("a", rect(0, 0, 6000, 1000)), ("b", rect(6000, 0, 9000, 1000))]
    door = {"id": "door_1", "center": [6000, 500], "angle": 90, "width": 900, "joins": ["a", "b"]}
    assert uncovered(floor(rooms, doors=[door]), [ap(1, 250, 250)]) == set()


# ---------- comparison with the engineer ----------

def test_matching_is_one_to_one_within_2m():
    f = floor([("r", rect(0, 0, 30000, 10000))], comms=[rect(0, 0, 3000, 3000)])
    c = task2.compare_with_engineer(f, [ap(1, 10000, 5000), ap(2, 10500, 5000)], [ap(1, 12000, 5000)])
    assert c["matched"] == 1                                      # exactly 2 m, used only once
    c = task2.compare_with_engineer(f, [ap(1, 10000, 5000)], [ap(1, 12000 + EPS, 5000)])
    assert c["matched"] == 0
