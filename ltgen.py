#!/usr/bin/env python3
"""
LTspice 회로도(.asc) 자동 생성 + 측정값(.txt) 추출

    python ltgen.py 회로.yaml              ->  output/<프로젝트>.zip  (회로별 .asc + 측정결과.txt)
    python ltgen.py 회로.yaml --no-sim     ->  회로도만
"""
from __future__ import annotations

import argparse
import glob
import heapq
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

U = 32
INF = float("inf")
GND = "0"

# 핀 좌표/외곽선은 LTspice 기본 lib/sym/*.asy 기준
SYMBOLS: dict[str, dict] = {
    "res":      dict(pins=[(16, 16), (16, 96)], out=[(0, -1), (0, 1)], bbox=(0, 16, 32, 96), prefix="R",
                     kind="passive", win=[(36, 40), (36, 76)], mid=56),
    "cap":      dict(pins=[(16, 0), (16, 64)], out=[(0, -1), (0, 1)], bbox=(0, 0, 32, 64), prefix="C",
                     kind="passive", win=[(24, 8), (24, 56)], mid=32),
    "ind":      dict(pins=[(16, 16), (16, 96)], out=[(0, -1), (0, 1)], bbox=(0, 16, 32, 96), prefix="L",
                     kind="passive", win=[(36, 40), (36, 80)], mid=56),
    "diode":    dict(pins=[(16, 0), (16, 64)], out=[(0, -1), (0, 1)], bbox=(0, 0, 32, 64), prefix="D",
                     kind="polar", win=[(24, 0), (24, 64)], mid=32),
    "zener":    dict(pins=[(16, 0), (16, 64)], out=[(0, -1), (0, 1)], bbox=(-4, 0, 36, 64), prefix="D",
                     kind="polar", win=[(24, 0), (24, 64)], mid=32),
    "schottky": dict(pins=[(16, 0), (16, 64)], out=[(0, -1), (0, 1)], bbox=(0, 0, 32, 64), prefix="D",
                     kind="polar", win=[(24, 0), (24, 64)], mid=32),
    "LED":      dict(pins=[(16, 0), (16, 64)], out=[(0, -1), (0, 1)], bbox=(0, 0, 72, 64), prefix="D",
                     kind="polar", win=[(24, 0), (24, 64)], mid=32),
    "voltage":  dict(pins=[(0, 16), (0, 96)], out=[(0, -1), (0, 1)], bbox=(-32, 16, 32, 96), prefix="V",
                     kind="source", win=[(24, 16), (24, 96)]),
    "current":  dict(pins=[(0, 0), (0, 80)], out=[(0, -1), (0, 1)], bbox=(-32, 0, 32, 80), prefix="I",
                     kind="source", win=[(24, 0), (24, 80)]),
    "bv":       dict(pins=[(0, 16), (0, 96)], out=[(0, -1), (0, 1)], bbox=(-32, 16, 32, 96), prefix="B",
                     kind="source", win=[(24, 16), (24, 96)]),
    "npn":      dict(pins=[(64, 0), (0, 48), (64, 96)], out=[(0, -1), (-1, 0), (0, 1)], bbox=(0, 0, 64, 96),
                     prefix="Q", kind="bjt", win=[(56, 32), (56, 68)]),
    "pnp":      dict(pins=[(64, 0), (0, 48), (64, 96)], out=[(0, -1), (-1, 0), (0, 1)], bbox=(0, 0, 64, 96),
                     prefix="Q", kind="bjtp", win=[(84, 32), (84, 68)]),
    "nmos":     dict(pins=[(48, 0), (0, 80), (48, 96)], out=[(0, -1), (-1, 0), (0, 1)], bbox=(0, 0, 48, 96),
                     prefix="M", kind="mos", win=[(56, 32), (56, 72)]),
    "pmos":     dict(pins=[(48, 0), (0, 80), (48, 96)], out=[(0, -1), (-1, 0), (0, 1)], bbox=(0, 0, 48, 96),
                     prefix="M", kind="mosp", win=[(56, 32), (56, 72)]),
    "opamp":    dict(pins=[(-32, 48), (-32, 80), (32, 64)], out=[(-1, 0), (-1, 0), (1, 0)],
                     bbox=(-32, 32, 32, 96), prefix="X", kind="opamp", win=[(0, 32)], sym="Opamps\\\\opamp"),
}

ORIENTS = {
    "passive": {"D": "R0", "R": "R270", "L": "R90", "U": "R180"},
    "polar":   {"D": "R0", "R": "R270", "L": "R90", "U": "R180"},
    "source":  {"D": "R0", "U": "R180"},
    "bjt":     {"A": "R0", "B": "M0"},
    "bjtp":    {"A": "M180", "B": "R180"},
    "mos":     {"A": "R0", "B": "M0"},
    "mosp":    {"A": "M180", "B": "R180"},
    "opamp":   {"A": "R0", "B": "M180"},
}
DIR_WORDS = {"down": "D", "right": "R", "left": "L", "up": "U", "d": "D", "r": "R", "l": "L", "u": "U"}

ALIASES = {
    "r": "res", "resistor": "res", "c": "cap", "capacitor": "cap", "l": "ind", "inductor": "ind",
    "v": "voltage", "vsource": "voltage", "i": "current", "isource": "current",
    "d": "diode", "led": "LED", "q": "npn", "m": "nmos", "u": "opamp", "x": "opamp", "b": "bv",
}

OPAMP_SUB = """.subckt opamp inm inp out params: Aol=100K GBW=10Meg
G1 0 n1 inp inm 1
R1 n1 0 {Aol}
C1 n1 0 {1/(6.283185307*GBW)}
E1 out 0 n1 0 1
.ends opamp"""


@dataclass
class Comp:
    ref: str
    type: str
    nodes: list[str]
    value: str = ""
    dir: str | None = None
    at: tuple[int, int] | None = None
    keep: bool = False
    attrs: dict = field(default_factory=dict)

    @property
    def sym(self) -> dict:
        return SYMBOLS[self.type]

    @property
    def spice_name(self) -> str:
        p = self.sym["prefix"]
        return self.ref if self.ref.upper().startswith(p) else p + self.ref


def transform(pt, rot):
    x, y = pt
    if rot[0] == "M":
        x = -x
    for _ in range(int(rot[1:]) // 90 % 4):
        x, y = -y, x
    return x, y


# ---------------------------------------------------------------------------
# 사양 읽기
# ---------------------------------------------------------------------------
def load_spec(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def norm_net(n) -> str:
    s = str(n).strip()
    return GND if s.lower() in ("0", "gnd", "ground") else s


def parse_comp(item) -> Comp:
    if isinstance(item, str):
        toks = item.split()
        ref, _, t = toks[0].partition(":")
        t = t or ALIASES.get(ref[0].lower(), "")
        t = ALIASES.get(t.lower(), t)
        if t not in SYMBOLS:
            raise ValueError(f"'{item}': 소자 종류를 알 수 없습니다")
        n = len(SYMBOLS[t]["pins"])
        return Comp(ref=ref, type=t, nodes=[norm_net(x) for x in toks[1:1 + n]], value=" ".join(toks[1 + n:]))
    t = str(item.get("type") or ALIASES.get(str(item["ref"])[0].lower(), ""))
    t = ALIASES.get(t.lower(), t)
    if t not in SYMBOLS:
        raise ValueError(f"{item['ref']}: 알 수 없는 소자 type '{t}' (지원: {', '.join(SYMBOLS)})")
    d = item.get("dir")
    if d is not None:
        d = DIR_WORDS.get(str(d).lower(), str(d))
    if item.get("flip"):
        d = "B"
    extra = {k: v for k, v in item.items()
             if k not in ("ref", "type", "nodes", "value", "dir", "flip", "at", "keep")}
    return Comp(ref=str(item["ref"]), type=t, nodes=[norm_net(x) for x in item["nodes"]],
                value=str(item.get("value", "")), dir=d,
                at=tuple(item["at"]) if "at" in item else None, keep=bool(item.get("keep")), attrs=extra)


def parse_components(circ: dict) -> list[Comp]:
    comps = [parse_comp(c) for c in circ["components"]]
    for c in comps:
        if len(c.nodes) != len(c.sym["pins"]):
            raise ValueError(f"{c.ref}: '{c.type}' 는 노드 {len(c.sym['pins'])}개가 필요합니다 ({c.nodes})")
        if c.dir and c.dir not in ORIENTS[c.sym["kind"]]:
            raise ValueError(f"{c.ref}: dir '{c.dir}' 는 {c.type} 에 쓸 수 없습니다")
    return comps


def as_list(x) -> list[str]:
    if x is None:
        return []
    return [str(i) for i in x] if isinstance(x, list) else [str(x)]


# ---------------------------------------------------------------------------
# 기하 도구
# ---------------------------------------------------------------------------


def shift(r, dx, dy):
    return (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)


def on_seg(p, s):
    (x1, y1), (x2, y2) = s
    return min(x1, x2) <= p[0] <= max(x1, x2) and min(y1, y2) <= p[1] <= max(y1, y2) and \
        (x1 == x2 == p[0] or y1 == y2 == p[1])


def mst(pts):
    n = len(pts)
    used = [False] * n
    d = [INF] * n
    par = [-1] * n
    d[0] = 0
    edges = []
    for _ in range(n):
        u = min((i for i in range(n) if not used[i]), key=lambda i: d[i])
        used[u] = True
        if par[u] >= 0:
            edges.append((pts[par[u]], pts[u]))
        for v in range(n):
            if not used[v]:
                w = abs(pts[u][0] - pts[v][0]) + abs(pts[u][1] - pts[v][1])
                if w < d[v]:
                    d[v], par[v] = w, u
    return edges


# ---------------------------------------------------------------------------
# 소자 풋프린트: 회전별 핀 -> 격자 단자(짧은 리드선 포함), 외곽, 글자 영역
# ---------------------------------------------------------------------------
@dataclass
class FP:
    rot: str
    org: tuple[int, int]
    terms: list[tuple[int, int]]
    stubs: list[list[tuple[int, int]]]
    pins: list[tuple[int, int]]
    body: tuple
    labels: list[tuple]
    windows: list[str]
    tdirs: list[tuple[int, int]] = field(default_factory=list)
    segs: list[tuple] = field(default_factory=list)


def text_w(s: str) -> int:
    return 10 * len(s) + 8


def make_fp(c: Comp, okey: str) -> FP:
    sym = c.sym
    rot = ORIENTS[sym["kind"]][okey]
    pins = [transform(p, rot) for p in sym["pins"]]
    outs = [transform(d, rot) for d in sym["out"]]
    best = None
    for rx in (0, 16):
        for ry in (0, 16):
            score, terms, stubs = 0, [], []
            for (px, py), (dx, dy) in zip(pins, outs):
                x, y = px + rx, py + ry
                pts = [(x, y)]
                if (y if dx else x) % U:
                    score += 1000
                    x, y = x + dx * 16, y + dy * 16
                    pts.append((x, y))
                    if dx:
                        y -= 16
                    else:
                        x -= 16
                    pts.append((x, y))
                while (x if dx else y) % U:
                    x, y = x + dx * 16, y + dy * 16
                if (x, y) != pts[-1]:
                    pts.append((x, y))
                score += abs(x - pts[0][0]) + abs(y - pts[0][1])
                terms.append((x, y))
                stubs.append(pts if len(pts) > 1 else [])
            if best is None or score < best[0]:
                best = (score, (rx, ry), terms, stubs)
    _, org, terms, stubs = best
    b = sym["bbox"]
    c1, c2 = transform((b[0], b[1]), rot), transform((b[2], b[3]), rot)
    body = (min(c1[0], c2[0]) + org[0], min(c1[1], c2[1]) + org[1],
            max(c1[0], c2[0]) + org[0], max(c1[1], c2[1]) + org[1])
    labels, windows = label_geometry(c, okey, rot, org, body)
    segs = []
    for k, poly in enumerate(stubs):
        for a, b in zip(poly, poly[1:]):
            segs.append((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]), k))
    return FP(rot, org, terms, stubs, [(p[0] + org[0], p[1] + org[1]) for p in pins], body, labels, windows,
              outs, segs)


def label_geometry(c: Comp, okey, rot, org, body):
    sym = c.sym
    texts = [c.ref] + ([c.value] if c.value and sym["kind"] != "opamp" else [])
    cx, cy = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
    rects, windows = [], []
    if sym["kind"] in ("passive", "polar") and okey in ("R", "L"):
        mid = sym["mid"]
        win = [(0, mid, "VBottom"), (32, mid, "VTop")] if okey == "L" else [(32, mid, "VTop"), (0, mid, "VBottom")]
        for n, (wx, wy, j) in enumerate(win):
            windows.append(f"WINDOW {0 if n == 0 else 3} {wx} {wy} {j} 2")
        for (wx, wy, _), s in zip(win, texts):
            ax, ay = transform((wx, wy), rot)
            ax, ay = ax + org[0], ay + org[1]
            w = text_w(s)
            rects.append((ax - w / 2, ay - 22, ax + w / 2, ay) if ay <= cy else (ax - w / 2, ay, ax + w / 2, ay + 22))
    elif sym["kind"] == "source" and okey == "D":
        for n, ((wx, wy), s) in enumerate(zip(sym["win"], texts)):
            windows.append(f"WINDOW {0 if n == 0 else 3} {-wx} {wy} Right 2")
            ax, ay = -wx + org[0], wy + org[1]
            rects.append((ax - text_w(s), ay - 11, ax, ay + 11))
    else:
        for (wx, wy), s in zip(sym["win"], texts):
            ax, ay = transform((wx, wy), rot)
            ax, ay = ax + org[0], ay + org[1]
            w = text_w(s)
            rects.append((ax, ay - 11, ax + w, ay + 11) if ax >= cx - 1 else (ax - w, ay - 11, ax, ay + 11))
    return rects, windows


# ---------------------------------------------------------------------------
# 배치 (담금질 기법)
# ---------------------------------------------------------------------------
W_LEN, W_BEND, W_OBS, W_CROSS, W_OVER = 1.0, 2.0, 10.0, 4.0, 12.0
W_GND, W_SUP, W_AREA, W_LAB, W_UP, W_LEFT, W_SRC = 1.5, 1.0, 0.35, 4.0, 5.0, 1.5, 1.5
SHIFTS = [(1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (-2, 0), (0, 2), (0, -2), (4, 0), (-4, 0), (0, 4), (0, -4),
          (1, 1), (-1, -1), (1, -1), (-1, 1)]
OFFS = [(0, 0), (64, 0), (-64, 0), (0, 64), (0, -64), (96, 0), (-96, 0), (0, 96), (0, -96),
        (128, 0), (-128, 0), (0, 128), (0, -128)]


class Placer:
    def __init__(self, comps: list[Comp], label_nets: set[str], seed: int, supply=frozenset()):
        self.comps = comps
        self.supply = supply
        self.n = len(comps)
        self.rng = random.Random(seed)
        self.label_nets = label_nets
        self.orients, self.fps = [], []
        for c in comps:
            keys = [c.dir] if c.dir else list(ORIENTS[c.sym["kind"]])
            self.orients.append(keys)
            self.fps.append({k: make_fp(c, k) for k in keys})
        self.netpins = defaultdict(list)
        for e, c in enumerate(comps):
            for k, n in enumerate(c.nodes):
                self.netpins[n].append((e, k))
        self.free = [e for e, c in enumerate(comps) if c.at is None]
        self.nodes = [c.nodes for c in comps]
        self.kinds = [c.sym["kind"] for c in comps]
        self.sources = [e for e, c in enumerate(comps) if c.sym["kind"] == "source"]

    def term(self, st, e, k):
        i, j, o = st[e]
        t = self.fps[e][o].terms[k]
        return t[0] + i * U, t[1] + j * U

    def evaluate(self, st, active=None):
        el = range(self.n) if active is None else active
        nodes = self.nodes
        B, T, S, LB = [], [], [], []
        netpts = defaultdict(set)
        for e in el:
            i, j, o = st[e]
            fp = self.fps[e][o]
            dx, dy = i * U, j * U
            b = fp.body
            B.append((b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy, e))
            nd = nodes[e]
            for k, (tx, ty) in enumerate(fp.terms):
                T.append((tx + dx, ty + dy, nd[k], e, fp.tdirs[k]))
                netpts[nd[k]].add((tx + dx, ty + dy))
            for x1, y1, x2, y2, k in fp.segs:
                S.append((x1 + dx, y1 + dy, x2 + dx, y2 + dy, e, nd[k]))
            for L in fp.labels:
                LB.append((L[0] + dx, L[1] + dy, L[2] + dx, L[3] + dy, e))
        nb = len(B)
        for a in range(nb):
            ax0, ay0, ax1, ay1, _ = B[a]
            ax0 -= 16
            ay0 -= 16
            ax1 += 16
            ay1 += 16
            for b in range(a + 1, nb):
                bx0, by0, bx1, by1, _ = B[b]
                if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                    return INF
        pt = {}
        for x, y, n, e, d in T:
            if pt.setdefault((x, y), n) != n:
                return INF
        for x, y, n, e, d in T:
            for bx0, by0, bx1, by1, f in B:
                if bx0 < x < bx1 and by0 < y < by1:
                    return INF
            for qx, qy in ((x + d[0] * 16, y + d[1] * 16), (x + 16, y), (x - 16, y), (x, y + 16), (x, y - 16)):
                if pt.get((qx, qy), n) != n:
                    continue
                for bx0, by0, bx1, by1, f in B:
                    if bx0 <= qx <= bx1 and by0 <= qy <= by1:
                        break
                else:
                    break
            else:
                return INF
            for x1, y1, x2, y2, f, m in S:
                if f != e and x1 <= x <= x2 and y1 <= y <= y2:
                    if not (m == n and ((x == x1 and y == y1) or (x == x2 and y == y2))):
                        return INF
        for x1, y1, x2, y2, e, n in S:
            for bx0, by0, bx1, by1, f in B:
                if f != e and ((y1 == y2 and by0 < y1 < by1 and x1 < bx1 and bx0 < x2) or
                               (x1 == x2 and bx0 < x1 < bx1 and y1 < by1 and by0 < y2)):
                    return INF
        ns = len(S)
        for a in range(ns):
            x1, y1, x2, y2, e, n = S[a]
            for b in range(a + 1, ns):
                u1, v1, u2, v2, f, m = S[b]
                if e != f and n != m and x1 <= u2 and u1 <= x2 and y1 <= v2 and v1 <= y2:
                    return INF

        rowB, colB = defaultdict(list), defaultdict(list)
        for bx0, by0, bx1, by1, f in B:
            y = (int(by0) // U + 1) * U
            while y < by1:
                rowB[y].append((bx0, bx1))
                y += U
            x = (int(bx0) // U + 1) * U
            while x < bx1:
                colB[x].append((by0, by1))
                x += U

        def hh(y, xa, xb):
            if xa > xb:
                xa, xb = xb, xa
            return sum(1 for bx0, bx1 in rowB.get(y, ()) if xa < bx1 and bx0 < xb)

        def vh(x, ya, yb):
            if ya > yb:
                ya, yb = yb, ya
            return sum(1 for by0, by1 in colB.get(x, ()) if ya < by1 and by0 < yb)

        cost = 0.0
        H, V = [], []
        for n, pts in netpts.items():
            if n in self.label_nets or len(pts) < 2:
                continue
            for (ax, ay), (bx, by) in mst(list(pts)):
                cost += (abs(ax - bx) + abs(ay - by)) / U * W_LEN
                if ax == bx:
                    h = vh(ax, ay, by)
                    V.append((ax, min(ay, by), max(ay, by), n))
                elif ay == by:
                    h = hh(ay, ax, bx)
                    H.append((ay, min(ax, bx), max(ax, bx), n))
                else:
                    cost += W_BEND
                    h1 = hh(ay, ax, bx) + vh(bx, ay, by)
                    h2 = vh(ax, ay, by) + hh(by, ax, bx)
                    if h1 <= h2:
                        h = h1
                        H.append((ay, min(ax, bx), max(ax, bx), n))
                        V.append((bx, min(ay, by), max(ay, by), n))
                    else:
                        h = h2
                        V.append((ax, min(ay, by), max(ay, by), n))
                        H.append((by, min(ax, bx), max(ax, bx), n))
                cost += h * W_OBS
        for x1, y1, x2, y2, e, n in S:
            if y1 == y2:
                H.append((y1, x1, x2, n))
            else:
                V.append((x1, y1, y2, n))
        for y, xa, xb, n in H:
            for x, ya, yb, m in V:
                if n != m and xa <= x <= xb and ya <= y <= yb:
                    cost += W_CROSS if (xa < x < xb and ya < y < yb) else W_OVER
        for segs in (H, V):
            groups = defaultdict(list)
            for s_ in segs:
                groups[s_[0]].append(s_)
            for g in groups.values():
                for a in range(len(g)):
                    for b in range(a + 1, len(g)):
                        if g[a][3] != g[b][3] and g[a][1] <= g[b][2] and g[b][1] <= g[a][2]:
                            cost += W_OVER

        ymax = max(max(t[1] for t in T), max(b[3] for b in B))
        ymin = min(min(t[1] for t in T), min(b[1] for b in B))
        for x, y, n, e, d in T:
            if n == GND:
                cost += (ymax - y) / U * W_GND
            elif n in self.supply:
                cost += (y - ymin) / U * W_SUP
        xs0 = min(min(b[0] for b in B), min((L[0] for L in LB), default=INF))
        xs1 = max(max(b[2] for b in B), max((L[2] for L in LB), default=-INF))
        cost += ((xs1 - xs0) + (ymax - ymin)) / U * W_AREA
        nl = len(LB)
        for a in range(nl):
            lx0, ly0, lx1, ly1, e = LB[a]
            for bx0, by0, bx1, by1, f in B:
                if f != e and lx0 < bx1 and bx0 < lx1 and ly0 < by1 and by0 < ly1:
                    cost += W_LAB
            for b in range(a + 1, nl):
                mx0, my0, mx1, my1, f = LB[b]
                if lx0 < mx1 and mx0 < lx1 and ly0 < my1 and my0 < ly1:
                    cost += W_LAB
            for y, xa, xb, n in H:
                if ly0 < y < ly1 and xa < lx1 and lx0 < xb:
                    cost += W_LAB * 0.5
            for x, ya, yb, n in V:
                if lx0 < x < lx1 and ya < ly1 and ly0 < yb:
                    cost += W_LAB * 0.5
        bmin = {b[4]: b[0] for b in B}
        xmin = min(bmin.values())
        for e in el:
            o, kind = st[e][2], self.kinds[e]
            if kind == "passive" or kind == "polar":
                cost += W_UP if o == "U" else (W_LEFT if o == "L" else 0)
            elif kind == "source":
                cost += (bmin[e] - xmin) / U * W_SRC
        return cost

    def bfs_order(self):
        order, seen = [], set()
        starts = sorted(self.sources, key=lambda e: self.comps[e].type != "voltage") or [0]
        for s in starts + list(range(self.n)):
            if s in seen:
                continue
            q = [s]
            seen.add(s)
            while q:
                e = q.pop(0)
                order.append(e)
                for n in self.comps[e].nodes:
                    if n == GND:
                        continue
                    for f, _ in self.netpins[n]:
                        if f not in seen:
                            seen.add(f)
                            q.append(f)
        return order

    def initial(self):
        st = [None] * self.n
        placed = []
        for e, c in enumerate(self.comps):
            if c.at is not None:
                t0 = self.fps[e][self.orients[e][0]].terms[0]
                st[e] = (int(c.at[0]) - t0[0] // U, int(c.at[1]) - t0[1] // U, self.orients[e][0])
                placed.append(e)
        for e in self.bfs_order():
            if st[e] is not None:
                continue
            cands = set()
            for o in self.orients[e]:
                fp = self.fps[e][o]
                for k, te in enumerate(fp.terms):
                    for f, kk in self.netpins[self.comps[e].nodes[k]]:
                        if st[f] is None:
                            continue
                        tx, ty = self.term(st, f, kk)
                        for ox, oy in OFFS:
                            cands.add(((tx + ox - te[0]) // U, (ty + oy - te[1]) // U, o))
            if not placed:
                cands = {(0, 0, o) for o in self.orients[e]}
            if not cands:
                imax = max(st[f][0] for f in placed)
                cands = {(imax + 6, 0, o) for o in self.orients[e]}
            best, bc = None, INF
            for cand in cands:
                st[e] = cand
                v = self.evaluate(st, placed + [e])
                if v < INF:
                    v += self.rng.random() * 0.01
                if v < bc:
                    best, bc = cand, v
            if best is None:
                imax = max((st[f][0] for f in placed), default=0)
                for step in range(6, 60, 3):
                    st[e] = (imax + step, 0, self.orients[e][0])
                    if self.evaluate(st, placed + [e]) < INF:
                        best = st[e]
                        break
            st[e] = best or (0, 0, self.orients[e][0])
            placed.append(e)
        return st

    def propose(self, st):
        rng = self.rng
        if not self.free:
            return None
        e = rng.choice(self.free)
        i, j, o = st[e]
        new = list(st)
        m = rng.random()
        if m < 0.08:
            axis = rng.randrange(2)
            cut = st[e][axis]
            d = rng.choice((-2, -1, 1, 2))
            for f in self.free:
                if st[f][axis] >= cut:
                    q = list(st[f])
                    q[axis] += d
                    new[f] = tuple(q)
            return new
        if m < 0.16:
            grp = self.cluster(st, e)
            di, dj = rng.choice(SHIFTS)
            for f in grp:
                new[f] = (st[f][0] + di, st[f][1] + dj, st[f][2])
            return new
        m = (m - 0.16) / 0.84
        if m < 0.3:
            di, dj = rng.choice(SHIFTS)
            new[e] = (i + di, j + dj, o)
        elif m < 0.42:
            if len(self.orients[e]) == 1:
                return None
            new[e] = (i, j, rng.choice([k for k in self.orients[e] if k != o]))
        elif m < 0.52:
            f = rng.choice(self.free)
            if f == e:
                return None
            new[e] = (st[f][0], st[f][1], o)
            new[f] = (i, j, st[f][2])
        else:
            o2 = o if rng.random() < 0.6 else rng.choice(self.orients[e])
            fp = self.fps[e][o2]
            k = rng.randrange(len(fp.terms))
            others = [(f, kk) for f, kk in self.netpins[self.comps[e].nodes[k]] if f != e]
            if not others:
                return None
            f, kk = rng.choice(others)
            tx, ty = self.term(st, f, kk)
            te = fp.terms[k]
            if m < 0.85:
                ox, oy = rng.choice(OFFS[:9]) if rng.random() < 0.7 else rng.choice(OFFS)
                new[e] = ((tx + ox - te[0]) // U, (ty + oy - te[1]) // U, o2)
            else:
                cx, cy = te[0] + i * U, te[1] + j * U
                if rng.random() < 0.5:
                    new[e] = (i + (tx - cx) // U, j, o2)
                else:
                    new[e] = (i, j + (ty - cy) // U, o2)
        return new

    def cluster(self, st, e):
        pts = defaultdict(set)
        for f in range(self.n):
            for k, n in enumerate(self.nodes[f]):
                if n != GND:
                    pts[self.term(st, f, k)].add(f)
        grp, todo = {e}, [e]
        while todo:
            f = todo.pop()
            for k, n in enumerate(self.nodes[f]):
                if n == GND:
                    continue
                for g in pts[self.term(st, f, k)]:
                    if g not in grp and self.comps[g].at is None and len(grp) < 5:
                        grp.add(g)
                        todo.append(g)
        return grp

    def anneal(self, st, iters):
        cur = self.evaluate(st)
        best, bc = st, cur
        T0, T1 = 6.0, 0.03
        for it in range(iters):
            T = T0 * (T1 / T0) ** (it / iters)
            new = self.propose(st)
            if new is None:
                continue
            c = self.evaluate(new)
            if c == INF:
                continue
            if cur == INF or c <= cur or self.rng.random() < math.exp((cur - c) / T):
                st, cur = new, c
                if c < bc:
                    best, bc = new, c
        return best, bc


# ---------------------------------------------------------------------------
# 배선 (격자 A*)
# ---------------------------------------------------------------------------
DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def snap_lo(v):
    return int(math.floor(v / 16)) * 16


def snap_hi(v):
    return int(math.ceil(v / 16)) * 16


def line_cells(a, b):
    (x1, y1), (x2, y2) = a, b
    if x1 == x2:
        s = 16 if y2 >= y1 else -16
        return [(x1, y) for y in range(y1, y2 + s, s)]
    s = 16 if x2 >= x1 else -16
    return [(x, y1) for x in range(x1, x2 + s, s)]


class Router:
    BEND, CROSS = 4, 12

    def __init__(self, comps, geo, label_nets):
        self.comps, self.geo, self.label_nets = comps, geo, label_nets
        self.blocked = set()
        self.soft = defaultdict(float)
        self.vert = {}
        self.wire = defaultdict(dict)
        self.edges = defaultdict(set)
        self.cells = defaultdict(set)
        self.cross = set()
        self.flags = []
        self.failed = []
        self.pinpts = set()
        xs, ys = [], []
        for g in geo:
            for r in [g["body"]] + g["labels"]:
                xs += [r[0], r[2]]
                ys += [r[1], r[3]]
            for t in g["terms"]:
                xs.append(t[0])
                ys.append(t[1])
        self.bounds = (snap_lo(min(xs)) - 160, snap_lo(min(ys)) - 160, snap_hi(max(xs)) + 160, snap_hi(max(ys)) + 160)
        pinset = {}
        for e, g in enumerate(geo):
            for k, p in enumerate(g["pins"]):
                pinset[p] = comps[e].nodes[k]
        for g in geo:
            b = g["body"]
            bx0, by0, bx1, by1 = snap_lo(b[0]), snap_lo(b[1]), snap_hi(b[2]), snap_hi(b[3])
            edge_ok = set()
            for p in g["pins"]:
                if p[0] in (bx0, bx1):
                    edge_ok |= {(p[0], p[1] - 16), (p[0], p[1] + 16)}
                if p[1] in (by0, by1):
                    edge_ok |= {(p[0] - 16, p[1]), (p[0] + 16, p[1])}
            for x in range(bx0, bx1 + 1, 16):
                for y in range(by0, by1 + 1, 16):
                    if (x, y) not in pinset and (x, y) not in edge_ok:
                        self.blocked.add((x, y))
            for x in range(snap_lo(b[0]) - 16, snap_hi(b[2]) + 17, 16):
                for y in range(snap_lo(b[1]) - 16, snap_hi(b[3]) + 17, 16):
                    self.soft[(x, y)] += 1
            for r in g["labels"]:
                for x in range(snap_lo(r[0]), snap_hi(r[2]) + 1, 16):
                    for y in range(snap_lo(r[1]), snap_hi(r[3]) + 1, 16):
                        self.soft[(x, y)] += 3
        for p, n in pinset.items():
            self.mark_vertex(p, n)
            self.pinpts.add(p)
        for e, g in enumerate(geo):
            for k, poly in enumerate(g["stubs"]):
                n = comps[e].nodes[k]
                for a, b in zip(poly, poly[1:]):
                    self.add_line(n, a, b)
                for p in poly:
                    self.mark_vertex(p, n)
            for k, t in enumerate(g["terms"]):
                self.mark_vertex(t, comps[e].nodes[k])
                self.cells[comps[e].nodes[k]].add(t)

    def mark_vertex(self, c, n):
        if self.vert.get(c, n) != n:
            raise RuntimeError("vertex conflict")
        self.vert[c] = n
        self.cells[n].add(c)

    def add_line(self, n, a, b):
        cells = line_cells(a, b)
        o = "V" if a[0] == b[0] else "H"
        for c1, c2 in zip(cells, cells[1:]):
            self.edges[n].add((min(c1, c2), max(c1, c2)))
        for c in cells:
            self.wire[c].setdefault(n, set()).add(o)
            self.cells[n].add(c)

    def groups(self, n):
        parent = {c: c for c in self.cells[n]}

        def find(c):
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c
        for a, b in self.edges[n]:
            parent[find(a)] = find(b)
        g = defaultdict(set)
        for c in self.cells[n]:
            g[find(c)].add(c)
        return list(g.values())

    def astar(self, n, sources, targets):
        tset = {c for c in targets if c not in self.cross}
        if not tset:
            return None
        tl = list(tset)
        x0, y0, x1, y1 = self.bounds

        def h(c):
            return min(abs(c[0] - t[0]) + abs(c[1] - t[1]) for t in tl) / 16
        heap, parent, best = [], {}, {}
        cnt = 0
        for c in sources:
            if c in self.cross:
                continue
            st = (c, None)
            best[st] = 0
            parent[st] = None
            heapq.heappush(heap, (h(c), 0, cnt, st))
            cnt += 1
        own = self.cells[n]
        while heap:
            f, g, _, st = heapq.heappop(heap)
            if g > best.get(st, INF):
                continue
            c, d = st
            if c in tset:
                path = []
                while st is not None:
                    path.append(st[0])
                    st = parent[st]
                return path[::-1]
            other = {m: o for m, o in self.wire.get(c, {}).items() if m != n}
            for nd in DIRS:
                if d is not None and nd == (-d[0], -d[1]):
                    continue
                if other and d is not None and nd != d:
                    continue
                c2 = (c[0] + nd[0] * 16, c[1] + nd[1] * 16)
                if not (x0 <= c2[0] <= x1 and y0 <= c2[1] <= y1) or c2 in self.blocked:
                    continue
                extra = 0
                if c2 in tset:
                    pass
                elif c2 in own:
                    continue
                else:
                    if c2 in self.vert:
                        continue
                    w = self.wire.get(c2)
                    if w:
                        ors = set().union(*w.values())
                        mine = "H" if nd[1] == 0 else "V"
                        if len(ors) > 1 or mine in ors:
                            continue
                        extra = self.CROSS
                g2 = g + 1 + extra + self.soft.get(c2, 0) + (self.BEND if d is not None and nd != d else 0)
                s2 = (c2, nd)
                if g2 < best.get(s2, INF):
                    best[s2] = g2
                    parent[s2] = st
                    heapq.heappush(heap, (g2 + h(c2), g2, cnt, s2))
                    cnt += 1
        return None

    def commit(self, n, path):
        for a, b in zip(path, path[1:]):
            self.add_line(n, a, b)
        for c in path[1:-1]:
            if any(m != n for m in self.wire[c]):
                self.cross.add(c)
        self.mark_vertex(path[0], n)
        self.mark_vertex(path[-1], n)
        for p, c, q in zip(path, path[1:], path[2:]):
            if (c[0] - p[0], c[1] - p[1]) != (q[0] - c[0], q[1] - c[1]):
                self.mark_vertex(c, n)

    def route_net(self, n):
        groups = self.groups(n)
        if len(groups) <= 1:
            return True
        groups.sort(key=lambda g: (min(c[0] for c in g), min(c[1] for c in g)))
        tree = set(groups[0])
        rest = groups[1:]
        while rest:
            def dist(g):
                return min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in tree for b in g)
            tgt = min(rest, key=dist)
            path = self.astar(n, tree, tgt)
            if path is None:
                return False
            self.commit(n, path)
            tree |= tgt | set(path)
            rest.remove(tgt)
        return True

    def route(self, order):
        for n in order:
            if not self.route_net(n):
                self.failed.append(n)

    def free_score(self, c):
        s = 0
        for dx, dy in ((0, -16), (0, -32), (16, -16), (16, -32), (-16, -16), (32, -16)):
            q = (c[0] + dx, c[1] + dy)
            if q not in self.blocked and q not in self.wire and q not in self.vert and self.soft.get(q, 0) < 1:
                s += 1
        return s

    def place_flags(self, named):
        for e, c in enumerate(self.comps):
            for k, n in enumerate(c.nodes):
                if (n in self.label_nets or n in self.failed) and (self.geo[e]["terms"][k], n) not in self.flags:
                    self.flags.append((self.geo[e]["terms"][k], n))
        if GND not in self.label_nets and GND not in self.failed and self.cells[GND]:
            cand = [c for c in self.cells[GND] if c not in self.cross]
            c = max(cand, key=lambda c: (c[1], -c[0]))
            self.mark_vertex(c, GND)
            self.flags.append((c, GND))
        for n in named:
            if n == GND or n in self.label_nets or n in self.failed or not self.cells[n]:
                continue
            cand = [c for c in self.cells[n] if c not in self.cross]

            def score(c):
                hz = 2 if self.wire.get(c, {}).get(n) == {"H"} else 0
                return (hz + self.free_score(c), c[0], -c[1])
            c = max(cand, key=score)
            self.mark_vertex(c, n)
            self.flags.append((c, n))

    def segments(self, n):
        adj = defaultdict(set)
        for a, b in self.edges[n]:
            adj[a].add(b)
            adj[b].add(a)

        split = self.pinpts | {f[0] for f in self.flags}

        def is_vertex(c):
            if c in split:
                return True
            nb = adj[c]
            if len(nb) != 2:
                return True
            a, b = nb
            return not (a[0] == b[0] == c[0] or a[1] == b[1] == c[1])
        segs, seen = [], set()
        for v in list(adj):
            if not is_vertex(v):
                continue
            for w in list(adj[v]):
                if (v, w) in seen:
                    continue
                d = (w[0] - v[0], w[1] - v[1])
                prev, cur = v, w
                while not is_vertex(cur):
                    prev, cur = cur, (cur[0] + d[0], cur[1] + d[1])
                seen.add((v, w))
                seen.add((cur, prev))
                segs.append((v, cur))
        return segs

    def score(self):
        total, bends = 0, 0
        for n, es in self.edges.items():
            total += len(es)
        for c, n in self.vert.items():
            ors = self.wire.get(c, {}).get(n, set())
            if ors == {"H", "V"}:
                bends += 1
        return total + 2 * bends + 8 * len(self.cross) + 60 * len(self.failed)


# ---------------------------------------------------------------------------
# 회로도 생성
# ---------------------------------------------------------------------------
def referenced_nets(lines: list[str], nets: set[str]) -> list[str]:
    found = []
    low = {n.lower(): n for n in nets}
    for line in lines:
        for m in re.finditer(r"\bV\(([^()]+)\)", line, flags=re.I):
            for part in m.group(1).split(","):
                n = low.get(part.strip().lower())
                if n and n not in found:
                    found.append(n)
    return found


def build_geometry(comps, fps, st):
    geo = []
    for e, c in enumerate(comps):
        i, j, o = st[e]
        fp = fps[e][o]
        dx, dy = i * U, j * U
        geo.append(dict(
            rot=fp.rot, org=(fp.org[0] + dx, fp.org[1] + dy), windows=fp.windows,
            pins=[(p[0] + dx, p[1] + dy) for p in fp.pins],
            terms=[(t[0] + dx, t[1] + dy) for t in fp.terms],
            stubs=[[(p[0] + dx, p[1] + dy) for p in poly] for poly in fp.stubs],
            body=shift(fp.body, dx, dy),
            labels=[shift(L, dx, dy) for L in fp.labels]))
    return geo


def _anneal_job(args):
    comps, label_nets, supply, seed, iters = args
    pl = Placer(comps, label_nets, seed, supply)
    st, cost = pl.anneal(pl.initial(), iters)
    return cost, st


def layout_circuit(comps, label_nets, named, supply=frozenset(), restarts=4, effort=1.0):
    nets = {n for c in comps for n in c.nodes}
    n = len(comps)
    iters = int(effort * (2000 * n + 4000) * max(1.0, n / 12))
    jobs = [(comps, label_nets, supply, seed, iters) for seed in range(restarts)]
    try:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(restarts, os.cpu_count() or 1)) as ex:
            results = list(ex.map(_anneal_job, jobs))
    except Exception:
        results = [_anneal_job(j) for j in jobs]
    results.sort(key=lambda r: r[0])
    pl = Placer(comps, label_nets, 0, supply)
    best = None
    for cost, st in results[:3]:
        if cost == INF:
            continue
        geo = build_geometry(comps, pl.fps, st)
        order = sorted((n for n in nets if n not in label_nets),
                       key=lambda n: (n == GND, len(pl.netpins[n])))
        tries = [order, order[::-1]]
        rng = random.Random(len(st))
        for _ in range(4):
            o2 = order[:]
            rng.shuffle(o2)
            tries.append(o2)
        for o in tries:
            r = Router(comps, geo, label_nets)
            r.route(o)
            sc = r.score() / 2 + cost
            if best is None or sc < best[0]:
                best = (sc, r, geo)
            if not r.failed:
                break
    if best is None:
        raise RuntimeError("배치에 실패했습니다")
    _, r, geo = best
    r.place_flags(named)
    return r, geo


def schematic_text(circ, comps, directives, supply=frozenset(), effort=1.0):
    nets = {n for c in comps for n in c.nodes}
    label_nets = {norm_net(n) for n in as_list(circ.get("labels"))}
    if str(circ.get("wiring", "")).lower() in ("label", "labels", "node", "nodes"):
        label_nets = set(nets)
    named = referenced_nets(directives, nets)
    for n in as_list(circ.get("show")):
        if norm_net(n) in nets and norm_net(n) not in named:
            named.append(norm_net(n))
    r, geo = layout_circuit(comps, label_nets, named, supply, effort=float(circ.get("effort", effort)))

    wires = []
    for n in list(r.edges):
        wires += r.segments(n)
    pts = [p for w in wires for p in w] + [f[0] for f in r.flags]
    for g in geo:
        pts += [(g["body"][0], g["body"][1]), (g["body"][2], g["body"][3])]
        for L in g["labels"]:
            pts += [(L[0], L[1]), (L[2], L[3])]
    x0 = snap_lo(min(p[0] for p in pts)) // U * U
    y0 = snap_lo(min(p[1] for p in pts)) // U * U
    dx, dy = 64 - x0, 64 - y0
    xmax = max(p[0] for p in pts) + dx
    ymax = max(p[1] for p in pts) + dy

    out = ["Version 4", ""]
    for a, b in sorted(wires):
        out.append(f"WIRE {a[0] + dx} {a[1] + dy} {b[0] + dx} {b[1] + dy}")
    for (x, y), n in r.flags:
        out.append(f"FLAG {x + dx} {y + dy} {n}")
    for c, g in zip(comps, geo):
        ox, oy = g["org"]
        out.append(f"SYMBOL {c.sym.get('sym', c.type)} {ox + dx} {oy + dy} {g['rot']}")
        out.extend(g["windows"])
        out.append(f"SYMATTR InstName {c.ref}")
        if c.value and not (c.type == "opamp" and c.value == "opamp"):
            out.append(f"SYMATTR Value {c.value}")
        for k, v in c.attrs.items():
            out.append(f"SYMATTR {k} {v}")
    ty = snap_hi(ymax) + 64
    user = [str(d) for d in circ.get("directives") or []]
    blocks = ([user] if user else []) + [[d] for d in directives if d not in user]
    for blk in blocks:
        out.append(f"TEXT 64 {ty} Left 2 !" + "\\n".join(blk))
        ty += 32 * len(blk)
    out[1] = f"SHEET 1 {max(880, snap_hi(xmax) + 160)} {max(680, ty + 64)}"
    text = "\n".join(out) + "\n"
    problems = verify_asc(text, comps)
    if problems:
        raise RuntimeError("회로도 연결 검증 실패: " + "; ".join(problems))
    return text, r


# ---------------------------------------------------------------------------
# 회로도 연결 검증 (생성된 .asc 를 다시 읽어 넷 비교)
# ---------------------------------------------------------------------------
def asc_nets(text: str, strict: bool):
    by_sym = {s.get("sym", k): k for k, s in SYMBOLS.items()}
    wires, flags, syms = [], [], []
    for line in text.splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "WIRE":
            wires.append(((int(t[1]), int(t[2])), (int(t[3]), int(t[4]))))
        elif t[0] == "FLAG":
            flags.append(((int(t[1]), int(t[2])), t[3]))
        elif t[0] == "SYMBOL":
            syms.append([by_sym[t[1]], int(t[2]), int(t[3]), t[4], None])
        elif t[0] == "SYMATTR" and t[1] == "InstName":
            syms[-1][4] = t[2]
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)
    pins = {}
    for typ, ox, oy, rot, ref in syms:
        for k, p in enumerate(SYMBOLS[typ]["pins"]):
            x, y = transform(p, rot)
            pins[(ref, k)] = (x + ox, y + oy)
            find((x + ox, y + oy))
    for a, b in wires:
        union(a, b)
    for p, name in flags:
        union(p, ("net", GND if name.lower() in ("0", "gnd") else name))
    if not strict:
        pts = set(pins.values()) | {p for w in wires for p in w} | {p for p, _ in flags}
        for p in pts:
            for w in wires:
                if on_seg(p, w) and p not in w:
                    union(p, w[0])
    return {k: find(p) for k, p in pins.items()}, find


def verify_asc(text: str, comps: list[Comp]) -> list[str]:
    problems = []
    for strict in (True, False):
        got, find = asc_nets(text, strict)
        root_of_net = {}
        for c in comps:
            for k, n in enumerate(c.nodes):
                r = got[(c.ref, k)]
                if root_of_net.setdefault(n, r) != r:
                    problems.append(f"{c.ref} 핀{k + 1} 이 넷 '{n}' 에 연결되지 않음")
        roots = defaultdict(list)
        for n, r in root_of_net.items():
            roots[r].append(n)
        for r, ns in roots.items():
            if len(ns) > 1:
                problems.append(f"넷 {ns} 이 서로 단락됨")
        for n, r in root_of_net.items():
            nr = find(("net", n))
            if nr != r and any(find(("net", m)) == nr for m in root_of_net if m != n):
                problems.append(f"라벨 '{n}' 위치 오류")
        if problems:
            break
    return problems


# ---------------------------------------------------------------------------
# 넷리스트 / 시뮬레이션
# ---------------------------------------------------------------------------
_SUFFIX = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6,
           "n": 1e-9, "p": 1e-12, "f": 1e-15}


def eng(v: str) -> float:
    m = re.match(r"^([-+]?[\d.]+(?:e[-+]?\d+)?)(meg|[tgkmuµnpf])?", v.strip().lower())
    if not m:
        raise ValueError(f"숫자로 해석할 수 없습니다: {v}")
    return float(m.group(1)) * _SUFFIX.get(m.group(2) or "", 1.0)


def build_directives(circ: dict, override: dict | None = None, for_asc=False, uses_opamp=False) -> list[str]:
    lines = []
    params = dict(circ.get("params") or {})
    sweep = circ.get("sweep")
    if sweep and sweep["param"] not in params:
        params[sweep["param"]] = sweep["values"][0]
    if override:
        params.update(override)
    for k, v in params.items():
        lines.append(f".param {k}={v}")
    if for_asc and sweep:
        lines.append(f".step param {sweep['param']} list {' '.join(str(v) for v in sweep['values'])}")
    user = [str(d) for d in circ.get("directives") or []]
    if uses_opamp and not any(re.match(r"\.subckt\s+opamp\b", d, re.I) for d in user):
        lines.append(".lib opamp.sub")
    lines += user
    for a in as_list(circ.get("analysis")):
        lines.append(a if a.startswith(".") else "." + a)
    for m in circ.get("measure") or []:
        lines.append(m if m.lower().startswith(".meas") else ".meas " + m)
    return lines


def comp_line(c: Comp) -> str:
    nodes = list(c.nodes)
    if c.type in ("nmos", "pmos"):
        nodes.append(nodes[2])
    val = c.value
    if c.type == "opamp" and (not val or val == "opamp"):
        val = "opamp Aol=100K GBW=10Meg"
    return f"{c.spice_name} {' '.join(nodes)} {val}".rstrip()


def tran_fix(line: str) -> str:
    m = re.match(r"^\.?tran\s+0\s+(\S+)(.*)$", line, flags=re.I)
    if m:
        return f".tran {eng(m.group(1)) / 1000:g} {m.group(1)}{m.group(2)}"
    m = re.match(r"^\.?tran\s+(\S+)\s*$", line, flags=re.I)
    if m:
        return f".tran {eng(m.group(1)) / 1000:g} {m.group(1)}"
    return line


def to_ngspice(line: str, has_op: bool) -> str:
    low = line.lower()
    if low.startswith(".lib opamp.sub"):
        return OPAMP_SUB
    if low.startswith(".tran"):
        return tran_fix(line)
    if low.startswith(".op") and has_op:
        return ".dc Vngdummy 0 1 1"
    if low.startswith(".meas"):
        line = re.sub(r"\b(AT|FROM|TO|TD)\s+(?!=)", r"\1=", line, flags=re.I)

        def dev_i(k):
            fn, dev = k.group(1).lower(), k.group(2).lower()
            if fn == "i" and dev[0] in "rcl":
                return f"@{dev}[i]"
            if fn == "i" and dev[0] == "d":
                return f"@{dev}[id]"
            if fn in ("ic", "ib", "ie", "id", "is", "ig") and dev[0] in "qm":
                return f"@{dev}[{fn}]"
            return k.group(0)
        line = re.sub(r"\b(I|Ic|Ib|Ie|Id|Is|Ig)\((\w+)\)", dev_i, line, flags=re.I)
        for fn, ng in (("mag", "vm"), ("db", "vdb"), ("ph", "vp"), ("re", "vr"), ("im", "vi")):
            line = re.sub(rf"\b{fn}\(\s*V\(([^()]+)\)\s*\)", rf"{ng}(\1)", line, flags=re.I)
        if has_op:
            line = re.sub(r"^\.meas(ure)?\s+op\b", ".meas dc", line, flags=re.I)
            if re.search(r"\bfind\b", line, re.I) and not re.search(r"\bat\s*=", line, re.I):
                line += " AT=0"
        pm = re.match(r"^(.*\b)param\s+([^'=\s].*)$", line, flags=re.I)
        if pm:
            line = f"{pm.group(1)}param='{pm.group(2).strip()}'"
    return line


def netlist(circ, comps, override, flavor) -> str:
    uses_opamp = any(c.type == "opamp" for c in comps)
    lines = [f"* {circ.get('name', 'circuit')}"] + [comp_line(c) for c in comps]
    directives = build_directives(circ, override, uses_opamp=uses_opamp)
    if flavor == "ngspice":
        has_op = any(d.lower().startswith(".op") for d in directives)
        if has_op:
            lines.append("Vngdummy ngdummy 0 0")
        lines.append(".options savecurrents")
        lines += [to_ngspice(d, has_op) for d in directives]
        lines += [".control", "run", ".endc", ".end"]
    else:
        lines += directives + [".backanno", ".end"]
    return "\n".join(lines) + "\n"


def find_ltspice() -> list[str] | None:
    env = os.environ.get("LTSPICE_EXE")
    if env:
        return [env] if os.name == "nt" or not env.lower().endswith(".exe") else ["wine", env]
    for c in [r"C:\Program Files\ADI\LTspice\LTspice.exe", r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\ADI\LTspice\LTspice.exe"),
              "/Applications/LTspice.app/Contents/MacOS/LTspice"]:
        if os.path.isfile(c):
            return [c]
    if shutil.which("wine"):
        for pat in ["drive_c/Program Files/ADI/LTspice/LTspice.exe",
                    "drive_c/Program Files/LTC/LTspiceXVII/XVIIx64.exe",
                    "drive_c/users/*/AppData/Local/Programs/ADI/LTspice/LTspice.exe"]:
            hits = glob.glob(str(Path.home() / ".wine" / pat))
            if hits:
                return ["wine", hits[0]]
    return None


def pick_sim(choice: str) -> str | None:
    if choice == "ltspice":
        if not find_ltspice():
            sys.exit("LTspice 실행파일을 찾을 수 없습니다. 환경변수 LTSPICE_EXE 를 지정하세요.")
        return "ltspice"
    if choice == "ngspice":
        if not shutil.which("ngspice"):
            sys.exit("ngspice 를 찾을 수 없습니다.")
        return "ngspice"
    if find_ltspice():
        return "ltspice"
    return "ngspice" if shutil.which("ngspice") else None


def read_text_any(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1:2] == b"\x00"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def parse_ltspice_log(text: str) -> dict[str, str]:
    res = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\w+)\s*:\s*.*?=\s*(\(.*?\)|" + NUM + ")", line) \
            or re.match(r"^\s*(\w+)\s*=\s*(\(.*?\)|" + NUM + ")", line)
        if m:
            res[m.group(1).lower()] = m.group(2)
    return res


def parse_ngspice_out(text: str) -> dict[str, str]:
    res = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\w+)\s*=\s*(" + NUM + ")", line)
        if m:
            res[m.group(1).lower()] = m.group(2)
    return res


def run_sim(net: Path, sim: str) -> dict[str, str]:
    if sim == "ltspice":
        subprocess.run(find_ltspice() + ["-b", net.name], cwd=net.parent, capture_output=True, timeout=600)
        log = net.with_suffix(".log")
        return parse_ltspice_log(read_text_any(log)) if log.exists() else {}
    p = subprocess.run(["ngspice", "-b", net.name], cwd=net.parent, capture_output=True, text=True, timeout=600)
    return parse_ngspice_out(p.stdout + "\n" + p.stderr)


# ---------------------------------------------------------------------------
# 전류 방향 정리: R/C/L 전류가 양수가 되도록 핀 순서를 맞춤
# ---------------------------------------------------------------------------
def read_raw_ascii(path: Path) -> dict[str, list]:
    lines = path.read_text(errors="replace").splitlines()
    names, cplx, nvar, npts = [], False, 0, 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("Flags:"):
            cplx = "complex" in ln
        elif ln.startswith("No. Variables:"):
            nvar = int(ln.split(":")[1])
        elif ln.startswith("No. Points:"):
            npts = int(ln.split(":")[1])
        elif ln.startswith("Variables:"):
            for _ in range(nvar):
                i += 1
                names.append(lines[i].split()[1].lower())
        elif ln.startswith("Values:"):
            toks = " ".join(lines[i + 1:]).split()
            data = {n: [] for n in names}
            pos = 0
            for _ in range(npts):
                pos += 1
                for n in names:
                    t = toks[pos]
                    pos += 1
                    if cplx:
                        re_, im_ = t.split(",")
                        data[n].append(complex(float(re_), float(im_)))
                    else:
                        data[n].append(float(t))
            return data
        i += 1
    return {}


def current_sign(v: list, t: list | None) -> int:
    if not v:
        return 0
    if isinstance(v[0], complex):
        k = max(range(len(v)), key=lambda i: abs(v[i]))
        z = v[k]
        if abs(z) < 1e-15:
            return 0
        x = z.real if abs(z.real) > 1e-3 * abs(z) else z.imag
        return 1 if x > 0 else -1
    peak = max(abs(x) for x in v)
    if peak < 1e-12:
        return 0
    if t and len(t) == len(v) and len(v) > 1 and t[-1] > t[0]:
        avg = sum((v[i] + v[i + 1]) / 2 * (t[i + 1] - t[i]) for i in range(len(v) - 1)) / (t[-1] - t[0])
    else:
        avg = sum(v) / len(v)
    if abs(avg) > 0.02 * peak:
        return 1 if avg > 0 else -1
    for x in v:
        if abs(x) > 0.1 * peak:
            return 1 if x > 0 else -1
    return 0


def presim(circ, comps, work: Path):
    """ngspice 로 미리 돌려 (1) 전류가 음수인 R/C/L 의 핀 순서를 뒤집고 (2) 전원 넷을 찾음"""
    if not shutil.which("ngspice"):
        return [], frozenset()
    analyses = [a if a.startswith(".") else "." + a for a in as_list(circ.get("analysis"))]
    prim = [a for a in analyses if a.split()[0].lower() in (".tran", ".dc")][:1]
    runs = [".op"] + (prim or [a for a in analyses if a.lower().startswith(".ac")][:1])
    base = [c for c in build_directives(circ, uses_opamp=any(c.type == "opamp" for c in comps))
            if not c.lower().startswith((".meas", ".tran", ".ac", ".dc", ".op", ".noise", ".tf", ".four"))]
    results = []
    for k, a in enumerate(runs):
        raw = work / f"_pre{k}.raw"
        cir = work / f"_pre{k}.cir"
        body = ["* pre"] + [comp_line(c) for c in comps] + [".options savecurrents"]
        body += [to_ngspice(d, False) for d in base]
        body += [".control", "set filetype=ascii", tran_fix(a)[1:], f"write {raw.name}", ".endc", ".end"]
        cir.write_text("\n".join(body) + "\n", encoding="utf-8")
        subprocess.run(["ngspice", "-b", cir.name], cwd=work, capture_output=True, timeout=600)
        results.append(read_raw_ascii(raw) if raw.exists() else {})

    def vec(data, name):
        return data.get(f"@{name}[i]") or data.get(f"i(@{name}[i])") or []
    signs = {}
    for data in results[1:] + results[:1]:
        t = data.get("time")
        for c in comps:
            if c.sym["kind"] != "passive" or c.keep or signs.get(c.ref):
                continue
            sgn = current_sign(vec(data, c.spice_name.lower()), t)
            if sgn:
                signs[c.ref] = sgn
    flipped = []
    for c in comps:
        if signs.get(c.ref) == -1:
            c.nodes.reverse()
            flipped.append(c.ref)

    op = results[0]
    volts = {n: op[f"v({n.lower()})"][0] for c in comps for n in c.nodes
             if n != GND and op.get(f"v({n.lower()})")}
    supply = set()
    vmax = max([v for v in volts.values()], default=0)
    for c in comps:
        if c.type == "voltage" and c.nodes[1] == GND and c.nodes[0] in volts:
            if vmax > 0 and volts[c.nodes[0]] >= 0.9 * vmax and is_dc(c.value):
                supply.add(c.nodes[0])
    return flipped, frozenset(supply)


def is_dc(value: str) -> bool:
    v = re.sub(r"(?i)^dc\s+", "", value.strip())
    try:
        eng(v)
        return re.fullmatch(r"[-+]?[\d.]+(?:e[-+]?\d+)?(meg|[tgkmuµnpf])?v?", v.lower()) is not None
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 결과 txt
# ---------------------------------------------------------------------------
def meas_names(circ: dict) -> list[str]:
    names = []
    for m in circ.get("measure") or []:
        toks = re.sub(r"^\.meas(ure)?\s+", "", m, flags=re.I).split()
        if toks and toks[0].lower() in ("tran", "ac", "dc", "op", "noise", "tf"):
            toks = toks[1:]
        if toks:
            names.append(toks[0])
    return names


def fmt_value(v: str | None) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.6g}"
    except ValueError:
        return v


def result_block(circ, rows) -> list[str]:
    names = meas_names(circ)
    lines = [f"[{circ['name']}]"]
    sweep = circ.get("sweep")
    if sweep:
        lines.append("\t".join([sweep["param"]] + names))
        for ov, res in rows:
            lines.append("\t".join([str(ov[sweep["param"]])] + [fmt_value(res.get(n.lower())) for n in names]))
    else:
        w = max((len(n) for n in names), default=4)
        for n in names:
            lines.append(f"{n.ljust(w)} = {fmt_value(rows[0][1].get(n.lower()))}")
    return lines


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", str(s)).strip() or "circuit"


def normalize_spec(spec: dict, stem: str):
    if "circuits" in spec:
        project = spec.get("project") or spec.get("name") or stem
        circuits = spec["circuits"]
    else:
        project = spec.get("project") or spec.get("name") or stem
        circuits = [spec]
    for k, c in enumerate(circuits):
        c.setdefault("name", f"circuit{k + 1}")
    return safe_name(project), circuits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LTspice .asc 자동 생성 + 측정값 txt 추출")
    ap.add_argument("spec", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("output"))
    ap.add_argument("--sim", choices=["auto", "ltspice", "ngspice"], default="auto")
    ap.add_argument("--no-sim", action="store_true")
    args = ap.parse_args(argv)

    project, circuits = normalize_spec(load_spec(args.spec), args.spec.stem)
    folder = args.out / project
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    sim = None if args.no_sim else pick_sim(args.sim)
    work = Path(tempfile.mkdtemp(prefix="ltgen_"))
    report = []
    try:
        for circ in circuits:
            name = safe_name(circ["name"])
            comps = parse_components(circ)
            flipped, supply = presim(circ, comps, work)
            uses_opamp = any(c.type == "opamp" for c in comps)
            text, router = schematic_text(circ, comps, build_directives(circ, for_asc=True, uses_opamp=uses_opamp),
                                          supply)
            (folder / f"{name}.asc").write_text(text, encoding="utf-8")
            msg = f"[회로도] {name}.asc"
            if flipped:
                msg += f"  (전류 방향 정리: {', '.join(flipped)})"
            if router.failed:
                msg += f"  (라벨로 연결한 넷: {', '.join(router.failed)})"
            print(msg)
            if sim and circ.get("measure"):
                sweep = circ.get("sweep")
                overrides = [{sweep["param"]: v} for v in sweep["values"]] if sweep else [{}]
                rows = []
                for k, ov in enumerate(overrides):
                    net = work / f"c{len(report)}_{k}.net"
                    net.write_text(netlist(circ, comps, ov, sim), encoding="utf-8")
                    rows.append((ov, run_sim(net, sim)))
                report.append(result_block(circ, rows))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if report:
        (folder / "측정결과.txt").write_text("\n\n".join("\n".join(b) for b in report) + "\n", encoding="utf-8")
        print(f"[측정결과] 측정결과.txt ({sim})")
    elif not args.no_sim and sim is None:
        print("[알림] LTspice/ngspice 가 없어 측정값은 만들지 않았습니다. .asc 를 LTspice 에서 실행하세요.")
    zpath = args.out / f"{project}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.iterdir()):
            z.write(f, f"{project}/{f.name}")
    print(f"[완료] {zpath}")
    if report:
        print()
        print((folder / "측정결과.txt").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
