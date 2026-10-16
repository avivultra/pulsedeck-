"""Tests for the vault graph model, layout and bubble images (no window)."""
from __future__ import annotations

import math
import unittest
from types import SimpleNamespace as P

from vault_graph import (
    Layout,
    ProgramGroup,
    build_graph,
    group_programs,
    pad_of,
    pick_programs,
)

GB = 1024 ** 3
MB = 1024 ** 2


def proc(pid, name, cpu=0.0, rss=100 * MB):
    return P(pid=pid, name=name, cpu_percent=cpu, rss_bytes=rss)


class GroupProgramsTest(unittest.TestCase):
    def setUp(self):
        # explorer(1) -> chrome(10) -> chrome(11, 12); Code(20) -> node(30); our own pid 99
        self.parents = {10: 1, 11: 10, 12: 10, 20: 1, 30: 20, 99: 20, 1: 0}
        self.names = {1: "explorer.exe", 10: "chrome.exe", 11: "chrome.exe", 12: "chrome.exe",
                      20: "Code.exe", 30: "node.exe", 99: "python.exe"}
        self.procs = [proc(1, "explorer.exe"), proc(10, "chrome.exe", 5, GB), proc(11, "chrome.exe", 3, GB),
                      proc(12, "chrome.exe", 1, GB), proc(20, "Code.exe"), proc(30, "node.exe"),
                      proc(99, "python.exe", 0.5, 50 * MB), proc(0, "System Idle Process", 90)]

    def groups(self):
        return {g.name: g for g in group_programs(self.procs, self.parents.get, self.names.get, 99)}

    def test_same_name_processes_fold_with_one_root(self):
        g = self.groups()["chrome"]
        self.assertEqual((g.count, g.cpu, g.rss), (3, 9.0, 3 * GB))
        self.assertEqual(g.root_pids, [10])
        self.assertEqual(g.parent_name, "explorer")

    def test_parent_family_and_self_and_hidden(self):
        gs = self.groups()
        self.assertEqual(gs["node"].parent_name, "Code")
        self.assertTrue(gs["PulseDeck"].is_self)
        self.assertNotIn("python", gs)                  # our own process is PulseDeck, not "python"
        self.assertNotIn("System Idle Process", gs)


class PickProgramsTest(unittest.TestCase):
    def test_heaviest_plus_busy_plus_self_and_tiny_idle_dropped(self):
        groups = [ProgramGroup(f"p{i}", 0.0, (100 - i) * MB, 1) for i in range(30)]
        groups.append(ProgramGroup("busy", 12.0, 1 * MB, 1))
        groups.append(ProgramGroup("tiny", 0.0, 1 * MB, 1))
        groups.append(ProgramGroup("PulseDeck", 0.3, 40 * MB, 1, is_self=True))
        names = [g.name for g in pick_programs(groups, limit=5)]
        self.assertEqual(names[:5], ["p0", "p1", "p2", "p3", "p4"])
        self.assertIn("busy", names)
        self.assertIn("PulseDeck", names)
        self.assertNotIn("tiny", names)


def _graph():
    chrome = ProgramGroup("chrome", 18.0, 3 * GB, 24, parent_name="explorer")
    explorer = ProgramGroup("explorer", 1.0, 200 * MB, 1)
    resources = [{"id": "cpu", "label": "CPU", "value": 34, "text": "34%"},
                 {"id": "ram", "label": "RAM", "value": 71, "text": "71%"}]
    ghosts = [{"name": "node", "pid": 7, "rss": 100 * MB, "idle": "3h", "parent": "explorer"}]
    events = [{"time": "14:02", "reason": "CPU 87%", "suspects": ["chrome.exe"], "res": "cpu"}]
    return build_graph([chrome, explorer], resources, ghosts, events, "PC")


class BuildGraphTest(unittest.TestCase):
    def test_every_bubble_is_something_real_and_linked(self):
        nodes, links = _graph()
        kinds = {n.kind for n in nodes.values()}
        self.assertEqual(kinds, {"pc", "res", "prog", "ghost", "event", "claude"})
        pairs = {(l.a, l.b, l.type) for l in links}
        self.assertIn(("prog:chrome", "res:ram", "res"), pairs)
        self.assertIn(("prog:chrome", "res:cpu", "res"), pairs)        # busy → linked to CPU
        self.assertNotIn(("prog:explorer", "res:cpu", "res"), pairs)   # idle → not
        self.assertIn(("prog:explorer", "prog:chrome", "tree"), pairs)
        self.assertIn(("ev:14:02", "prog:chrome", "event"), pairs)
        self.assertIn(("prog:explorer", "ghost:7", "ghost"), pairs)
        self.assertGreater(nodes["prog:chrome"].r, nodes["prog:explorer"].r)   # size = memory


class LayoutTest(unittest.TestCase):
    def test_settles_without_overlaps_and_goes_to_sleep(self):
        layout = Layout()
        layout.update(*_graph())
        layout.settle(2000)
        self.assertTrue(layout.asleep)
        vis = layout.visible_nodes()
        for i, a in enumerate(vis):
            for b in vis[i + 1:]:
                d = math.hypot(a.x - b.x, a.y - b.y)
                self.assertGreater(d, pad_of(a) + pad_of(b), (a.key, b.key))

    def test_positions_survive_a_rebuild_and_new_nodes_wake_it(self):
        layout = Layout()
        layout.update(*_graph())
        layout.settle(2000)
        x = layout.nodes["prog:chrome"].x
        nodes, links = _graph()
        self.assertFalse(layout.update(nodes, links))      # same set of bubbles
        self.assertEqual(layout.nodes["prog:chrome"].x, x)
        self.assertTrue(layout.asleep)
        nodes, links = _graph()
        nodes.pop("ghost:7")
        links = [l for l in links if l.b != "ghost:7"]
        self.assertTrue(layout.update(nodes, links))
        self.assertFalse(layout.asleep)

    def test_hidden_kinds_leave_the_layout(self):
        layout = Layout()
        layout.update(*_graph())
        layout.hidden_kinds = {"ghost", "event"}
        kinds = {n.kind for n in layout.visible_nodes()}
        self.assertNotIn("ghost", kinds)
        self.assertTrue(all(layout.nodes[l.a].kind not in ("ghost", "event") and
                            layout.nodes[l.b].kind not in ("ghost", "event") for l in layout.visible_links()))


class SpritesTest(unittest.TestCase):
    def test_every_kind_renders_with_transparency(self):
        from vault_sprites import render
        for kind, kw in (("pc", {}), ("res", {"frac": .5, "res": "cpu"}), ("prog", {"cpu": 18, "count": 24}),
                         ("prog", {"is_self": True}), ("ghost", {}), ("event", {}), ("claude", {}),
                         ("prog", {"dim": True, "focus": True})):
            img = render(kind, 20, 1.0, **kw)
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.getpixel((0, 0))[3], 0, kind)          # corners transparent
            cx = img.width // 2
            self.assertGreater(img.getpixel((cx, cx))[3], 0, kind)      # centre painted


class RenderSendsOnlyChangesTest(unittest.TestCase):
    """The window repaints 30 times a second while the graph moves; every Tk
    call it can skip is CPU saved. A fake canvas counts the calls (no window)."""

    def _window(self):
        from unittest import mock
        import vault_window
        w = vault_window.VaultWindow.__new__(vault_window.VaultWindow)
        w.canvas = mock.Mock()
        w._drawn = {}
        return w

    def test_moves_below_half_a_pixel_are_not_sent(self):
        w = self._window()
        w._move(7, 100.0, 50.0)
        w._move(7, 100.3, 50.2)
        w._move(7, 100.6, 50.0)
        self.assertEqual([c.args for c in w.canvas.coords.call_args_list],
                         [(7, 100.0, 50.0), (7, 100.6, 50.0)])

    def test_only_changed_options_are_sent(self):
        w = self._window()
        w._cfg(7, text="chrome", fill="#aaa", state="normal")
        w._cfg(7, text="chrome", fill="#aaa", state="normal")
        w._cfg(7, text="chrome", fill="#fff", state="normal")
        self.assertEqual([c.kwargs for c in w.canvas.itemconfigure.call_args_list],
                         [{"text": "chrome", "fill": "#aaa", "state": "normal"}, {"fill": "#fff"}])

    def test_a_reheat_settles_in_seconds_not_tens_of_seconds(self):
        # 2 steps a frame at 30 fps = 60 steps a second.
        layout = Layout()
        nodes, links = build_graph([ProgramGroup("a", 0, GB, 1)], [], [], [])
        layout.update(nodes, links)
        layout.settle(5000)
        layout.heat(0.3)
        steps = 0
        while not layout.asleep:
            layout.step()
            steps += 1
        self.assertLess(steps / 60, 6.0)


if __name__ == "__main__":
    unittest.main()
