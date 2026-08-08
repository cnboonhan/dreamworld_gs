# common — modules ported from the dreamworld pipeline

Vendored unchanged (Apache-2.0, see ../../NOTICE.md):

| Module | Role |
| --- | --- |
| `geometry.py` | camera pose maths in the gz convention (+X forward, +Z up) |
| `png_io.py` | pure-stdlib PNG writers — no cv2 or PIL in the RMF image |

`capture.py` uses both. Nav-graph loading is deliberately *not* ported: this
repo reads its own `capture_plan.json`, which already carries the lane
endpoints in metres.
