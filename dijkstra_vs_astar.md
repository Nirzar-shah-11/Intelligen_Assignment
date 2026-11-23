# Dijkstra vs A* Benchmark — planA_floor

| Algorithm | Dijkstra time (ms) | A\* time (ms) | Dijkstra energy (J) | A\* energy (J) | Recommended |
|---|---:|---:|---:|---:|:---:|
| find_shortest_cable_any_angle | 13.16 | **7.88** | 0.168 | **0.089** | A\* |
| find_shortest_cable_right_angles | 20.37 | **7.57** | 0.243 | **0.088** | A\* |
| plan_cables | 26.38 | **8.00** | 0.345 | **0.093** | A\* |
| compute_signal_distances | **19.13** | 72.38 | **0.229** | 0.742 | Dijkstra |
| check_coverage | **21.35** | 75.59 | **0.276** | 0.788 | Dijkstra |
| place_aps | **2,549** | 2,657 | 30.83 | **28.79** | Dijkstra |

