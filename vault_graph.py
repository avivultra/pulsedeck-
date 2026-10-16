"""The graph behind the vault window: what the bubbles are and how they move.

Pure Python, no Tk: `build_graph` turns the live state of the computer into
nodes and links, and `Layout` is a small force simulation that places them and
goes to sleep once they settle (so an idle window costs nothing).

Every node is something real on the computer:
  pc     — the computer itself (centre)
  res    — a resource: CPU, memory, disk, network, temperature
  prog   — a program; same-name processes folded together (size = memory)
  ghost  — an abandoned process found by the Ghost Sweeper
  event  — a load event from today's spike report, linked to its suspect
  claude — the Ask-Claude button, as a bubble
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# Never shown as programs: they are the OS's own bookkeeping, not an answer to
# "who is using the computer".
HIDDEN_NAMES = {"system idle process", "idle", "system", "registry", "memory compression",
                "secure system", "kthreadd"}
MAX_PROGRAMS = 14


@dataclass
class ProgramGroup:
    name: str
    cpu: float                 # % of the whole machine
    rss: int
    count: int
    pids: list[int] = field(default_factory=list)
    root_pids: list[int] = field(default_factory=list)   # members whose parent is not the same name
    parent_name: str | None = None
    is_self: bool = False


def group_programs(procs, parent_of, name_of, self_pid: int) -> list[ProgramGroup]:
    """Fold sampler ProcessInfo rows into ProgramGroups.

    `parent_of(pid) -> ppid | None` and `name_of(pid) -> str | None` are
    lookups (cached by the caller). The window's own process becomes a
    separate "PulseDeck" group so the monitor's cost is visible on its own.
    """
    groups: dict[str, ProgramGroup] = {}
    for p in procs:
        low = p.name.lower()
        if low in HIDDEN_NAMES:
            continue
        if p.pid == self_pid:
            key, display, is_self = "\0pulsedeck", "PulseDeck", True
        else:
            key, display, is_self = low, _display_name(p.name), False
        g = groups.get(key)
        if g is None:
            g = groups[key] = ProgramGroup(display, 0.0, 0, 0, is_self=is_self)
        g.cpu += p.cpu_percent
        g.rss += p.rss_bytes
        g.count += 1
        g.pids.append(p.pid)
    for g in groups.values():
        members = set(g.pids)
        for pid in g.pids:
            ppid = parent_of(pid)
            if ppid is None or ppid not in members:
                g.root_pids.append(pid)
        # The family's parent: the parent of its first root, if that is a
        # different program.
        for pid in g.root_pids:
            ppid = parent_of(pid)
            pname = name_of(ppid) if ppid else None
            if pname and _display_name(pname) != g.name:
                g.parent_name = _display_name(pname)
                break
    return list(groups.values())


def _display_name(name: str) -> str:
    return name[:-4] if name.lower().endswith(".exe") else name


MIN_RSS = 8 * 1024 ** 2      # smaller and idle: not worth a bubble


def pick_programs(groups: list[ProgramGroup], limit: int = MAX_PROGRAMS) -> list[ProgramGroup]:
    """The heaviest by memory, plus anything busy on the CPU, plus PulseDeck."""
    by_mem = sorted((g for g in groups if g.rss >= MIN_RSS or g.cpu >= 2.0),
                    key=lambda g: g.rss, reverse=True)
    chosen = by_mem[:limit]
    names = {g.name for g in chosen}
    for g in sorted(groups, key=lambda g: g.cpu, reverse=True):
        if g.cpu >= 2.0 and g.name not in names:
            chosen.append(g)
            names.add(g.name)
    for g in groups:
        if g.is_self and g.name not in names:
            chosen.append(g)
    return chosen


@dataclass
class Node:
    key: str
    kind: str
    label: str
    data: dict
    r: float
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    pinned: bool = False


@dataclass
class Link:
    a: str
    b: str
    type: str          # core / res / tree / ghost / event
    w: float = 1.0


def radius_for(rss: int) -> float:
    gb = rss / 1024 ** 3
    return 9 + math.sqrt(max(gb, 0.0)) * 17


def build_graph(programs: list[ProgramGroup], resources: list[dict], ghosts: list[dict],
                events: list[dict], pc_label: str = "") -> tuple[dict[str, Node], list[Link]]:
    """Nodes keyed by a stable id (so positions survive refreshes) and links.

    resources: {"id", "label", "value" (0-100 for the gauge), "text", "sub"}
    ghosts:    {"name", "pid", "rss", "idle", "parent"}
    events:    {"time", "reason", "suspects": [names], "res": "cpu"|"ram"}
    """
    nodes: dict[str, Node] = {}
    links: list[Link] = []

    def add(key, kind, label, data, r):
        nodes[key] = Node(key, kind, label, data, r)

    add("pc", "pc", pc_label, {}, 26)
    for r in resources:
        add("res:" + r["id"], "res", r["label"], r, 19)
        links.append(Link("pc", "res:" + r["id"], "core"))
    add("claude", "claude", "Claude", {}, 13)
    links.append(Link("pc", "claude", "core", 0.6))

    prog_names = {g.name for g in programs}
    for g in programs:
        key = "prog:" + g.name
        add(key, "prog", g.name, {"group": g}, radius_for(g.rss))
        if "res:ram" in nodes:
            links.append(Link(key, "res:ram", "res", min(1.0, g.rss / (2 * 1024 ** 3))))
        if g.cpu >= 2.0 and "res:cpu" in nodes:
            links.append(Link(key, "res:cpu", "res", min(1.0, g.cpu / 15)))
    for g in programs:
        if g.parent_name and g.parent_name in prog_names:
            links.append(Link("prog:" + g.parent_name, "prog:" + g.name, "tree", 0.5))

    for gh in ghosts:
        key = f"ghost:{gh['pid']}"
        add(key, "ghost", gh["name"], gh, radius_for(gh.get("rss", 0)) * 0.85)
        parent = gh.get("parent")
        if parent and parent in prog_names:
            links.append(Link("prog:" + parent, key, "ghost", 0.5))
        else:
            links.append(Link("pc", key, "ghost", 0.3))

    for ev in events:
        key = "ev:" + ev["time"]
        if key in nodes:
            continue
        add(key, "event", ev["time"], ev, 8)
        linked = False
        for s in ev.get("suspects", [])[:2]:
            name = _display_name(s)
            if name in prog_names:
                links.append(Link(key, "prog:" + name, "event"))
                linked = True
        res = ev.get("res")
        if res and "res:" + res in nodes:
            links.append(Link(key, "res:" + res, "event", 0.5))
            linked = True
        if not linked:
            links.append(Link(key, "pc", "event", 0.3))
    return nodes, links


# ---------------------------------------------------------------- layout

REST = {"core": 150, "res": 190, "tree": 110, "ghost": 80, "event": 70}
SLEEP_BELOW = 0.003
# Per step. 0.985 settles a reheat in ~4 s of animation; the old 0.992 crept
# for ~20 s, and the graph changes more often than that, so it never rested.
ALPHA_DECAY = 0.985


def pad_of(n: Node) -> float:
    """Radius plus room for rings, satellites and labels."""
    if n.kind == "prog" and n.data["group"].count > 1:
        return n.r + 14
    return n.r + (11 if n.kind == "res" else 6)


class Layout:
    """Force-directed placement that keeps positions across rebuilds."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.links: list[Link] = []
        self.alpha = 1.0
        self.hidden_kinds: set[str] = set()
        self.hidden_links: set[str] = set()

    @property
    def asleep(self) -> bool:
        return self.alpha <= SLEEP_BELOW

    def heat(self, alpha: float) -> None:
        self.alpha = max(self.alpha, alpha)

    def update(self, nodes: dict[str, Node], links: list[Link]) -> bool:
        """Swap in a fresh graph, keeping old positions. True if the set of
        nodes changed (then the layout is reheated a little)."""
        changed = set(nodes) != set(self.nodes)
        rnd = random.Random(len(nodes))
        for key, n in nodes.items():
            old = self.nodes.get(key)
            if old is not None:
                n.x, n.y, n.vx, n.vy, n.pinned = old.x, old.y, old.vx, old.vy, old.pinned
            else:
                anchor = self._anchor_for(n, nodes, links)
                n.x = anchor[0] + rnd.uniform(-40, 40)
                n.y = anchor[1] + rnd.uniform(-40, 40)
        if not self.nodes:      # first build: start from a spiral, resources on a ring
            self._seed(nodes)
        self.nodes, self.links = nodes, links
        if changed:
            self.heat(0.3 if self.nodes else 1.0)
        return changed

    def _anchor_for(self, n: Node, nodes, links) -> tuple[float, float]:
        for link in links:
            other = link.b if link.a == n.key else link.a if link.b == n.key else None
            if other and other in self.nodes:
                o = self.nodes[other]
                return o.x, o.y
        return 0.0, 0.0

    def _seed(self, nodes: dict[str, Node]) -> None:
        res = [n for n in nodes.values() if n.kind == "res"]
        for i, n in enumerate(nodes.values()):
            a, rr = i * 2.399963, 40 + 14 * math.sqrt(i)
            n.x, n.y = math.cos(a) * rr * 2.2, math.sin(a) * rr * 1.6
        for i, n in enumerate(res):
            a = -math.pi / 2 + i * 2 * math.pi / max(1, len(res))
            n.x, n.y = math.cos(a) * 140, math.sin(a) * 140
        if "pc" in nodes:
            nodes["pc"].x = nodes["pc"].y = 0.0

    def visible_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind not in self.hidden_kinds]

    def visible_links(self) -> list[Link]:
        out = []
        for link in self.links:
            if link.type in self.hidden_links:
                continue
            a, b = self.nodes.get(link.a), self.nodes.get(link.b)
            if a is None or b is None or a.kind in self.hidden_kinds or b.kind in self.hidden_kinds:
                continue
            out.append(link)
        return out

    def step(self) -> None:
        alpha = self.alpha
        vis = self.visible_nodes()
        for i, a in enumerate(vis):
            for b in vis[i + 1:]:
                dx, dy = b.x - a.x, b.y - a.y
                d2 = dx * dx + dy * dy or 0.01
                d = math.sqrt(d2)
                f = 5200 / d2 * alpha
                fx, fy = dx / d * f, dy / d * f
                a.vx -= fx
                a.vy -= fy
                b.vx += fx
                b.vy += fy
        for link in self.visible_links():
            a, b = self.nodes[link.a], self.nodes[link.b]
            dx, dy = b.x - a.x, b.y - a.y
            d = math.hypot(dx, dy) or 0.01
            rest = REST[link.type] - (link.w * 50 if link.type == "res" else 0) + (a.r + b.r) * 0.3
            k = (0.012 + link.w * 0.03 if link.type == "res" else 0.04) * alpha
            f = (d - rest) * k
            fx, fy = dx / d * f, dy / d * f
            a.vx += fx
            a.vy += fy
            b.vx -= fx
            b.vy -= fy
        for n in vis:
            n.vx -= n.x * 0.004 * alpha
            n.vy -= n.y * 0.004 * alpha
            if n.pinned:
                n.vx = n.vy = 0.0
                continue
            n.vx *= 0.82
            n.vy *= 0.82
            n.x += n.vx
            n.y += n.vy
        # Hard collision pass: bubbles, with their rings and labels, never overlap.
        for i, a in enumerate(vis):
            for b in vis[i + 1:]:
                dx, dy = b.x - a.x, b.y - a.y
                d = math.hypot(dx, dy) or 0.01
                min_d = pad_of(a) + pad_of(b) + 14
                if d < min_d:
                    push, ux, uy = (min_d - d) / 2, dx / d, dy / d
                    if not a.pinned:
                        a.x -= ux * push
                        a.y -= uy * push
                    if not b.pinned:
                        b.x += ux * push
                        b.y += uy * push
        pc = self.nodes.get("pc")
        if pc is not None and not pc.pinned:
            pc.x *= 0.9
            pc.y *= 0.9
        self.alpha = max(0.0, self.alpha * ALPHA_DECAY - 0.0004)

    def settle(self, steps: int = 600) -> None:
        for _ in range(steps):
            self.step()
            if self.asleep:
                break

    def bounds(self) -> tuple[float, float, float, float]:
        vis = self.visible_nodes() or [Node("pc", "pc", "", {}, 26)]
        return (min(n.x - pad_of(n) for n in vis), min(n.y - pad_of(n) for n in vis),
                max(n.x + pad_of(n) for n in vis), max(n.y + pad_of(n) + 18 for n in vis))
