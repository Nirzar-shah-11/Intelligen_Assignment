import heapq   # priority queue: the smallest item always comes out first
import json
import math

import numpy as np
import shapely   # 2D geometry
from scipy.sparse import coo_matrix   # sparse matrix, used to build graphs
from scipy.optimize import linear_sum_assignment
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import LineString, Point, Polygon
import shapely.ops
from shapely.ops import unary_union

# All distances are in mm.
min_ap_dist = 6000              # APs must be at least 6 m apart
min_wall_dist = 600             # APs must be 0.6 m from walls and columns
obstacle_long_side = 3000       # obstacle sides up to 3 m need no AP nearby
obstacle_side = 5000            # long obstacle sides need an AP within 5 m
max_aps = 24                    # one comms room serves at most 24 APs

coverage_range = 12000          # an AP covers up to 12 m
wall_cost = 4000                # each wall counts as 4 m of extra distance
grid_size = 500                 # coverage grid: one dot every 0.5 m
wall_ap_offset = 200            # wall-mounted AP sits 0.2 m off the wall
steps = [(1, 0), (0, 1), (1, 1), (1, -1), (2, 1), (1, 2), (2, -1), (1, -2)]   # grid neighbour directions

max_cable = 90000               # cable can be at most 90 m

candidate_step = 1000           # placer tries a spot every 1 m
compare_match_distance = 2000   # our AP and the engineer's match if within 2 m
nudge_max_room = 120_000_000    # rooms up to 120 m2 get their AP moved towards the middle


# ================= Small helpers =================

def ap_to_point(ap):
    # AP dictionary -> Shapely point.
    x, y = ap["position"]
    return Point(x, y)


def make_violation(rule, ap_ids, detail, at=None):
    # One broken rule: which rule, which APs, what is wrong, and where.
    return {"rule": rule, "ap_ids": ap_ids, "detail": detail, "at": at}


def format_metres(mm):
    # 2500 -> "2.5 m"
    return str(round(mm / 1000, 2)) + " m"


def get_polygon_sides(points):
    # All sides of a shape, including the last point back to the first.
    sides = []
    for i in range(len(points)):
        start = points[i]
        end = points[(i + 1) % len(points)]
        sides.append(LineString([start, end]))
    return sides


def door_point_to_plan(cx, cy, angle, lx, ly):
    # Move a point from the door's own drawing onto the plan: rotate, then shift.
    a = math.radians(angle)
    x = cx + lx * math.cos(a) - ly * math.sin(a)
    y = cy + lx * math.sin(a) + ly * math.cos(a)
    return x, y


def get_door_endpoints(door):
    # The two ends of a door, half a width either side of its centre.
    cx, cy = door["center"]
    half = door["width"] / 2
    return (door_point_to_plan(cx, cy, door["angle"], -half, 0),
            door_point_to_plan(cx, cy, door["angle"], half, 0))


def split_walls_at_doors(floor):
    # Cut a gap in each wall where a door is.
    # Returns a list of pieces: {"wall_id", "start", "end"}.
    walls = []
    for wall in floor["walls"]:
        wall_start = wall["start"]
        wall_end = wall["end"]
        wall_gaps = []

        # Step 1: what kind of wall is it?
        x1, y1 = wall_start
        x2, y2 = wall_end
        if y1 == y2:
            orientation = "horizontal"
        elif x1 == x2:
            orientation = "vertical"
        else:
            # Slanted wall: keep it whole.
            walls.append({"wall_id": wall["id"], "start": wall_start, "end": wall_end})
            continue

        # Step 2: find the doors that lie on this wall.
        for door in floor["doors"]:
            door_start, door_end = get_door_endpoints(door)
            dx1, dy1 = door_start
            dx2, dy2 = door_end

            if orientation == "horizontal":
                if abs(dy1 - y1) < 1 and abs(dy2 - y1) < 1:      # door has the wall's y
                    gap_start = min(dx1, dx2)
                    gap_end = max(dx1, dx2)
                    if gap_start >= min(x1, x2) and gap_end <= max(x1, x2):   # inside the wall
                        wall_gaps.append((gap_start, gap_end))
            elif orientation == "vertical":
                if abs(dx1 - x1) < 1 and abs(dx2 - x1) < 1:      # door has the wall's x
                    gap_start = min(dy1, dy2)
                    gap_end = max(dy1, dy2)
                    if gap_start >= min(y1, y2) and gap_end <= max(y1, y2):   # inside the wall
                        wall_gaps.append((gap_start, gap_end))

        # Step 3: walk along the wall and skip over each gap.
        wall_gaps.sort()

        if orientation == "horizontal":
            position = min(x1, x2)                     # start at the left end
            for gap_start, gap_end in wall_gaps:
                if gap_start > position:
                    walls.append({"wall_id": wall["id"],
                                  "start": [position, y1], "end": [gap_start, y1]})
                position = gap_end                     # jump over the door
            if position < max(x1, x2):                 # last piece, after the last door
                walls.append({"wall_id": wall["id"],
                              "start": [position, y1], "end": [max(x1, x2), y1]})

        elif orientation == "vertical":
            position = min(y1, y2)                     # start at the top end
            for gap_start, gap_end in wall_gaps:
                if gap_start > position:
                    walls.append({"wall_id": wall["id"],
                                  "start": [x1, position], "end": [x1, gap_start]})
                position = gap_end
            if position < max(y1, y2):
                walls.append({"wall_id": wall["id"],
                              "start": [x1, position], "end": [x1, max(y1, y2)]})

    return walls


# ================= Rules: spacing, clearance, obstacles, capacity =================

def check_ap_spacing(aps):
    # Rule: every pair of APs must be at least min_ap_dist apart.
    found = []
    for i in range(len(aps)):
        for j in range(i + 1, len(aps)):            # each pair only once
            a, b = aps[i], aps[j]
            d = ap_to_point(a).distance(ap_to_point(b))
            if d < min_ap_dist:
                found.append(make_violation("spacing", [a["id"], b["id"]],
                                            format_metres(d) + " apart, need at least " + format_metres(min_ap_dist),
                                            a["position"]))
    return found


def check_clearance(floor, aps):
    # Rule: APs must keep min_wall_dist away from walls and columns.
    # Walls: we use the full walls from floor.json (doors are not cut out).
    # So an AP standing in a door opening also counts as "near a wall".
    walls = [(w["id"], LineString([w["start"], w["end"]])) for w in floor["walls"]]
    columns = [(c["id"], Polygon(c["points"])) for c in floor["columns"]]

    found = []
    for ap in aps:
        p = ap_to_point(ap)

        if ap.get("type") != "wall":                 # wall-mounted APs may touch walls
            for wall_id, line in walls:
                d = p.distance(line)
                if d < min_wall_dist:
                    found.append(make_violation(
                        "clearance", [ap["id"]],
                        format_metres(d) + " from " + wall_id + ", need at least " + format_metres(min_wall_dist),
                        ap["position"]))

        for column_id, shape in columns:
            d = p.distance(shape)                    # 0 if the AP is inside the column
            if d < min_wall_dist:
                found.append(make_violation(
                    "clearance", [ap["id"]],
                    format_metres(d) + " from " + column_id + ", need at least " + format_metres(min_wall_dist),
                    ap["position"]))
    return found


def get_obstacle_sides_needing_ap(points):
    # Which sides of an obstacle need an AP nearby.
    sides = get_polygon_sides(points)
    lengths = [s.length for s in sides]
    if max(lengths) <= obstacle_long_side:
        return []                                    # obstacle is small: no side rule
    if max(lengths) == min(lengths):
        return sides                                 # all sides equal: check all of them
    order = sorted(range(len(sides)), key=lambda i: -lengths[i])
    return [sides[i] for i in order[:2]]             # the two longest sides


def check_obstacles(floor, aps):
    # Rule: no AP inside an obstacle, and an AP near each long obstacle side.
    found = []
    for obstacle in floor["obstacles"]:
        shape = Polygon(obstacle["points"])
        name = obstacle["id"] + " (" + obstacle.get("kind", "obstacle") + ")"

        # a: no AP inside
        for ap in aps:
            if shape.contains(ap_to_point(ap)):
                found.append(make_violation(
                    "obstacle", [ap["id"]], "AP is inside " + name, ap["position"]))

        # b: each long side needs an AP within 5 m
        for side in get_obstacle_sides_needing_ap(obstacle["points"]):
            near = [ap for ap in aps if ap_to_point(ap).distance(side) <= obstacle_side]
            if not near:
                middle = side.interpolate(0.5, normalized=True)
                found.append(make_violation(
                    "obstacle", [],
                    "no AP within " + format_metres(obstacle_side) + " of a " + format_metres(side.length)
                    + " side of " + name,
                    [round(middle.x, 1), round(middle.y, 1)]))
    return found


def check_capacity(aps):
    # Rule: one comms room serves at most max_aps APs.
    count = {}
    for ap in aps:
        room = ap.get("comms_room")
        if room:                                     # APs without a cable yet are skipped
            count[room] = count.get(room, 0) + 1

    found = []
    for room, n in sorted(count.items()):
        if n > max_aps:
            ids = [ap["id"] for ap in aps if ap.get("comms_room") == room]
            found.append(make_violation(
                "capacity", ids,
                room + " serves " + str(n) + " APs, at most " + str(max_aps)))
    return found


def check_basic_rules(floor, aps):
    # The four simple rules. Coverage and cables are checked separately in check_all.
    return (check_ap_spacing(aps)
            + check_clearance(floor, aps)
            + check_obstacles(floor, aps)
            + check_capacity(aps))


# ================= Coverage grid =================

def build_grid(floor):
    # Cover the floor with dots every grid_size mm and link neighbouring dots.
    # The link cost is the distance, plus wall_cost for every wall crossed.
    rooms = sorted(floor["rooms"], key=lambda r: Polygon(r["points"]).area)   # smallest room first
    room_shapes = [Polygon(r["points"]) for r in rooms]
    outline = unary_union(room_shapes)                                        # the whole building
    blocked = unary_union([Polygon(o["points"]) for o in floor["obstacles"]] or [Polygon()])
    no_need = unary_union([Polygon(c["points"]) for c in floor["columns"] + floor["comms_rooms"]]
                          or [Polygon()])                                     # areas that need no coverage
    walls = [LineString([w["start"], w["end"]]) for w in split_walls_at_doors(floor)]
    wall_tree = shapely.STRtree(walls)   # spatial index: finds the walls a line touches quickly

    # Dots on a regular grid over the building.
    min_x, min_y, max_x, max_y = outline.bounds
    xs = np.arange(min_x + grid_size / 2, max_x, grid_size)
    ys = np.arange(min_y + grid_size / 2, max_y, grid_size)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    gx, gy = gx.ravel(), gy.ravel()   # flatten the 2D arrays into 1D lists
    pts = shapely.points(gx, gy)

    # Keep dots inside the building, not in an obstacle, and not on a wall.
    keep = shapely.contains(outline, pts) & ~shapely.intersects(blocked, pts)
    on_wall = np.zeros(len(pts), dtype=bool)
    on_wall[wall_tree.query(pts, predicate="dwithin", distance=1)[0]] = True
    keep &= ~on_wall

    gx, gy, pts = gx[keep], gy[keep], pts[keep]
    n = len(gx)

    # Which room each dot is in (the smallest room wins), and whether it needs coverage.
    room_of = np.full(n, -1)
    for k, shape in enumerate(room_shapes):
        free = room_of == -1
        room_of[free & shapely.contains(shape, pts)] = k
    needs = ~shapely.intersects(no_need, pts) & (room_of >= 0)

    # Dot coordinates -> (column, row). index finds a dot's number from its (column, row), so neighbours are easy to find.
    col = np.round((gx - xs[0]) / grid_size).astype(int)
    row = np.round((gy - ys[0]) / grid_size).astype(int)
    index = {(c, r): i for i, (c, r) in enumerate(zip(col, row))}

    # Connect each dot to its neighbours in 8+ directions.
    a_list, b_list = [], []
    for dc, dr in steps:
        for i in range(n):
            j = index.get((col[i] + dc, row[i] + dr))
            if j is not None:
                a_list.append(i)
                b_list.append(j)
    a, b = np.array(a_list), np.array(b_list)

    # Draw a straight line for each connection and drop any that leave the building or cut through an obstacle.
    lines = shapely.linestrings(np.stack([np.stack([gx[a], gy[a]], 1),
                                          np.stack([gx[b], gy[b]], 1)], 1))
    ok = shapely.covered_by(lines, outline) & ~shapely.intersects(lines, blocked)
    a, b, lines = a[ok], b[ok], lines[ok]

    cost = shapely.length(lines) + wall_cost * count_walls_crossed(wall_tree, lines)

    return {"x": gx, "y": gy, "needs": needs, "room_of": room_of, "rooms": rooms,
            "a": a, "b": b, "cost": cost, "n": n,
            "outline": outline, "blocked": blocked, "room_shapes": room_shapes,
            "walls": walls, "wall_tree": wall_tree}


def count_walls_crossed(wall_tree, lines):
    # For each line, how many walls it crosses.
    hits = wall_tree.query(lines, predicate="intersects")[0]   # query: which walls in the tree touch each line?
    return np.bincount(hits, minlength=len(lines))


def get_signal_start_point(ap, grid):
    # Where the AP's signal starts. Ceiling APs: their own position.
    # Wall APs: a point just off the wall, on the room side.
    x, y = ap["position"]
    if ap.get("type") != "wall":
        return Point(x, y)

    p = Point(x, y)
    wall = min(grid["walls"], key=lambda w: w.distance(p))               # the nearest wall
    (x1, y1), (x2, y2) = wall.coords[0], wall.coords[-1]
    length = math.hypot(x2 - x1, y2 - y1)
    nx, ny = -(y2 - y1) / length, (x2 - x1) / length                     # direction pointing away from the wall
    sides = [Point(x + nx * wall_ap_offset, y + ny * wall_ap_offset),
             Point(x - nx * wall_ap_offset, y - ny * wall_ap_offset)]    # one point on each side of the wall

    wanted = ap.get("room")
    for k, room in enumerate(grid["rooms"]):
        if wanted and wanted not in (room["id"], room.get("name")):
            continue
        for s in sides:
            if grid["room_shapes"][k].contains(s):
                return s
    return p


def connect_ap_to_grid(ap, grid):
    # Link the AP to the grid dots within 2 grid steps. Returns those dots and the link costs.
    p = get_signal_start_point(ap, grid)
    near = np.nonzero(np.hypot(grid["x"] - p.x, grid["y"] - p.y) <= 2 * grid_size)[0]
    if len(near) == 0:
        return near, np.array([])
    lines = shapely.linestrings(np.stack([np.stack([np.full(len(near), p.x), np.full(len(near), p.y)], 1),
                                          np.stack([grid["x"][near], grid["y"][near]], 1)], 1))
    ok = shapely.covered_by(lines, grid["outline"]) & ~shapely.intersects(lines, grid["blocked"])
    near, lines = near[ok], lines[ok]
    cost = shapely.length(lines) + wall_cost * count_walls_crossed(grid["wall_tree"], lines)
    return near, cost


def compute_signal_distances(grid, aps):
    # Distance (walls included) from the nearest AP to every grid dot.
    n = grid["n"]
    a, b, cost = list(grid["a"]), list(grid["b"]), list(grid["cost"])
    starts = []
    for k, ap in enumerate(aps):
        near, c = connect_ap_to_grid(ap, grid)
        node = n + k                                  # each AP is an extra node after the dots
        starts.append(node)
        a += [node] * len(near)
        b += list(near)
        cost += list(c)

    size = n + len(aps)
    if not starts:
        return np.full(n, np.inf)
    graph = coo_matrix((cost, (a, b)), shape=(size, size)).tocsr()   # lists -> sparse graph
    dist = dijkstra(graph, directed=False, indices=starts, min_only=True, limit=coverage_range)
    return dist[:n]


def check_coverage(floor, aps, grid=None):
    # Rule: every dot that needs Wi-Fi must be within coverage_range of an AP.
    if grid is None:
        grid = build_grid(floor)

    found = []
    for ap in aps:
        if not grid["outline"].covers(ap_to_point(ap)):
            found.append(make_violation("coverage", [ap["id"]],
                                        "AP is outside the building", ap["position"]))

    dist = compute_signal_distances(grid, aps)
    missing = grid["needs"] & (dist > coverage_range)      # dots that need Wi-Fi but are too far

    # One violation per room that has uncovered dots.
    for k, room in enumerate(grid["rooms"]):
        spots = np.nonzero(missing & (grid["room_of"] == k))[0]
        if len(spots) == 0:
            continue
        area = len(spots) * grid_size * grid_size / 1e6
        first = spots[0]
        v = make_violation("coverage", [],
                           (room.get("name") or room["id"]) + ": " + str(round(area, 2)) + " m2 not covered",
                           [float(grid["x"][first]), float(grid["y"][first])])
        v["room"] = room["id"]
        v["spots"] = [[float(grid["x"][i]), float(grid["y"][i])] for i in spots]
        found.append(v)
    return found


# ================= Rules: cables =================

def build_cable_shapes(floor):
    # Where cables may run: the building minus the obstacles, grown 1 mm so a cable may
    # touch an obstacle's edge. Subtracting first (instead of checking "inside the building"
    # and "not in an obstacle" separately) stops a cable squeezing along the outer wall
    # past an obstacle that touches it, where there is no gap at all.
    blocked = unary_union([Polygon(o["points"]) for o in floor["obstacles"]] or [Polygon()])
    outline = unary_union([Polygon(r["points"]) for r in floor["rooms"]])
    free = outline.difference(blocked).buffer(1)
    shapely.prepare(free)                      # makes the many covered_by checks faster
    return free


def is_cable_segment_ok(a, b, shapes):
    # A cable piece is fine if it stays in the free area (inside the building, out of obstacles).
    return LineString([a, b]).covered_by(shapes)


def get_route_length(route):
    # Total length of a route (a list of points).
    return sum(math.dist(route[i], route[i + 1]) for i in range(len(route) - 1))


def find_shortest_cable_any_angle(floor, position, comms, shapes):
    # Shortest cable from an AP to a comms room, with lines at any angle.
    # Dijkstra over the corners of rooms, obstacles and the comms room.
    # Returns (length, route), or (inf, None) if there is no way.
    room = Polygon(comms["points"])
    start = tuple(position)
    if room.covers(Point(start)):
        return 0.0, [list(start)]                    # the AP is already in the comms room

    # Possible turning points: the AP and every corner.
    corners = []
    for o in floor["obstacles"]:
        corners += [tuple(p) for p in o["points"]]
    for r in floor["rooms"]:
        corners += [tuple(p) for p in r["points"]]
    corners += [tuple(p) for p in comms["points"]]
    nodes = [start] + sorted(set(corners) - {start})

    best = {0: 0.0}                  # shortest known distance to each node
    came_from = {0: None}            # previous node on the best route
    queue = [(0.0, 0)]
    done = set()
    finish = None                    # best way into the comms room: (length, node, end point)
    while queue:
        d, i = heapq.heappop(queue)
        if i in done:
            continue
        if finish is not None and d >= finish[0]:
            break                                    # nothing shorter can be found any more
        done.add(i)
        here = nodes[i]

        # Can we go straight from here to the comms room wall?
        if finish is None or d < finish[0]:
            p, _ = shapely.ops.nearest_points(room.exterior, Point(here))
            if is_cable_segment_ok(here, (p.x, p.y), shapes):
                total = d + Point(here).distance(p)
                if finish is None or total < finish[0]:
                    finish = (total, i, [p.x, p.y])

        # Try every other corner as the next step.
        for j in range(1, len(nodes)):
            if j in done:
                continue
            nd = d + math.dist(here, nodes[j])
            if nd < best.get(j, math.inf) and (finish is None or nd < finish[0]) \
                    and is_cable_segment_ok(here, nodes[j], shapes):
                best[j] = nd
                came_from[j] = i
                heapq.heappush(queue, (nd, j))

    if finish is None:
        return math.inf, None
    # Walk back from the end to the start to build the route.
    route = [finish[2]]
    i = finish[1]
    while i is not None:
        route.append(list(nodes[i]))
        i = came_from[i]
    return finish[0], route[::-1]


def check_cables(floor, aps):
    # Rule: every AP needs a valid cable to a comms room, at most max_cable long.
    shapes = build_cable_shapes(floor)
    rooms = {c["id"]: c for c in floor["comms_rooms"]}
    found = []
    for ap in aps:
        wanted = ap.get("comms_room")

        if wanted and wanted not in rooms:
            found.append(make_violation("cable", [ap["id"]],
                                        "comms room " + str(wanted) + " does not exist", ap["position"]))
            continue

        # The AP has a cable: check its start, end, path and length.
        route = ap.get("cable")
        if route:
            if math.dist(route[0], ap["position"]) > 1:
                found.append(make_violation("cable", [ap["id"]], "cable does not start at the AP", ap["position"]))
            if not wanted or Polygon(rooms[wanted]["points"]).distance(Point(route[-1])) > 1:
                found.append(make_violation("cable", [ap["id"]],
                                            "cable does not end at its comms room", route[-1]))
            for i in range(len(route) - 1):
                if not is_cable_segment_ok(route[i], route[i + 1], shapes):
                    found.append(make_violation("cable", [ap["id"]],
                                                "cable goes through an obstacle or outside the building", route[i]))
                    break
            length = get_route_length(route)
            if length > max_cable:
                found.append(make_violation("cable", [ap["id"]],
                                            "cable " + format_metres(length) + ", at most " + format_metres(max_cable),
                                            ap["position"]))
            continue

        # No cable yet: check that a comms room is close enough to reach.
        targets = [rooms[wanted]] if wanted else list(rooms.values())
        lengths = [find_shortest_cable_any_angle(floor, ap["position"], c, shapes)[0] for c in targets]
        shortest = min(lengths) if lengths else math.inf
        if shortest > max_cable:
            detail = "no comms room within " + format_metres(max_cable)
            if shortest < math.inf:
                detail += " (shortest cable " + format_metres(shortest) + ")"
            found.append(make_violation("cable", [ap["id"]], detail, ap["position"]))
    return found


def check_all(floor, aps, grid=None):
    # Run every rule and return all violations.
    return (check_basic_rules(floor, aps)
            + check_coverage(floor, aps, grid)
            + check_cables(floor, aps))


# ================= Placer: choose where the APs go =================

def place_aps(floor, grid=None):
    # Put ceiling APs on an empty floor so all rules pass, using as few as we can.
    grid = grid or build_grid(floor)
    x, y = grid["x"], grid["y"]

    # 1. Possible spots: every 1 m, inside a room that needs Wi-Fi.
    on_step = (np.round(x - x.min()) % candidate_step == 0) & (np.round(y - y.min()) % candidate_step == 0)
    spots = np.nonzero(on_step & grid["needs"])[0]

    # Keep spots 0.6 m from walls and columns, and within 90 m of cable from a comms room.
    pts = shapely.points(x[spots], y[spots])
    walls = unary_union([LineString([w["start"], w["end"]]) for w in floor["walls"]])
    columns = unary_union([Polygon(c["points"]) for c in floor["columns"]] or [Polygon()])
    ok = (shapely.distance(walls, pts) >= min_wall_dist) & (shapely.distance(columns, pts) >= min_wall_dist)
    shapes = build_cable_shapes(floor)
    ok &= [min(find_shortest_cable_any_angle(floor, [x[i], y[i]], c, shapes)[0] for c in floor["comms_rooms"]) <= max_cable
           for i in spots]
    spots = spots[ok]

    # 2. Which dots each spot would cover.
    graph = coo_matrix((grid["cost"], (grid["a"], grid["b"])), shape=(grid["n"], grid["n"])).tocsr()
    covers = (dijkstra(graph, directed=False, indices=spots, limit=coverage_range) <= coverage_range) & grid["needs"]

    def is_too_close_to_chosen(i, chosen):
        # Is spot i closer than min_ap_dist to any chosen spot?
        return any(math.dist((x[i], y[i]), (x[j], y[j])) < min_ap_dist for j in chosen)

    # 3. Greedy: keep picking the spot that covers the most uncovered dots.
    chosen = []
    uncovered = grid["needs"] & covers.any(axis=0)
    while uncovered.any():
        gain = covers[:, uncovered].sum(axis=1)      # uncovered dots each spot would add
        for k in np.argsort(-gain, kind="stable"):
            if gain[k] > 0 and not is_too_close_to_chosen(spots[k], [spots[c] for c in chosen]):
                chosen.append(k)
                uncovered &= ~covers[k]
                break
        else:
            break                                   # nothing left that we can still cover

    # 4. Long obstacle sides with no AP within 5 m: add the nearest allowed spot.
    sides = [s for o in floor["obstacles"] for s in get_obstacle_sides_needing_ap(o["points"])]

    def is_side_served(side, chosen):
        # Is there a chosen AP within obstacle_side of this side?
        return any(side.distance(Point(x[spots[c]], y[spots[c]])) <= obstacle_side for c in chosen)

    for side in sides:
        if not is_side_served(side, chosen):
            near = sorted(range(len(spots)), key=lambda k: side.distance(Point(x[spots[k]], y[spots[k]])))
            for k in near:
                if side.distance(Point(x[spots[k]], y[spots[k]])) > obstacle_side:
                    break
                if not is_too_close_to_chosen(spots[k], [spots[c] for c in chosen]):
                    chosen.append(k)
                    break

    # 5. Remove any AP we don't need.
    wanted = covers[chosen].any(axis=0)
    for k in list(reversed(chosen)):
        rest = [c for c in chosen if c != k]
        if rest and (covers[rest].any(axis=0) >= wanted).all() and \
                all(is_side_served(s, rest) for s in sides if is_side_served(s, chosen)):
            chosen = rest

    # Number the APs by position (top to bottom, left to right).
    chosen.sort(key=lambda k: (y[spots[k]], x[spots[k]]))
    aps = [{"id": "ap_" + str(n + 1), "position": [float(x[spots[k]]), float(y[spots[k]])], "type": "ceiling"}
           for n, k in enumerate(chosen)]
    return move_aps_towards_room_centre(floor, grid, aps)


def move_aps_towards_room_centre(floor, grid, aps):
    # The greedy step likes spots near walls. Move each AP towards the middle of its room,
    # but only if every rule still passes. Big open rooms are skipped (their middle means nothing).
    def count_problems(layout):
        return len(check_basic_rules(floor, layout) + check_coverage(floor, layout, grid) + check_cables(floor, layout))

    allowed = count_problems(aps)                    # the layout must not get worse than this
    for ap in aps:
        rooms = [r for r in floor["rooms"] if Polygon(r["points"]).covers(ap_to_point(ap))]
        if not rooms:
            continue
        room = min(rooms, key=lambda r: Polygon(r["points"]).area)
        if Polygon(room["points"]).area > nudge_max_room:
            continue
        centre = Polygon(room["points"]).centroid
        old = ap["position"]
        for f in (1, 0.75, 0.5, 0.25):              # try the centre first, then closer to where it was
            ap["position"] = [round(old[0] + f * (centre.x - old[0]), -2),
                              round(old[1] + f * (centre.y - old[1]), -2)]
            if count_problems(aps) <= allowed:
                break
        else:
            ap["position"] = old                    # no better spot: keep it where it was
    return aps


# ================= Cable planner: route cables and pick comms rooms =================

def find_shortest_cable_right_angles(floor, position, comms, shapes):
    # Shortest cable using only horizontal and vertical lines, around obstacles.
    room = Polygon(comms["points"])
    if room.covers(Point(position)):
        return 0.0, [list(position)]

    # Grid lines through the AP and through every corner of rooms, obstacles and the comms room.
    things = floor["rooms"] + floor["obstacles"] + [comms]
    xs = sorted({position[0]} | {p[0] for t in things for p in t["points"]})
    ys = sorted({position[1]} | {p[1] for t in things for p in t["points"]})

    def get_neighbour_steps(i, j):
        # Moves of one grid line up, down, left or right that keep the cable valid.
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = i + di, j + dj
            if 0 <= a < len(xs) and 0 <= b < len(ys) and \
                    is_cable_segment_ok((xs[i], ys[j]), (xs[a], ys[b]), shapes):
                yield a, b, abs(xs[a] - xs[i]) + abs(ys[b] - ys[j])

    start = (xs.index(position[0]), ys.index(position[1]))
    best, came_from, queue = {start: 0.0}, {start: None}, [(0.0, start)]
    while queue:
        d, here = heapq.heappop(queue)
        if d > best[here]:
            continue
        if room.exterior.distance(Point(xs[here[0]], ys[here[1]])) < 1:      # reached the comms room
            route = []
            while here:                              # walk back to the start
                route.append([xs[here[0]], ys[here[1]]])
                here = came_from[here]
            return d, simplify_route(route[::-1])
        for a, b, step in get_neighbour_steps(*here):
            if d + step < best.get((a, b), math.inf):
                best[(a, b)] = d + step
                came_from[(a, b)] = here
                heapq.heappush(queue, (d + step, (a, b)))
    return math.inf, None


def simplify_route(route):
    # Drop points in the middle of a straight line.
    out = [route[0]]
    for i in range(1, len(route) - 1):
        (x1, y1), (x2, y2), (x3, y3) = out[-1], route[i], route[i + 1]
        if (x2 - x1) * (y3 - y2) != (y2 - y1) * (x3 - x2):      # keep only real turns
            out.append(route[i])
    return out + route[1:][-1:]


def plan_cables(floor, aps):
    # Give each AP a comms room and a cable. Returns new AP dicts with "comms_room" and "cable".
    shapes = build_cable_shapes(floor)
    options = {}
    for ap in aps:
        options[ap["id"]] = []
        for comms in floor["comms_rooms"]:
            length, route = find_shortest_cable_right_angles(floor, ap["position"], comms, shapes)
            slanted = False
            if length > max_cable:                       # slanted lines only if straight ones are too long
                length, route = find_shortest_cable_any_angle(floor, ap["position"], comms, shapes)
                slanted = True
            if length <= max_cable:
                options[ap["id"]].append((slanted, length, comms["id"], route))
        options[ap["id"]].sort(key=lambda o: o[:3])     # straight first, then shortest

    # APs with the fewest choices go first, so they get a room before rooms fill up.
    count, result = {}, {}
    for ap in sorted(aps, key=lambda a: (len(options[a["id"]]), a["id"])):
        free = [o for o in options[ap["id"]] if count.get(o[2], 0) < max_aps]    # rooms that still have space
        pick = (free or options[ap["id"]] or [None])[0]
        new = dict(ap)
        if pick:
            new["comms_room"], new["cable"] = pick[2], pick[3]
            count[pick[2]] = count.get(pick[2], 0) + 1
        result[ap["id"]] = new
    return [result[ap["id"]] for ap in aps]


# ================= Compare with the engineer =================

def compare_with_engineer(floor, ours, engineer):
    # Pair our APs with the engineer's (one-to-one, at most 2 m apart) and check the engineer's layout.
    pairs = []
    if ours and engineer:
        d = np.array([[math.dist(a["position"], b["position"]) for b in engineer] for a in ours])   # distance table
        rows, cols = linear_sum_assignment(np.where(d <= compare_match_distance, d, 1e9))           # best one-to-one pairing
        pairs = [{"ours": "our_" + ours[r]["id"], "engineer": "eng_" + engineer[c]["id"], "apart_m": round(float(d[r, c]) / 1000, 2)}
                 for r, c in zip(rows, cols) if d[r, c] <= compare_match_distance]

    matched_ours = {p["ours"] for p in pairs}
    matched_eng = {p["engineer"] for p in pairs}
    eng_problems = check_all(floor, plan_cables(floor, engineer))
    return {
        "engineer_aps": len(engineer),
        "our_aps": len(ours),
        "matched": len(pairs),
        "pairs": pairs,
        "our_unmatched": ["our_" + a["id"] for a in ours if "our_" + a["id"] not in matched_ours],
        "engineer_unmatched": ["eng_" + a["id"] for a in engineer if "eng_" + a["id"] not in matched_eng],
        "engineer_breaks": {r: sum(v["rule"] == r for v in eng_problems) for r in sorted({v["rule"] for v in eng_problems})},
        "engineer_problems": eng_problems,
    }


# ================= Run everything on one plan =================

def run_plan(path, out_path=None):
    # Load a floor plan, place APs, plan cables, check the rules and save the result as JSON.
    with open(path) as f:
        floor = json.load(f)
    grid = build_grid(floor)
    aps = plan_cables(floor, place_aps(floor, grid))
    result = {"aps": aps, "problems": check_all(floor, aps, grid)}
    if floor.get("aps_reference"):
        result["comparison"] = compare_with_engineer(floor, aps, floor["aps_reference"])
    with open(out_path or path.replace(".json", "_aps.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        r = run_plan(path)
        print(path, ":", len(r["aps"]), "APs,", len(r["problems"]), "problems")