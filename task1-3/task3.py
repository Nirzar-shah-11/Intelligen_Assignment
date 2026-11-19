import json
import math
import os
import random
import sys
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union
import resvg_py

import task2

MIN_AREA = 100e6     # 100 m2 (in mm2)
MAX_AREA = 250e6     # 250 m2
MAX_SIDE = 24000     # 24 m
BORDER = 3000        # 3 m grey border in each picture
MM_PER_PX = 50       # 1 pixel = 5 cm in every picture
MIN_CUT_MARGIN = 3000   # ignore column lines within 3 m of a room edge
DEFAULT_SECTION_SIZE = 12000  # section size when a room has no columns


# ---------- 1. split the floor into parts ----------


def is_room_too_big(shape):
    # A room is too big if its area or its longest side is over the limit.
    x0, y0, x1, y1 = shape.bounds
    return shape.area > MAX_AREA or max(x1 - x0, y1 - y0) > MAX_SIDE


def get_cut_positions(column_values, low, high):
    # Cut along column lines (not too close to the room edges). No columns: cut every 12 m.
    lines = sorted({v for v in column_values if low + MIN_CUT_MARGIN <= v <= high - MIN_CUT_MARGIN})
    if not lines:
        n = math.ceil((high - low) / DEFAULT_SECTION_SIZE)
        lines = [low + (high - low) * i / n for i in range(1, n)]
    return [low] + lines + [high]


def cut_room_into_sections(room, floor):
    # Cut a big room into a grid of sections along its column lines.
    shape = Polygon(room["points"])
    x0, y0, x1, y1 = shape.bounds
    cols = [c["center"] for c in floor["columns"] if shape.covers(Point(c["center"]))]
    xs = get_cut_positions([c[0] for c in cols], x0, x1)
    ys = get_cut_positions([c[1] for c in cols], y0, y1)
    sections = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            section = shape.intersection(box(xs[i], ys[j], xs[i + 1], ys[j + 1]))
            if section.area > 1e6:          # ignore tiny slivers
                sections.append({"rooms": [room["id"]], "shape": section})
    return sections


def can_merge_parts(a, b):
    # Merge only if they share at least 1 m of edge, stay within the limits and stay (almost) a rectangle.
    if a["shape"].intersection(b["shape"]).length < 1000:
        return False
    both = unary_union([a["shape"], b["shape"]])
    x0, y0, x1, y1 = both.bounds
    return (both.area <= MAX_AREA and max(x1 - x0, y1 - y0) <= MAX_SIDE
            and both.area >= 0.85 * (x1 - x0) * (y1 - y0))


def merge_score(a, b):
    # Lower is better: same room first, then smaller result, then the most square one.
    both = unary_union([a["shape"], b["shape"]])
    x0, y0, x1, y1 = both.bounds
    return (a["rooms"] != b["rooms"], round(both.area / 1e6), max(x1 - x0, y1 - y0))


def split_floor_into_parts(floor):
    # One part per room; big rooms are cut into sections.
    parts = []
    for room in floor["rooms"]:
        shape = Polygon(room["points"])
        parts += cut_room_into_sections(room, floor) if is_room_too_big(shape) else [{"rooms": [room["id"]], "shape": shape}]

    # Merge the smallest part with a neighbour, again and again, until no small part can grow.
    while True:
        small = sorted([p for p in parts if p["shape"].area < MIN_AREA], key=lambda p: p["shape"].area)
        pair = next(((p, q) for p in small for q in parts if q is not p and can_merge_parts(p, q)), None)
        if pair is None:
            break
        p = pair[0]
        candidates = [q for q in parts if q is not p and can_merge_parts(p, q)]
        q = min(candidates, key=lambda q: merge_score(p, q))
        q["shape"] = unary_union([p["shape"], q["shape"]])
        q["rooms"] = sorted(set(p["rooms"]) | set(q["rooms"]))
        parts.remove(p)

    # Number the parts top to bottom, left to right. Store IDs, not copies.
    parts.sort(key=lambda p: (p["shape"].bounds[1], p["shape"].bounds[0]))
    for n, p in enumerate(parts):
        near = p["shape"].buffer(1)     # tiny buffer so touching items count
        p["id"] = "part_" + str(n + 1)
        p["box"] = list(p["shape"].bounds)
        p["area_m2"] = round(p["shape"].area / 1e6, 1)
        p["doors"] = [d["id"] for d in floor["doors"] if near.covers(Point(d["center"]))]
        p["columns"] = [c["id"] for c in floor["columns"] if near.intersects(Polygon(c["points"]))]
        p["obstacles"] = [o["id"] for o in floor["obstacles"] if near.intersects(Polygon(o["points"]))]
    return parts


# ---------- 2. text description of a part ----------

def compass_direction(dx, dy):
    # Turn an offset into a compass word. North = top of the plan (y grows downwards).
    ns = "north" if dy < -1000 else "south" if dy > 1000 else ""
    ew = "west" if dx < -1000 else "east" if dx > 1000 else ""
    return (ns + "-" + ew).strip("-") or "here"


def describe_part(part, floor, other_aps):
    # Build the text note that goes with the picture: size, walls, neighbours, comms room, obstacles, nearby APs.
    rooms = {r["id"]: dict(r, name=r["name"] or r["id"]) for r in floor["rooms"]}   # unnamed rooms use their id
    x0, y0, x1, y1 = part["box"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    lines = ["Size " + str(round((x1 - x0) / 1000, 1)) + " m x " + str(round((y1 - y0) / 1000, 1)) + " m. Rooms: "
             + ", ".join(rooms[r]["name"] for r in part["rooms"]) + "."]

    # Outside walls: edges that lie on the building's outer edge.
    bx0, by0, bx1, by1 = unary_union([Polygon(r["points"]) for r in floor["rooms"]]).bounds
    outside = [s for s, edge in (("north", y0 <= by0), ("south", y1 >= by1), ("west", x0 <= bx0), ("east", x1 >= bx1)) if edge]
    if outside:
        lines.append("Outside wall: " + ", ".join(outside) + ".")

    # Neighbours, from the adjacency list in floor.json (only rooms that touch this part).
    for a in floor["adjacency"]:
        for mine, other in ((a["a"], a["b"]), (a["b"], a["a"])):
            if mine in part["rooms"] and other in rooms and other not in part["rooms"]:
                shape = Polygon(rooms[other]["points"])
                if part["shape"].distance(shape) < 1:
                    c = shape.intersection(part["shape"].buffer(1)).centroid
                    via = a["via"]
                    if via == "door" and not any(set(d["joins"]) == {mine, other} and d["id"] in part["doors"]
                                                 for d in floor["doors"]):
                        via = "wall"                         # the door between these rooms is in another part
                    lines.append(via.capitalize() + " to " + rooms[other]["name"]
                                 + " (" + compass_direction(c.x - cx, c.y - cy) + ").")

    # A bay of a big room: the rest of that room is open around it.
    for r in part["rooms"]:
        if Polygon(rooms[r]["points"]).area > part["shape"].area + 1e6:
            lines.append("Open to the rest of " + rooms[r]["name"] + ".")

    # Comms room: direction and straight distance from the middle.
    for c in floor["comms_rooms"]:
        p = Polygon(c["points"])
        lines.append("Comms room " + str(round(p.distance(Point(cx, cy)) / 1000)) + " m to the "
                     + compass_direction(p.centroid.x - cx, p.centroid.y - cy) + ".")

    # Obstacles in or within 5 m of the part.
    for o in floor["obstacles"]:
        d = part["shape"].distance(Polygon(o["points"]))
        if d <= 5000:
            lines.append(o["kind"].capitalize() + (" inside this part." if d == 0 else " " + str(round(d / 1000, 1)) + " m away."))

    # APs in the grey margin: they already cover the edge of this part (0-1000 picture grid).
    for a in other_aps:
        if 0 < part["shape"].distance(Point(a["position"])) <= BORDER:
            u, v = mm_to_grid(part, a["position"])
            lines.append("AP just outside at (" + str(u) + ", " + str(v) + ").")

    lines = list(dict.fromkeys(lines))      # a merged part can list the same neighbour twice
    lines.append("Rules: every point within 12 m of an AP (+4 m per wall crossed, 0 through doors); "
                 "APs at least 6 m apart; at least 0.6 m from walls and columns (wall-mounted APs: columns only); "
                 "no AP inside an obstacle; each long side of an obstacle over 3 m needs an AP within 5 m.")
    return "\n".join(lines)


# ---------- 3. picture and the 0-1000 grid ----------

def get_picture_view(part):
    # The picture shows the part plus the grey border: (left, top, width, height) in mm.
    x0, y0, x1, y1 = part["box"]
    return x0 - BORDER, y0 - BORDER, x1 - x0 + 2 * BORDER, y1 - y0 + 2 * BORDER


def mm_to_grid(part, position):
    # mm on the plan -> 0-1000 grid over the picture (0,0 = top-left, 1000,1000 = bottom-right).
    vx, vy, vw, vh = get_picture_view(part)
    return round((position[0] - vx) / vw * 1000), round((position[1] - vy) / vh * 1000)


def grid_to_mm_transform(part):
    # Exact way back from the grid to mm, saved in meta.
    vx, vy, vw, vh = get_picture_view(part)
    return {"x_mm": "origin_x + u * mm_per_unit_x", "y_mm": "origin_y + v * mm_per_unit_y",
            "origin_x": vx, "origin_y": vy, "mm_per_unit_x": vw / 1000, "mm_per_unit_y": vh / 1000}


def draw_part_image(part, floor, path, aps=()):
    # Draw the part as an SVG (rooms, obstacles, walls, doors, optional APs) and save it as PNG.
    vx, vy, vw, vh = get_picture_view(part)
    x0, y0, x1, y1 = part["box"]

    def polygon_svg(points, fill):
        return '<polygon points="' + " ".join(f"{x},{y}" for x, y in points) + f'" fill="{fill}"/>'

    def line_svg(a, b, color, width):
        return f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="{color}" stroke-width="{width}"/>'

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx} {vy} {vw} {vh}" '
           f'width="{vw / MM_PER_PX:.0f}" height="{vh / MM_PER_PX:.0f}">',
           f'<rect x="{vx}" y="{vy}" width="{vw}" height="{vh}" fill="#9a9a9a"/>']   # outside the building: grey
    svg += [polygon_svg(r["points"], "#ffffff") for r in floor["rooms"]]                # inside: white
    svg += [polygon_svg(o["points"], "#F2FB00") for o in floor["obstacles"]]
    svg += [polygon_svg(c["points"], "#f4b183") for c in floor["comms_rooms"]]
    svg += [polygon_svg(c["points"], "#000444") for c in floor["columns"]]
    svg += [line_svg(w["start"], w["end"], "#FF00AE", 150) for w in floor["walls"]]
    for d in floor["doors"]:                                       # doors as green gaps
        a = math.radians(d["angle"])
        dx, dy = d["width"] / 2 * math.cos(a), d["width"] / 2 * math.sin(a)
        cx, cy = d["center"]
        svg.append(line_svg((cx - dx, cy - dy), (cx + dx, cy + dy), "#2e9e4f", 200))
    for a in aps:                                                  # APs as blue dots (correctness only)
        svg.append(f'<circle cx="{a["position"][0]}" cy="{a["position"][1]}" r="350" fill="#1f6feb"/>')
    # Light shade over the border strips, so the plan around the part stays readable
    # (rooms stay lighter than the outside), plus a dashed outline of the part itself.
    for x, y, w, h in [(vx, vy, vw, BORDER), (vx, y1, vw, BORDER), (vx, y0, BORDER, y1 - y0), (x1, y0, BORDER, y1 - y0)]:
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#000000" fill-opacity="0.25"/>')
    svg.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="none" '
               f'stroke="#000000" stroke-width="60" stroke-dasharray="400 300"/>')
    svg.append("</svg>")
    with open(path, "wb") as f:
        f.write(bytes(resvg_py.svg_to_bytes(svg_string="".join(svg))))


# ---------- 4. answers ----------

def assign_aps_to_parts(parts, aps):
    # Each AP belongs to one part: the smallest part it stands in (matters on borders).
    owner = {}
    for a in aps:
        holders = [p for p in parts if p["shape"].buffer(1).covers(Point(a["position"]))]
        if holders:
            owner[a["id"]] = min(holders, key=lambda p: p["area_m2"])["id"]
    return owner


def aps_to_grid_list(part, aps):
    # APs as {x, y, type} on the 0-1000 grid: the format the model answers in.
    return [{"x": mm_to_grid(part, a["position"])[0], "y": mm_to_grid(part, a["position"])[1],
             "type": a.get("type", "ceiling")} for a in aps]


def make_broken_layout(aps, part, mine, kind, rng):
    # Make a wrong layout on purpose: move, add or remove one AP in this part.
    x0, y0, x1, y1 = part["box"]
    aps = [dict(a) for a in aps]
    if kind == "removed" and mine:
        aps = [a for a in aps if a["id"] != mine[0]["id"]]
    elif kind == "moved" and mine:
        a = next(a for a in aps if a["id"] == mine[0]["id"])
        a["position"] = [a["position"][0] + rng.choice([-1, 1]) * rng.uniform(2000, 4000),
                         a["position"][1] + rng.choice([-1, 1]) * rng.uniform(2000, 4000)]
    else:
        kind = "added"      # also the fallback when the part has no AP to move or remove
        # Pick a spot inside the part and at least 1.5 m from every AP, so the new AP is
        # visible as its own dot (dots are 0.7 m wide) and does not hide an existing one.
        spot = None
        for _ in range(200):
            p = (rng.uniform(x0, x1), rng.uniform(y0, y1))
            if part["shape"].covers(Point(p)) and all(math.dist(p, a["position"]) >= 1500 for a in aps):
                spot = p
                break
        if spot is None:                                  # very crowded part: take any spot inside it
            spot = (rng.uniform(x0, x1), rng.uniform(y0, y1))
        aps.append({"id": "ap_added", "type": "ceiling", "position": list(spot)})
    return aps, kind


def find_problems_in_part(floor, grid, aps, part, owner):
    # Run the Task 2 rules engine on the whole floor; keep the problems that touch this part.
    found = task2.check_all(floor, task2.plan_cables(floor, aps), grid)
    mine = {a["id"] for a in aps if owner.get(a["id"]) == part["id"]}
    keep = []
    inside = lambda p: part["shape"].covers(Point(p))
    for v in found:
        if v["rule"] in ("capacity", "cable"):
            # Floor-wide rules: they depend on every AP on the floor and on routes across it,
            # which one part cannot show. The rules engine checks them on the whole floor.
            continue
        if v["rule"] == "coverage" and v.get("spots"):
            # Report only the uncovered area that lies in this part (the room may span several parts).
            n = sum(1 for p in v["spots"] if inside(p))
            if n:
                keep.append({"rule": "coverage", "detail": v["detail"].split(":")[0] + ": "
                             + str(round(n * task2.grid_size ** 2 / 1e6, 2)) + " m2 not covered in this part"})
            continue
        at = v.get("at")
        if set(v["ap_ids"]) & mine or (at and inside(at)):
            # Say which APs break the rule, in the same 0-1000 coordinates as the question,
            # so an answer can be checked AP by AP (not just by its wording).
            by_id = {x["id"]: x for x in aps}
            where = [mm_to_grid(part, by_id[i]["position"]) for i in v["ap_ids"] if i in by_id]
            keep.append({"rule": v["rule"], "aps": where, "detail": v["detail"]})
    return keep


# ---------- 5. export the dataset ----------

SYSTEM = ("You help plan Wi-Fi access points (APs) on office floor plans. The image shows one part of a floor; "
          "the grey margin is outside the part. Coordinates are on a 0-1000 grid over the image, "
          "(0,0) top-left. Answer only with JSON.")
ASK_PLACE = "Where should the APs go in this part? Answer as a JSON list of {x, y, type}."
ASK_CHECK = "Is this AP layout correct? Answer with a JSON list of rule violations, or [] if none."


def build_message(text, image=None):
    # One chat message: optional image first, then the text.
    return ([{"type": "image", "image": image}] if image else []) + [{"type": "text", "text": text}]


def build_training_example(image, user_text, answer, meta):
    # One training example: system prompt, user question (+ image), assistant answer.
    return {"messages": [{"role": "system", "content": build_message(SYSTEM)},
                         {"role": "user", "content": build_message(user_text, image)},
                         {"role": "assistant", "content": build_message(json.dumps(answer))}],
            "meta": meta}


def export_dataset(floor_path, aps_path=None, out_dir="dataset"):
    with open(floor_path) as f:
        floor = json.load(f)
    plan = os.path.basename(floor_path).removesuffix(".json").removesuffix("_floor")   # "l_shape_floor.json" -> "l_shape"

    # Where the APs come from: approved in Part 4 > engineer (plan A) > placer file > run the placer.
    reviewed = floor_path.replace(".json", "_reviewed.json")
    if os.path.exists(reviewed):
        with open(reviewed) as f:
            aps, source = json.load(f)["aps"], "reviewed"
    elif floor["aps_reference"]:
        aps, source = floor["aps_reference"], "engineer"
    elif aps_path:
        with open(aps_path) as f:
            aps, source = json.load(f)["aps"], "placer"
    else:
        aps, source = task2.run_plan(floor_path)["aps"], "placer"

    os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
    parts = split_floor_into_parts(floor)
    owner = assign_aps_to_parts(parts, aps)
    grid = task2.build_grid(floor)
    rng = random.Random(plan)                    # same broken layouts every run

    with open(os.path.join(out_dir, plan + ".jsonl"), "w") as out:
        for k, part in enumerate(parts):
            name = plan + "_" + part["id"]
            mine = [a for a in aps if owner.get(a["id"]) == part["id"]]
            others = [a for a in aps if owner.get(a["id"]) != part["id"]]
            text = describe_part(part, floor, others)
            meta = {"plan": plan, "part": part["id"], "source": source, "transform": grid_to_mm_transform(part)}

            # Placement question: picture without APs -> the APs in this part.
            image = "images/" + name + ".png"
            draw_part_image(part, floor, os.path.join(out_dir, image))
            out.write(json.dumps(build_training_example(image, text + "\n" + ASK_PLACE, aps_to_grid_list(part, mine),
                                                        dict(meta, question="placement"))) + "\n")

            # Correctness question: the real layout, and one layout broken on purpose.
            kind = ["moved", "added", "removed"][k % 3]
            for layout, label in [(aps, "unchanged"), make_broken_layout(aps, part, mine, kind, rng)]:
                layout_owner = assign_aps_to_parts(parts, layout)
                shown = [a for a in layout if layout_owner.get(a["id"]) == part["id"]]
                image = "images/" + name + "_" + label + ".png"
                draw_part_image(part, floor, os.path.join(out_dir, image), layout)
                user = text + "\nAPs in this part: " + json.dumps(aps_to_grid_list(part, shown)) + "\n" + ASK_CHECK
                answer = find_problems_in_part(floor, grid, layout, part, layout_owner)
                out.write(json.dumps(build_training_example(image, user, answer,
                                                            dict(meta, question="correctness", layout=label))) + "\n")

    # Save the part list (without the shapely objects) so other steps can reuse it.
    with open(os.path.join(out_dir, plan + "_parts.json"), "w") as f:
        json.dump([{k: v for k, v in p.items() if k != "shape"} for p in parts], f, indent=2)
    print(plan, ":", len(parts), "parts,", 3 * len(parts), "examples, APs from", source)


if __name__ == "__main__":
    export_dataset(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)