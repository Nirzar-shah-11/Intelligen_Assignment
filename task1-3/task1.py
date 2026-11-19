import json
import sys
import math
import xml.etree.ElementTree as ET
from collections import Counter
from shapely.geometry import Polygon

# ---------- settings ----------

KNOWN_KINDS = {"lift-core", "stair-core", "plant", "rack"}
KNOWN_LAYERS = ["walls", "doors", "rooms", "columns", "comms-room", "obstacles", "aps-reference"]

# ---------- warnings and counting ----------

warnings = []


def load_dimension(root):
    viewBox = root.get("viewBox")
    if viewBox:
        x, y, width, height = map(float, viewBox.split())
        dimension = {"width": width, "height": height}
        print("Loaded dimension:", dimension)
        return dimension
    return None

def warn(*parts):
    # Print a warning and save it for the report.
    message = " ".join(str(p) for p in parts)
    warnings.append(message)
    print("Warning:", message)

def count_by(items, key):
    # Count items per value, e.g. {"door": 13, "wall": 11}.
    return dict(Counter(item[key] for item in items))

# ---------- reading the SVG ----------

def get_layer(root, layer_name):
    # Find the group with this layer name.
    for g in root.iter():
        if g.get("data-layer") == layer_name:
            return g
    return []          # no such layer

def layer_items(root, layer_name):
    # Return (element, source) pairs, e.g. (<polygon>, "rooms/polygon[3]").
    # Numbering starts at 1. We drop the namespace part of the tag.
    items = []
    for index, element in enumerate(get_layer(root, layer_name)):
        tag = element.tag.split("}")[-1]
        items.append((element, layer_name + "/" + tag + "[" + str(index + 1) + "]"))
    return items

def read_points(text):
    # "0,0 8000,0 8000,6000" -> [(0.0, 0.0), (8000.0, 0.0), (8000.0, 6000.0)]
    points = []
    for pair in text.split():
        x, y = pair.split(",")
        points.append((float(x), float(y)))
    return points

def read_transform(text):
    # "translate(4000,6000) rotate(0)" -> (4000.0, 6000.0, 0.0)
    for word in ["translate", "rotate", "(", ")", ","]:
        text = text.replace(word, " ")
    numbers = [float(n) for n in text.split()]
    cx, cy = numbers[0], numbers[1]
    angle = numbers[2] if len(numbers) > 2 else 0.0
    return cx, cy, angle

# ---------- geometry helpers ----------

def edges(points):
    # Each side of a shape as ((x1, y1), (x2, y2)). The last side goes back to the first corner.
    return [(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]

def calculate_room_area(points):
    # Points are in mm, result is in m².
    return Polygon(points).area / 1_000_000

def point_in_polygon(x, y, points):
    # Count edges crossed by a line going right from the point.
    # Odd = inside, even = outside.
    inside = False
    for (x1, y1), (x2, y2) in edges(points):
        if (y1 > y) != (y2 > y):
            cross_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < cross_x:
                inside = not inside
    return inside

def near_line(x, y, a, b, tolerance=50):
    # Is the point within `tolerance` mm of the line from a to b?
    (x1, y1), (x2, y2) = a, b
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(x - x1, y - y1) <= tolerance
    t = ((x - x1) * dx + (y - y1) * dy) / length_sq   # closest spot along the line
    t = max(0, min(1, t))                              # stay between the two ends
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)) <= tolerance

def point_on_edge(x, y, points, tolerance=50):
    # Is the point on any edge of the shape? (Walls are 150 mm thick, so allow a small gap.)
    return any(near_line(x, y, a, b, tolerance) for a, b in edges(points))

def door_local_to_global(cx, cy, angle, lx, ly):
    # Move a point from the door's own drawing onto the plan: rotate, then shift.
    a = math.radians(angle)
    x = cx + lx * math.cos(a) - ly * math.sin(a)
    y = cy + lx * math.sin(a) + ly * math.cos(a)
    return x, y

def overlap(a1, a2, b1, b2):
    # Where two stretches on the same line overlap: (start, end).
    return max(min(a1, a2), min(b1, b2)), min(max(a1, a2), max(b1, b2))

# ---------- walls ----------

def merge(pieces):
    # Join pieces on one line that overlap or touch.
    pieces.sort()
    merged = [list(pieces[0])]
    for start, end in pieces[1:]:
        if start <= merged[-1][1]:            # touches the last piece
            merged[-1][1] = max(merged[-1][1], end)
        else:                                  # gap, so a new wall
            merged.append([start, end])
    return merged

def remove_duplicate_walls(root):
    # Read every wall line as (x1, y1, x2, y2, source).
    lines = [tuple(float(line.get(k)) for k in ("x1", "y1", "x2", "y2")) + (source,)
             for line, source in layer_items(root, "walls")]

    horizontal = {}   # y -> list of (start_x, end_x)
    vertical = {}     # x -> list of (start_y, end_y)
    slanted = []      # kept as they are, not merged bcoz they are not horizontal or vertical

    # Sort lines into horizontal, vertical or slanted.
    for x1, y1, x2, y2, source in lines:
        if y1 == y2:
            horizontal.setdefault(y1, []).append((min(x1, x2), max(x1, x2)))
        elif x1 == x2:
            vertical.setdefault(x1, []).append((min(y1, y2), max(y1, y2)))
        else:
            slanted.append({"start": [x1, y1], "end": [x2, y2]})

    # Merge each line's pieces into the final walls.
    walls = []
    for y, pieces in sorted(horizontal.items()):
        for start, end in merge(pieces):
            walls.append({"start": [start, y], "end": [end, y]})
    for x, pieces in sorted(vertical.items()):
        for start, end in merge(pieces):
            walls.append({"start": [x, start], "end": [x, end]})
    walls += slanted

    # Add an id, plus the original SVG lines that lie along this wall.
    for i, wall in enumerate(walls):
        wall["id"] = "wall_" + str(i + 1)
        wall["source"] = [source for x1, y1, x2, y2, source in lines
                          if near_line(x1, y1, wall["start"], wall["end"], 1)
                          and near_line(x2, y2, wall["start"], wall["end"], 1)]

    print("Wall lines in SVG:", len(lines))
    print("Walls after merging:", len(walls))
    print("Duplicate walls removed:", len(lines) - len(walls))
    return walls

def on_a_wall(x, y, walls, tolerance=50):
    # Is the point on any wall?
    return any(near_line(x, y, wall["start"], wall["end"], tolerance) for wall in walls)

# ---------- obstacles, comms rooms, rooms ----------

def read_shapes(root, layer_name, id_prefix, extra=None):
    # Read every polygon in a layer. Give each an id and a source.
    # `extra` maps a field to an SVG attribute, e.g. {"kind": "data-kind"}.
    shapes = []
    for polygon, source in layer_items(root, layer_name):
        shape = {"id": id_prefix + str(len(shapes) + 1)}
        for field, attribute in (extra or {}).items():
            shape[field] = polygon.get(attribute)
        shape["points"] = read_points(polygon.get("points"))
        shape["source"] = source
        shapes.append(shape)
    return shapes

def read_obstacles(root):
    obstacles = read_shapes(root, "obstacles", "obstacle_", {"kind": "data-kind"})

    # Warn if the kind is missing or not one we know.
    for obstacle in obstacles:
        if obstacle["kind"] not in KNOWN_KINDS:
            warn(obstacle["id"], "has unknown kind:", obstacle["kind"], "(" + obstacle["source"] + ")")
    return obstacles

def read_comms_rooms(root):
    return read_shapes(root, "comms-room", "comms_")

def remove_duplicate_rooms(root, obstacles, comms_rooms):
    # Shapes we already have. Corners are sorted so the starting corner doesn't matter.
    taken = [sorted(thing["points"]) for thing in obstacles + comms_rooms]

    items = layer_items(root, "rooms")
    rooms = []
    for polygon, source in items:
        name = polygon.get("data-name")
        points = read_points(polygon.get("points"))

        # Skip if this shape is already an obstacle, comms room or earlier room.
        if sorted(points) in taken:
            print("Removed duplicate room:", name, "(" + source + ")")
            continue
        taken.append(sorted(points))

        # Id = room number + cleaned name (just the number if there is no name).
        number = str(len(rooms) + 1)
        if name:
            clean = "".join(c if c.isalnum() else "_" for c in name)
            room_id = "room_" + number + "_" + clean
        else:
            room_id = "room_" + number
            warn("room", number, "has no name")

        rooms.append({
            "id": room_id,
            "name": name,
            "points": points,
            "area_m2": calculate_room_area(points),
            "source": source,
        })

    print("Room polygons in SVG:", len(items))
    print("Rooms after cleaning:", len(rooms))
    print("Duplicate rooms removed:", len(items) - len(rooms))
    return rooms

def find_room(x, y, rooms):
    # Id of the smallest room holding the point, or "outside".
    found = [room for room in rooms if point_in_polygon(x, y, room["points"])]
    if not found:
        return "outside"
    return min(found, key=lambda room: room["area_m2"])["id"]

def area_of(room_id, rooms):
    # Area of a room by id. "outside" counts as the biggest space.
    for room in rooms:
        if room["id"] == room_id:
            return room["area_m2"]
    return float("inf")

# ---------- doors ----------

def door_ends(door):
    # The two ends of the door opening, on the plan.
    cx, cy = door["center"]
    half = door["width"] / 2
    return door_local_to_global(cx, cy, door["angle"], -half, 0), door_local_to_global(cx, cy, door["angle"], half, 0)

def read_doors(root, rooms):
    doors = []
    for g, source in layer_items(root, "doors"):
        cx, cy, angle = read_transform(g.get("transform"))

        # 1. Which two spaces does it join? The rooms with an edge through the door centre.
        joins = [room["id"] for room in rooms if point_on_edge(cx, cy, room["points"])]
        if len(joins) == 1:
            joins.append("outside")       # only one room touches it

        # 2. Which way does it swing? See where the end of the swing arc lands.
        swings_into = find_room(*door_local_to_global(cx, cy, angle, 450, -900), rooms)

        # 3. Exterior = swings into the bigger space (or outside), so it opens out of its room.
        larger = max(joins, key=lambda j: area_of(j, rooms)) if joins else None
        exterior = swings_into == larger

        doors.append({
            "id": "door_" + str(len(doors) + 1),
            "center": [cx, cy],
            "angle": angle,
            "width": 900,
            "joins": joins,
            "swings_into": swings_into,
            "exterior": exterior,
            "source": source,
        })

        # A door should join exactly 2 spaces.
        if len(joins) != 2:
            warn(doors[-1]["id"], "touches", len(joins), "rooms:", joins)

    print("Doors:", len(doors))
    print("Exterior doors:", sum(1 for d in doors if d["exterior"]))
    return doors

# ---------- columns ----------

def read_columns(root):
    # Each column: 4 corner points and a centre.
    columns = []
    for rect, source in layer_items(root, "columns"):
        x, y, w, h = (float(rect.get(k)) for k in ("x", "y", "width", "height"))
        columns.append({
            "id": "column_" + str(len(columns) + 1),
            "points": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
            "center": [x + w / 2, y + h / 2],
            "source": source,
        })
    print("Columns:", len(columns))
    return columns

def door_overlapping_column(doors, columns):
    # For each door, find columns sitting in its opening.
    # Overlap = column centre is closer to the opening than half the column width.
    overlaps = []
    for door in doors:
        end1, end2 = door_ends(door)
        hits = []
        for column in columns:
            xs = [p[0] for p in column["points"]]
            half_width = (max(xs) - min(xs)) / 2
            if near_line(*column["center"], end1, end2, half_width):
                hits.append(column["id"])
        if hits:
            overlaps.append({"door": door["id"], "columns": hits})
            warn(door["id"], "overlaps columns:", hits)
    if not overlaps:
        print("No doors overlap any columns")
    return overlaps

# ---------- reference APs (plan A only) ----------

def read_aps(root, rooms):
    # Read each reference AP and find its room.
    aps = []
    for circle, source in layer_items(root, "aps-reference"):
        x, y = float(circle.get("cx")), float(circle.get("cy"))
        room = find_room(x, y, rooms)
        aps.append({
            "id": "ap_" + str(len(aps) + 1),
            "position": [x, y],
            "type": circle.get("data-type"),
            "room": room,
            "source": source,
        })
        if room == "outside":
            warn(aps[-1]["id"], "is not inside any room")
    print("Reference APs:", len(aps))
    return aps

# ---------- adjacency (which rooms touch) ----------

def shared_edge(points_a, points_b, tolerance=50):
    # Longest stretch where an edge of A lies along an edge of B.
    # Returns (length, middle point), or (0, None). Horizontal and vertical edges only.
    best_len, best_mid = 0, None
    for (ax1, ay1), (ax2, ay2) in edges(points_a):
        for (bx1, by1), (bx2, by2) in edges(points_b):
            if ay1 == ay2 and by1 == by2 and abs(ay1 - by1) <= tolerance:      # both horizontal
                start, end = overlap(ax1, ax2, bx1, bx2)
                mid = ((start + end) / 2, ay1)
            elif ax1 == ax2 and bx1 == bx2 and abs(ax1 - bx1) <= tolerance:    # both vertical
                start, end = overlap(ay1, ay2, by1, by2)
                mid = (ax1, (start + end) / 2)
            else:
                continue
            if end - start > best_len:
                best_len, best_mid = end - start, mid
    return best_len, best_mid

def build_adjacency(rooms, doors, walls, min_length=100):
    adjacency = []

    # Check every pair of rooms.
    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            length, mid = shared_edge(a["points"], b["points"])
            if length < min_length:          # only a corner touch, or none
                continue

            # A door joins them -> "door". Otherwise "wall" or "open".
            if any(a["id"] in door["joins"] and b["id"] in door["joins"] for door in doors):
                via = "door"
            else:
                via = "wall" if on_a_wall(mid[0], mid[1], walls) else "open"

            adjacency.append({"a": a["id"], "b": b["id"], "via": via,
                              "shared_length_mm": length, "source": "computed"})

    # Doors to the outside.
    for door in doors:
        if "outside" in door["joins"]:
            room = [j for j in door["joins"] if j != "outside"][0]
            adjacency.append({"a": room, "b": "outside", "via": "door",
                              "shared_length_mm": door["width"], "source": "computed"})

    print("Adjacency links:", len(adjacency), count_by(adjacency, "via"))
    return adjacency

# ---------- confidence ----------

def add_confidence(data):
    # Give every object a confidence score.
    #   1.0 = read from the SVG, no doubt
    #   0.7 = read, but something looks odd
    #   0.5 = could not be placed or classified properly
    # To leave confidence out of the JSON, comment out the call in process().
    doors_on_columns = [i["door"] for i in data["overlapping_columns"]]

    for key, items in data.items():
        if key in ("dimension", "overlapping_columns"):   # not lists of objects
            continue
        for item in items:
            item["confidence"] = 1.0

    for room in data["rooms"]:
        if not room["name"]:
            room["confidence"] = 0.7                  # no name
    for door in data["doors"]:
        if door["id"] in doors_on_columns:
            door["confidence"] = 0.7                  # column in the opening
        if len(door["joins"]) != 2:
            door["confidence"] = 0.5                  # doesn't join exactly 2 spaces
    for ap in data["aps_reference"]:
        if ap["room"] == "outside":
            ap["confidence"] = 0.5                    # not inside any room
    for obstacle in data["obstacles"]:
        if obstacle["kind"] not in KNOWN_KINDS:
            obstacle["confidence"] = 0.5              # unknown kind

# ---------- report ----------

def print_report(name, root, data):
    # One summary per plan: counts, merged duplicates, things to check.
    walls, rooms, doors = data["walls"], data["rooms"], data["doors"]

    line_count = len(get_layer(root, "walls"))
    kept = [room["source"] for room in rooms]
    removed = [polygon.get("data-name") for polygon, source in layer_items(root, "rooms")
               if source not in kept]
    unknown = [g.get("data-layer") for g in root.iter()
               if g.get("data-layer") and g.get("data-layer") not in KNOWN_LAYERS]
    low = [item["id"] for key, items in data.items() if key != "dimension" for item in items
           if "id" in item and item.get("confidence", 1.0) < 1.0]

    print()
    print("=" * 40)
    print("Report:", name)
    print("=" * 40)
    print("Objects")
    print("  walls          ", len(walls), "  (from", line_count, "SVG lines)")
    print("  rooms          ", len(rooms))
    print("  doors          ", len(doors), "  (exterior:", sum(1 for d in doors if d["exterior"]), ")")
    print("  columns        ", len(data["columns"]))
    print("  obstacles      ", len(data["obstacles"]))
    print("  comms rooms    ", len(data["comms_rooms"]))
    print("  reference APs  ", len(data["aps_reference"]))
    print("  adjacency links", len(data["adjacency"]), " ", count_by(data["adjacency"], "via"))
    print("Duplicates merged")
    print("  wall lines     ", line_count - len(walls))
    print("  room polygons  ", len(removed), " ", removed)
    print("Could not classify")
    print("  unknown layers ", len(unknown), " ", unknown)
    unknown_kinds = [o["id"] for o in data["obstacles"] if o["kind"] not in KNOWN_KINDS]
    print("  unknown kinds  ", len(unknown_kinds), " ", unknown_kinds)
    print("Needs review     ", len(warnings))
    for message in warnings:
        print("  -", message)
    print("Low confidence   ", len(low), " ", low)
    print()

# ---------- saving ----------

def save_json(path, data):
    # Write JSON by hand so each item is on its own line.
    parts = []
    for key, items in data.items():
        if key == "dimension":                        # one object (or null), not a list
            parts.append('"dimension": ' + json.dumps(items))
            continue
        lines = ",\n".join("  " + json.dumps(item) for item in items)
        parts.append('"' + key + '": [\n' + lines + "\n]")
    with open(path, "w") as out:
        out.write("{\n" + ",\n".join(parts) + "\n}\n")

# ---------- main program ----------

def process(name):
    # Read one plan, clean it, print the report and save the JSON.
    if name.endswith(".svg"):
        name = name[:-4]
    root = ET.parse(name + ".svg").getroot()
    warnings.clear()                        # fresh warnings for each plan

    # Read and clean everything.
    walls = remove_duplicate_walls(root)
    obstacles = read_obstacles(root)
    comms_rooms = read_comms_rooms(root)
    rooms = remove_duplicate_rooms(root, obstacles, comms_rooms)
    doors = read_doors(root, rooms)
    columns = read_columns(root)
    aps = read_aps(root, rooms)
    adjacency = build_adjacency(rooms, doors, walls)
    overlapping_columns = door_overlapping_column(doors, columns)
    dimension = load_dimension(root)

    data = {
        "dimension": dimension,
        "walls": walls,
        "rooms": rooms,
        "doors": doors,
        "columns": columns,
        "obstacles": obstacles,
        "comms_rooms": comms_rooms,
        "aps_reference": aps,
        "adjacency": adjacency,
        "overlapping_columns": overlapping_columns,
    }
    add_confidence(data)                    # comment out to skip confidence
    print_report(name, root, data)

    # Save everything to one JSON file.
    save_json(f"{name}_floor.json", data)
    print(f"Saved to {name}_floor.json")

# Run on the files given on the command line, e.g.  python reader.py planA.svg planB.svg
# If none are given, ask for one.
if __name__ == "__main__":
    files = sys.argv[1:] or [input("SVG file name: ")]
    for file_name in files:
        process(file_name)
