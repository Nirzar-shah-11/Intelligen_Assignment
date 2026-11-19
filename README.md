## How to run

```bash
# Run from root directory (Intelligen_Assignment/)

python -m venv venv
source venv.bin/activate
pip install -r requirements.txt
python task1-3/task1.py data/*.svg                                         # -> data/<plan>_floor.json + report per plan
python task1-3/task2.py data/*_floor.json                                  # -> data/<plan>_floor_aps.json (APs, cables, problems, comparison)
python task1-3/task3.py data/planA_floor.json                              # -> dataset/planA.jsonl, dataset/images/, dataset/planA_parts.json
python task1-3/task3.py data/planB_floor.json data/planB_floor_aps.json    # use the placer's APs saved by task2
pytest -q                                             # rules engine tests
```

## Task2 questions (Comparison with the engineers plan i.e. Plan A)

**How many of the engineer’s APs are matched, and how many of yours are unmatched?**

- Only 2 of the engineer's 37 APs match one of mine. One is in exactly the same spot and the other is 1.77 m away. The other 35 have no match.

- Out of my 17 APs, 15 have no match.


**Which rules the engineer’s layout breaks, according to your rules engine?**

- The engineer's layout breaks 3 rules, 9 problems in total.

- **Spacing (7 problems):** 7 pairs of APs are closer than the 6 m minimum. Two pairs are 4.0 m apart and five pairs are 4.8 m apart. Most are two APs in the same office.

- **Obstacle (1 problem):** no AP is within 5 m of the 9 m side of the plant room.

- **Capacity (1 problem):** all 37 APs connect to the one comms room, and the limit is 24.

**Where does your placer disagree with the engineer, and why?**

The biggest disagreement is how many APs we use and where we put them. The engineer uses 37, with an AP in the middle of every office and a regular grid in the open plan (18 APs). My placer uses 17. It leaves most of the small offices without their own AP and covers them from the open plan or the next office, and several of my APs sit near walls and corners instead of in the middle of a room. I think this happens because my rules only check that every point is within 12 m of an AP. My approach doesn't factor in the serving capacity, so the cheapest answer is to use as few APs as possible. The engineer was designing for capacity and signal quality, not just reach, so they put more APs in and centred them in each room. This is also why only 2 APs match. 

## Part 3: how each piece keeps the rest of the floor in view

**Pieces follow the building.** Each room is one piece.

- A room that is too big is cut along its column lines (the structural bays). If it has no columns, I cut it every 12 m.
- A piece smaller than 100 m² is merged with a neighbour. I only do this if the result stays under the size limit and stays close to a rectangle.
- A piece stores only ids (rooms, doors, columns, obstacles in floor.json) and its bounding box. It never copies the geometry.

**Size: 100–250 m², longest side at most 24 m.**

- 24 m is twice the 12 m coverage range. So an AP anywhere in the piece matters to the whole piece. A piece needs only a few APs (with 6 m spacing, about 1–6).
- Below about 100 m², a piece is often one small office. The answer is "one AP" or "none", which teaches the model little. So I group small rooms with their neighbours.
- At 5 cm per pixel, a 24 m piece plus its margin is about 600 px wide. A 150 mm wall is 3 px, and the 0.6 m clearance is 12 px. That is easy to see, and it fits the input size of current open vision-language models.

**The model gets context from three places.**

1. **The picture.** It has a 3 m grey margin of the plan around the piece. So walls, doors and obstacles just outside the piece are visible.
2. **The text note.** It says what the picture cannot show:
   - which neighbours are in which direction, and whether the edge to each is a door, a wall or open space;
   - whether a side is an outside wall;
   - whether the piece is a bay cut from a larger room (an open edge);
   - where the comms room is and how far away;
   - obstacles within 5 m;
   - APs already placed just outside the piece, in the same 0–1000 coordinates.
3. **The rules.** Every note states the rules. The exact transform from 0–1000 back to millimetres is stored in `meta`. So I can map the model's answers back onto the floor and check them.

**What I would not ask the model to do.** I would not trust it to decide if a layout is legal. These checks need the whole floor, and one piece cannot show them:

- Coverage is a path distance through doors and walls.
- Cable length can be up to 90 m across the floor.
- Switch capacity counts the APs on the whole floor.

My rules engine computes all of these exactly. I would also not ask the model to give millimetre-exact positions, route cables, decide which rooms need coverage, or approve a plan.

The model has one job: propose AP positions for a piece. The rules engine checks every proposal against the whole floor. An engineer approves the result.

## What I Implemented

**Parts 1–3 are done.**

## What I skipped

**Part 4 (review web app) and Part 5.** Not Implemented

## Part 8: Deliverables, timeline and assessment

**Assumptions**
- All limits are inclusive: exactly 12 m, 6 m, 0.6 m, 5 m and 90 m pass.
- Every room needs coverage except lift cores, stair cores and comms rooms.
- Coverage is checked on a 0.5 m grid. Grid paths can be slightly longer than the true path, so the check errs on the safe side.
- A wall-mounted AP's signal starts 0.2 m off its wall.
- Cables stay inside the building. They can touch an obstacle's edge but not cross it.
- My placer uses ceiling APs only.
- Plans B, C and the synthetic plans have no engineer layout, so their dataset APs come from my placer.

**Design decisions**
- One source of truth: `<plan>_floor.json` stores each object once. Areas, door connections and adjacency are computed from the geometry every run. See `SCHEMA.md`.
- Repeated rooms are dropped, and repeated wall lines are merged into one wall.
- Coverage uses the shortest path on the grid. A wall adds 4 m, a door adds 0, and obstacles and the outside can't be crossed.
- Each rule is one function in `task2.py`, and `check_all` runs them all. The same checks judge my layout and the engineer's.
- The placer is greedy and repeatable. It picks the spot that covers the most uncovered area while staying 6 m from other APs. Then it adds APs for long obstacle sides and removes any it doesn't need.
- Cabling tries right-angle routes first and uses diagonals only if every right-angle route is over 90 m. Each AP goes to the nearest comms room with fewer than 24 APs.

**Next steps**
- Build Part 4, the review app.
- Add a capacity rule (users or area per AP).
- Build ids from content, not SVG order.
- Handle layer transforms and slanted walls.
- Find outside walls from the real outline, which matters for L-shaped plans.
- Stop pieces being cut through obstacles, and use square images.


- **Hold out whole plans, not just pieces.** Include a shape the model has never seen. This stops pieces from the same floor leaking between training and test.

- **Check each piece.** Use the 0–1000 transform in `meta` to get back to millimetres. Then measure:
  - APs matched one to one within 2 m (precision and recall against the reference);
  - extra and missing APs;
  - the predicted layout run through my rules engine on the whole floor (coverage of the piece's rooms under the 12 m rule, spacing, clearance, obstacles).

- **For "is this layout correct?"** Measure precision and recall for each rule. Test on unchanged layouts and on layouts I broke on purpose.

- **Compare with a baseline.** The baseline is my heuristic placer.


I would trust the model only if, on unseen plans, it does at least as well as the placer and almost never misses a violation. Even then, its output is only a proposal. The rules engine checks it and an engineer approves it.



## How I used AI tools

- **Coding: Claude (Anthropic).** Claude was used for coding:
  - the reader, rules engine, placer and cabling, tiling and exporter;
  - the unit tests.

  It also created the five synthetic test plans in `plans/` as SVG files. These plans were created by passing the reference images to claude and getting 5/6 different floor plans as output.

- **Brainstorming: ChatGPT and Claude.** I used both to brainstorm and discuss approaches:
  - how to compute coverage distance through walls and doors;
  - how to divide a floor into pieces;
  - how to keep each piece aware of the rest of the floor.

