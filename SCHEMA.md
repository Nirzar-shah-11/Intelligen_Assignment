# floor.json schema

This is what the reader writes for each plan. One file per plan, called `<plan>_floor.json`.

All coordinates are in millimetres, same as the SVG: origin top-left, y pointing down. Points are written as `[x, y]`. Areas are in m².

Each section is a list, and each item is on its own line so the file is easy to diff and read.

## General rules

- Every physical object appears once. If the SVG draws something twice, the reader keeps one copy and reports the duplicate.
- Ids are built from the order things appear in the SVG, so running the reader again on the same file gives the same ids.
- `source` says where the object came from in the SVG, as `layer/element[n]`, counting from 1. For example `rooms/polygon[3]` is the third polygon in the rooms layer. Things the reader works out itself (like adjacency) have `"source": "computed"`.
- Areas, door connections and adjacency are always calculated from the geometry. Nothing is typed in by hand.

## walls

The wall centrelines after merging. The SVG draws a wall once per room outline, so a shared wall can appear several times or in overlapping pieces. These are joined into one line.

| field | meaning |
|---|---|
| id | `wall_1`, `wall_2`, ... |
| start, end | the two ends of the wall |
| source | list of all the SVG lines that were merged into this wall |

Only horizontal and vertical walls are merged. Slanted walls are kept as they are.

Example: on plan A the wall at y = 6000 is drawn 8 times in the SVG and becomes one wall from x = 0 to x = 60000, with 8 entries in `source`.

## rooms

Spaces people use. Polygons in the rooms layer that have the same shape as an obstacle or a comms room are dropped here, because they are the same physical thing (for example "Lift Core" in plan A).

| field | meaning |
|---|---|
| id | room number + name, e.g. `room_1_Office_N1`. Just `room_3` if the room has no name |
| name | the `data-name` from the SVG, can be null |
| points | the corners of the room |
| area_m2 | calculated with the shoelace formula, so it works for L-shaped rooms too |
| source | e.g. `rooms/polygon[1]` |

Note: the id is set once by the reader. If someone renames a room later, the id should stay the same.

## doors

| field | meaning |
|---|---|
| id | `door_1`, `door_2`, ... |
| center | middle of the door opening, from the `translate(...)` |
| angle | rotation in degrees, from the `rotate(...)` |
| width | always 900 mm |
| joins | the two spaces the door connects. One of them can be `"outside"` |
| swings_into | the space the door opens into |
| exterior | true if the door joins a room to the outside of the building |
| source | e.g. `doors/g[4]` |

How `joins` is found: the door centre sits on a wall, so the reader looks for rooms whose edge passes through it. Two rooms means it joins them. One room means the other side is outside.

How `swings_into` is found: the swing arc ends 900 mm off the wall. The reader checks which room that point lands in.

## columns

| field | meaning |
|---|---|
| id | `column_1`, ... |
| points | the 4 corners |
| center | middle of the column |
| source | e.g. `columns/rect[7]` |

## obstacles

| field | meaning |
|---|---|
| id | `obstacle_1`, ... |
| kind | from `data-kind`: lift-core, stair-core, plant or rack |
| points | the corners |
| source | e.g. `obstacles/polygon[2]` |

## comms_rooms

| field | meaning |
|---|---|
| id | `comms_1`, ... |
| points | the corners |
| source | e.g. `comms-room/polygon[1]` |

## aps_reference

Only filled for plan A. These are the engineer's real AP positions, copied as they are. The reader doesn't move or fix them.

| field | meaning |
|---|---|
| id | `ap_1`, ... |
| position | centre of the circle |
| type | `ceiling` or `wall` |
| room | id of the room the AP is in. If it's inside more than one room, the smallest one. `"outside"` if none |
| source | e.g. `aps-reference/circle[37]` |

## adjacency

Which rooms are next to each other. Worked out by the reader, not read from the SVG.

| field | meaning |
|---|---|
| a, b | the two room ids. `b` can be `"outside"` for a door to the outside |
| via | `door`, `wall` or `open` |
| shared_length_mm | how long the shared edge is |
| source | always `"computed"` |

How `via` is decided:

1. If a door joins the two rooms, it's `door`.
2. Otherwise, if a wall runs along the shared edge, it's `wall`.
3. Otherwise it's `open` (the rooms touch with nothing between them).

Rooms that only touch at a corner (shared edge under 100 mm) are not counted.

## overlapping_columns

Doors that have a column sitting in their opening. This is a warning, not something that should happen in a real building. Plan A has two.

| field | meaning |
|---|---|
| door | the door id |
| columns | ids of the columns in the opening |

## Tolerances

- 50 mm when checking if a point is on a wall or edge (walls are drawn 150 mm thick).
- 1 mm when matching original wall lines to merged walls.
- 100 mm minimum shared edge for two rooms to count as neighbours.
- Duplicate shapes are matched by exact corner coordinates.
