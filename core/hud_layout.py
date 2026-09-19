"""Logical-pixel HUD geometry, independent of Qt and the monitor's DPI.

The caller passes its *workspace*, not the desktop. System monitors, chat,
headers and footers belong to the outer layout and cannot intersect this area.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    def intersects(self, other):
        return (self.x < other.x + other.width and other.x < self.x + self.width
                and self.y < other.y + other.height and other.y < self.y + self.height)


def activity_layout(width, height, anchor="topright", size=1.0, obstacles=()):
    """Return a contained dock and disjoint task workspace; shrink rather than clip.

    Obstacle edges are candidate origins, so floating elements can reserve space
    without introducing screen-specific constants. The primary HUD is never
    allowed to retain a large centered listening indicator during an activity.
    """
    width, height = max(1, width), max(1, height)
    margin = min(12, width / 20, height / 20)
    side = min(190 * max(.55, min(1.6, size)), width - 2 * margin, height * .34)
    while side >= 8:
        left, right = margin, width - margin - side
        candidates = [(right if anchor == "topright" else left, margin),
                      (left if anchor == "topright" else right, margin)]
        for obstacle in obstacles:
            candidates.extend([(obstacle.x + obstacle.width + margin, margin),
                               (right, obstacle.y + obstacle.height + margin)])
        for x, y in candidates:
            dock = Rect(x, y, side, side)
            if (x >= margin and y >= margin and x + side <= width - margin
                    and y + side <= height * .55
                    and not any(dock.intersects(o) for o in obstacles)):
                top = y + side + margin
                return dock, Rect(margin, top, width - 2 * margin,
                                  max(1, height - top - margin))
        side *= .85
    # Degenerate surface: report no dock instead of painting through an obstacle.
    return Rect(0, 0, 0, 0), Rect(margin, margin, width - 2 * margin, height - 2 * margin)


def fit_window(window, area):
    """Fit a persisted logical-pixel rectangle onto any (also negative) work area."""
    width = max(1, min(window.width, area.width))
    height = max(1, min(window.height, area.height))
    return Rect(max(area.x, min(window.x, area.x + area.width - width)),
                max(area.y, min(window.y, area.y + area.height - height)), width, height)


def valid_placement(value):
    """Untrusted config must not produce huge windows or invalid Qt integers."""
    if not isinstance(value, dict):
        return None
    keys = ("x", "y", "width", "height")
    if any(type(value.get(key)) is not int for key in keys):
        return None
    if not (-1_000_000 <= value["x"] <= 1_000_000 and -1_000_000 <= value["y"] <= 1_000_000
            and 100 <= value["width"] <= 4000 and 100 <= value["height"] <= 4000):
        return None
    return {key: value[key] for key in keys}
