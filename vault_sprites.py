"""Bubble images for the vault graph, drawn with Pillow.

Tk's canvas has no anti-aliasing, gradients or transparency, so each bubble
is rendered once as a smooth RGBA image (3x supersampled, then reduced) and
moved around as a canvas image item. Images are cached by everything that
changes how they look (size, CPU ring in 5 % steps, dimmed, focused), so a
refresh redraws only the bubbles whose look actually changed.
"""

from __future__ import annotations

import math
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFilter

SS = 3                       # supersampling factor
BG = (30, 30, 30)            # the graph background (#1e1e1e)
ACCENT = (168, 130, 255)     # Obsidian purple
SATELLITE = (154, 134, 214)
CLAUDE = (217, 119, 87)
EVENT = (251, 70, 76)

RES_COLORS = {
    "cpu": (122, 167, 255), "ram": (255, 169, 92), "disk": (90, 209, 154),
    "net": (95, 208, 224), "temp": (255, 107, 107),
}


def heat(cpu: float) -> tuple[int, int, int]:
    """CPU % of the machine -> ring colour: violet, amber, then red."""
    t = min(1.0, cpu / 18)
    c1, c2, c3 = ACCENT, (255, 170, 80), (255, 80, 80)
    a, b, u = (c1, c2, t * 2) if t < 0.5 else (c2, c3, (t - 0.5) * 2)
    return tuple(round(a[i] + (b[i] - a[i]) * u) for i in range(3))


def hex_of(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def blend(rgb, alpha: float, bg=BG) -> str:
    """A colour as it would look at `alpha` over the background (Tk has no alpha)."""
    return hex_of(round(bg[i] + (rgb[i] - bg[i]) * alpha) for i in range(3))


class _Canvas:
    """A supersampled RGBA drawing surface centred on the bubble."""

    def __init__(self, half: float) -> None:
        self.half = half
        self.size = max(4, int(math.ceil(half * 2)))
        self.img = Image.new("RGBA", (self.size * SS, self.size * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.c = self.size * SS / 2

    def box(self, r: float):
        c, r = self.c, r * SS
        return [c - r, c - r, c + r, c + r]

    def circle(self, r, fill=None, outline=None, width=1):
        self.d.ellipse(self.box(r), fill=fill, outline=outline, width=max(1, int(width * SS)))

    def arc(self, r, frac, rgb, width):
        if frac <= 0.01:
            return
        self.d.arc(self.box(r), -90, -90 + 360 * min(1.0, frac), fill=rgb + (255,),
                   width=max(1, int(width * SS)))

    def dashed(self, r, rgb, width, dash_deg=14):
        for start in range(0, 360, dash_deg * 2):
            self.d.arc(self.box(r), start, start + dash_deg, fill=rgb, width=max(1, int(width * SS)))

    def polygon(self, pts, fill=None, outline=None, width=1):
        c = self.c
        p = [(c + x * SS, c + y * SS) for x, y in pts]
        self.d.polygon(p, fill=fill)
        if outline:
            self.d.line(p + [p[0]], fill=outline, width=max(1, int(width * SS)), joint="curve")

    def sphere(self, r, base, light):
        """Radial gradient lit from the top-left: concentric discs, base to light."""
        steps = max(8, int(r))
        for i in range(steps):
            t = i / (steps - 1)
            rr = r * (1 - 0.82 * t)
            col = tuple(round(base[k] + (light[k] - base[k]) * t ** 1.6) for k in range(3))
            off = r * 0.36 * t
            cx = cy = self.c - off * SS
            self.d.ellipse([cx - rr * SS, cy - rr * SS, cx + rr * SS, cy + rr * SS], fill=col + (255,))

    def glow(self, r, rgb, strength=0.35):
        layer = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse(self.box(r), fill=rgb + (int(255 * strength),))
        layer = layer.filter(ImageFilter.GaussianBlur(radius=r * SS * 0.35))
        self.img = Image.alpha_composite(layer, self.img)
        self.d = ImageDraw.Draw(self.img)

    def done(self, dim: bool):
        img = self.img.resize((self.size, self.size), Image.LANCZOS)
        if dim:
            alpha = img.getchannel("A").point(lambda v: int(v * 0.22))
            img.putalpha(alpha)
        return img


def _hex_pts(r):
    return [(r * math.cos(math.pi / 6 + i * math.pi / 3), r * math.sin(math.pi / 6 + i * math.pi / 3))
            for i in range(6)]


def _star_pts(r):
    return [((r if i % 2 == 0 else r * 0.42) * math.cos(-math.pi / 2 + i * math.pi / 8),
             (r if i % 2 == 0 else r * 0.42) * math.sin(-math.pi / 2 + i * math.pi / 8))
            for i in range(16)]


def render(kind: str, r: float, k: float, *, cpu: float = 0.0, count: int = 1,
           frac: float = 0.0, res: str = "", is_self: bool = False,
           dim: bool = False, focus: bool = False) -> Image.Image:
    """One bubble as a PIL RGBA image. `r` is the radius in screen pixels and
    `k` the zoom (used for ring widths and gaps)."""
    s = max(0.6, k) ** 0.5
    half = r + 22 * max(0.7, k) + 4
    cv = _Canvas(half)
    if kind == "pc":
        cv.glow(r + 10 * k, ACCENT, 0.30)
        cv.sphere(r, (91, 74, 143), (214, 199, 255))
        cv.circle(r + 4 * k, outline=ACCENT + (255,), width=1.5)
    elif kind == "res":
        col = RES_COLORS.get(res, (180, 180, 180))
        cv.polygon(_hex_pts(r), fill=(35, 42, 51, 255), outline=col + (255,), width=1.6)
        cv.circle(r + 7 * k, outline=(51, 51, 51, 255), width=3.5 * s)
        cv.arc(r + 7 * k, frac, col, 3.5 * s)
    elif kind == "prog":
        hot = heat(cpu)
        if cpu >= 8:
            cv.glow(r + 8 * k, hot, 0.32)
        if is_self:
            cv.sphere(r, (60, 51, 87), (196, 178, 248))
        else:
            cv.sphere(r, (52, 52, 56), (176, 176, 182))
        if count > 1:
            orbit, n = r + 6 * k, min(count, 36)
            dot = 1.7 * s
            for i in range(n):
                a = i / n * 2 * math.pi
                x, y = orbit * math.cos(a), orbit * math.sin(a)
                c = cv.c
                cv.d.ellipse([c + (x - dot) * SS, c + (y - dot) * SS, c + (x + dot) * SS, c + (y + dot) * SS],
                             fill=SATELLITE + (255,))
        ring_r = r + (11 if count > 1 else 5) * k
        cv.arc(ring_r, cpu / 20, hot, 3 * s)
        if is_self:
            cv.dashed(r + 3 * k, ACCENT + (255,), 1.2)
    elif kind == "ghost":
        cv.circle(r, fill=(140, 140, 140, 34))
        cv.dashed(r, (150, 150, 150, 255), 1.3)
    elif kind == "event":
        cv.glow(r + 4 * k, EVENT, 0.35)
        cv.polygon([(0, -r), (r, 0), (0, r), (-r, 0)], fill=EVENT + (255,))
    elif kind == "claude":
        cv.glow(r + 6 * k, CLAUDE, 0.30)
        cv.polygon(_star_pts(r), fill=CLAUDE + (255,))
    if focus:
        extra = 15 if kind == "prog" and count > 1 else 9
        cv.circle(r + extra * k, outline=(255, 255, 255, 235), width=1.5)
    return cv.done(dim)


class SpriteCache:
    """LRU of Tk PhotoImages keyed by look. Needs a Tk root to exist."""

    def __init__(self, limit: int = 400) -> None:
        self.limit = limit
        self._cache: OrderedDict = OrderedDict()

    def get(self, kind: str, r: float, k: float, **kw):
        from PIL import ImageTk
        if kind == "prog":
            # 24 steps over the ring's 0-20 % range: a busy program's bubble is
            # re-rendered only when its ring visibly moves.
            kw["cpu"] = round(min(kw.get("cpu", 0.0), 20.0) * 1.2) / 1.2
        key = (kind, round(r), round(k * 20), kw.get("cpu", 0) if kind == "prog" else 0,
               min(kw.get("count", 1), 36), round(kw.get("frac", 0) * 40), kw.get("res", ""),
               kw.get("is_self", False), kw.get("dim", False), kw.get("focus", False))
        img = self._cache.get(key)
        if img is None:
            img = ImageTk.PhotoImage(render(kind, round(r), k, **kw))
            self._cache[key] = img
            if len(self._cache) > self.limit:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return img
