"""Generate assets/logo.svg: python3 assets/make_logo.py assets/logo.svg"""
import math, sys
C = "#D97757"

def bez(p0, p1, p2, p3, t):
    u = 1 - t
    return tuple(u**3*a + 3*u*u*t*b + 3*u*t*t*c + t**3*d for a, b, c, d in zip(p0, p1, p2, p3))

def stroke(points, widths):
    """Variable-width stroke with round ends, as one filled path."""
    left, right = [], []
    n = len(points)
    for i, (p, w) in enumerate(zip(points, widths)):
        q0, q1 = points[max(i - 1, 0)], points[min(i + 1, n - 1)]
        dx, dy = q1[0] - q0[0], q1[1] - q0[1]
        L = math.hypot(dx, dy) or 1
        nx, ny = -dy / L * w / 2, dx / L * w / 2
        left.append((p[0] + nx, p[1] + ny)); right.append((p[0] - nx, p[1] - ny))
    pts = left + right[::-1]
    d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in pts) + "Z"
    caps = "".join(f'<circle cx="{p[0]:.2f}" cy="{p[1]:.2f}" r="{w/2:.2f}"/>'
                   for p, w in ((points[0], widths[0]), (points[-1], widths[-1])))
    return f'<path d="{d}"/>' + caps

def curve(p0, p1, p2, p3, wmax, wend, n=24):
    pts = [bez(p0, p1, p2, p3, i / n) for i in range(n + 1)]
    ws = [wend + (wmax - wend) * math.sin(math.pi * i / n) ** 0.8 for i in range(n + 1)]
    return stroke(pts, ws)

def ray(cx, cy, ang, r0, r1, w0, w1, n=12):
    pts = [(cx + r * math.cos(ang), cy + r * math.sin(ang)) for r in (r0 + (r1 - r0) * i / n for i in range(n + 1))]
    ws = [w0 + (w1 - w0) * (i / n) ** 0.7 for i in range(n + 1)]
    return stroke(pts, ws)

# brain: overlapping lobes, nudged so the two halves aren't mirror images
lobes_l = [(88,84,33),(62,112,29),(58,146,26),(78,174,25),(108,184,24),(114,68,25),(96,128,36),(104,158,32),(110,100,32),(74,92,22)]
nudge = [(1,-2,1),(2,1,-1),(-1,2,1),(1,-1,0),(-2,1,1),(0,1,-1),(0,0,0),(1,0,0),(0,-1,1),(1,1,0)]
circ = []
for (x, y, r), (dx, dy, dr) in zip(lobes_l, nudge):
    circ.append(f'<circle cx="{x}" cy="{y}" r="{r}"/>')
    circ.append(f'<circle cx="{256-x+dx}" cy="{y+dy}" r="{r+dr}"/>')

cuts = []
# midline, tapering towards the spark
cuts.append(stroke([(128, 34), (128, 66), (128.5, 94)], [2, 6, 3.5]))
cuts.append(stroke([(128.5, 166), (128, 190), (128, 222)], [3.5, 6, 2]))
# folds (left, then a slightly different right)
folds = [((72,96),(84,98),(92,108),(91,124)), ((54,137),(68,134),(82,140),(87,155)), ((81,191),(84,179),(95,172),(108,174))]
for f in folds:
    cuts.append(curve(*f, 7.5, 4.5))
for i, f in enumerate(folds):
    j = [(1,1),(0,2),(-1,1)][i]
    cuts.append(curve(*[(256 - x + j[0], y + j[1]) for x, y in f], 7.2, 4.5))
# spark: uneven lengths and angles, rays fine at the centre and full at the tip
cx, cy = 128, 130
lens = [32, 22, 29, 24, 31, 20, 30, 23, 27, 25]
jit = [0.00, 0.07, -0.05, 0.04, -0.03, 0.08, 0.02, -0.06, 0.05, -0.04]
for i, (L, j) in enumerate(zip(lens, jit)):
    a = 2 * math.pi * i / len(lens) - math.pi / 2 + j
    cuts.append(ray(cx, cy, a, 0, L, 4.5, 10.5 if L > 26 else 9.5))
cuts.append(f'<circle cx="{cx}" cy="{cy}" r="6"/>')

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="10 26 236 200" width="256" height="217" role="img" aria-label="collaude">
  <defs>
    <mask id="cuts" maskUnits="userSpaceOnUse" x="0" y="0" width="256" height="256">
      <rect width="256" height="256" fill="#fff"/>
      <g fill="#000">{"".join(cuts)}</g>
    </mask>
  </defs>
  <g fill="{C}" mask="url(#cuts)">{"".join(circ)}</g>
</svg>
'''
open(sys.argv[1], "w").write(svg)
