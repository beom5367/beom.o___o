#!/usr/bin/env python3
"""
ltgen.py - LTspice 회로도(.asc) 자동 생성 + 시뮬레이션 + 측정값(.txt) 추출

사용법:
    python ltgen.py examples/rc_lowpass.yaml            # .asc/.net 생성 + (가능하면) 시뮬레이션 + txt
    python ltgen.py examples/rc_lowpass.yaml --no-sim   # .asc/.net 만 생성
    python ltgen.py spec.yaml --sim ngspice             # 시뮬레이터 강제 지정 (ltspice | ngspice | auto)
    python ltgen.py spec.yaml -o out_dir

회로 사양(YAML/JSON) 형식은 README.md 참고.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 심볼 정의: LTspice 기본 라이브러리 심볼의 핀 좌표(R0 기준)와 SPICE 핀 순서
#   pins   : 핀 좌표 목록 (nodes 순서와 동일)
#   prefix : 넷리스트 소자 접두어
#   sym    : .asc 에 쓸 심볼 경로 (생략 시 type 이름)
#   (핀 좌표는 LTspice 기본 lib/sym/*.asy 파일 기준)
# ---------------------------------------------------------------------------
SYMBOLS: dict[str, dict] = {
    "res":      {"pins": [(16, 16), (16, 96)], "prefix": "R"},
    "cap":      {"pins": [(16, 0), (16, 64)], "prefix": "C"},
    "ind":      {"pins": [(16, 16), (16, 96)], "prefix": "L"},
    "voltage":  {"pins": [(0, 16), (0, 96)], "prefix": "V"},          # +, -
    "current":  {"pins": [(0, 0), (0, 80)], "prefix": "I"},           # 전류: 핀1 -> 핀2
    "diode":    {"pins": [(16, 0), (16, 64)], "prefix": "D"},         # A, K
    "zener":    {"pins": [(16, 0), (16, 64)], "prefix": "D"},
    "schottky": {"pins": [(16, 0), (16, 64)], "prefix": "D"},
    "LED":      {"pins": [(16, 0), (16, 64)], "prefix": "D"},
    "npn":      {"pins": [(64, 0), (0, 48), (64, 96)], "prefix": "Q"},   # C, B, E
    "pnp":      {"pins": [(64, 0), (0, 48), (64, 96)], "prefix": "Q"},   # C, B, E
    "nmos":     {"pins": [(48, 0), (0, 80), (48, 96)], "prefix": "M"},   # D, G, S
    "pmos":     {"pins": [(48, 0), (0, 80), (48, 96)], "prefix": "M"},   # D, G, S
    # 이상적 opamp (OpAmps\opamp): 노드 순서 In-, In+, OUT / value = 서브회로 이름
    "opamp":    {"pins": [(-32, 48), (-32, 80), (32, 64)], "prefix": "X", "sym": "OpAmps\\opamp"},
    "bv":       {"pins": [(0, 16), (0, 96)], "prefix": "B"},          # 행동 전압원 (value 예: "V=V(a)*2")
}

# 사용자가 쓰기 편하도록 별칭 허용
ALIASES = {
    "r": "res", "resistor": "res",
    "c": "cap", "capacitor": "cap",
    "l": "ind", "inductor": "ind",
    "v": "voltage", "vsource": "voltage",
    "i": "current", "isource": "current",
    "d": "diode", "led": "LED",
}

GRID_X = 176     # 소자 간 가로 간격
GRID_Y = 256     # 행 간 세로 간격
PER_ROW = 5      # 한 행에 놓을 소자 수


@dataclass
class Component:
    ref: str
    type: str
    nodes: list[str]
    value: str = ""
    rot: str = "R0"
    pos: tuple[int, int] | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 좌표 변환
# ---------------------------------------------------------------------------
def transform(pt: tuple[int, int], rot: str) -> tuple[int, int]:
    """LTspice 회전/미러(R0,R90,R180,R270,M0,M90,M180,M270) 적용"""
    x, y = pt
    if rot.startswith("M"):
        x = -x
    ang = int(rot[1:]) % 360
    for _ in range(ang // 90):
        x, y = -y, x
    return x, y


# ---------------------------------------------------------------------------
# 사양 로드
# ---------------------------------------------------------------------------
def load_spec(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            sys.exit("PyYAML 이 필요합니다: pip install pyyaml  (또는 .json 사양을 사용하세요)")
        return yaml.safe_load(text)
    return json.loads(text)


def parse_components(spec: dict) -> list[Component]:
    # 사용자 정의 심볼 등록
    for name, sym in (spec.get("symbols") or {}).items():
        SYMBOLS[name] = {"pins": [tuple(p) for p in sym["pins"]], "prefix": sym.get("prefix", "X"),
                         "sym": sym.get("sym", name)}

    comps = []
    for i, c in enumerate(spec["components"]):
        t = ALIASES.get(str(c["type"]).lower(), c["type"])
        if t not in SYMBOLS:
            raise ValueError(f"알 수 없는 소자 type '{c['type']}' (지원: {', '.join(SYMBOLS)})")
        nodes = [str(n) for n in c["nodes"]]
        npins = len(SYMBOLS[t]["pins"])
        if len(nodes) != npins:
            raise ValueError(f"{c['ref']}: '{t}' 는 노드 {npins}개가 필요합니다 (받은 값: {nodes})")
        pos = tuple(c["pos"]) if "pos" in c else None
        comps.append(Component(
            ref=str(c["ref"]), type=t, nodes=nodes, value=str(c.get("value", "")),
            rot=str(c.get("rot", "R0")), pos=pos,
            extra={k: v for k, v in c.items() if k not in ("ref", "type", "nodes", "value", "rot", "pos")},
        ))
    return comps


def auto_layout(comps: list[Component]) -> None:
    """pos 가 없는 소자를 격자에 배치 (전원/소스는 왼쪽 앞으로)"""
    order = sorted(
        [c for c in comps if c.pos is None],
        key=lambda c: 0 if c.type in ("voltage", "current", "bv") else 1,
    )
    for idx, c in enumerate(order):
        col, row = idx % PER_ROW, idx // PER_ROW
        # 왼쪽으로 튀어나온 핀(opamp 등)이 겹치지 않도록 여유를 둠
        minx = min(transform(p, c.rot)[0] for p in SYMBOLS[c.type]["pins"])
        c.pos = (96 + col * GRID_X - min(minx, 0), 96 + row * GRID_Y)


# ---------------------------------------------------------------------------
# .asc 생성
# ---------------------------------------------------------------------------
def build_directives(spec: dict, param_override: dict | None = None, for_asc: bool = False) -> list[str]:
    lines: list[str] = []
    params = dict(spec.get("params") or {})
    if param_override:
        params.update(param_override)
    for k, v in params.items():
        lines.append(f".param {k}={v}")
    sweep = spec.get("sweep")
    if for_asc and sweep:
        vals = " ".join(str(v) for v in sweep["values"])
        lines.append(f".step param {sweep['param']} list {vals}")
    for d in spec.get("directives") or []:
        lines.append(str(d))
    for a in _as_list(spec.get("analysis")):
        lines.append(a if a.startswith(".") else "." + a)
    for m in spec.get("measure") or []:
        lines.append(m if m.lower().startswith(".meas") else ".meas " + m)
    return lines


def _as_list(x) -> list[str]:
    if x is None:
        return []
    return [str(i) for i in x] if isinstance(x, list) else [str(x)]


def write_asc(spec: dict, comps: list[Component], path: Path) -> None:
    out = ["Version 4", "SHEET 1 1600 1200"]
    max_x = max_y = 0
    flags = []
    for c in comps:
        x0, y0 = c.pos
        out.append(f"SYMBOL {SYMBOLS[c.type].get('sym', c.type)} {x0} {y0} {c.rot}")
        out.append(f"SYMATTR InstName {c.ref}")
        if c.value:
            out.append(f"SYMATTR Value {c.value}")
        for k, v in c.extra.items():   # 예: Value2, SpiceLine 등
            out.append(f"SYMATTR {k} {v}")
        for pin, net in zip(SYMBOLS[c.type]["pins"], c.nodes):
            dx, dy = transform(pin, c.rot)
            px, py = x0 + dx, y0 + dy
            flags.append(f"FLAG {px} {py} {net}")
            max_x, max_y = max(max_x, px), max(max_y, py)
    out.extend(flags)

    ty = max_y + 96
    title = spec.get("title") or spec.get("name", "")
    if title:
        out.append(f"TEXT 64 {ty} Left 2 ;{title}")
        ty += 32
    # 사용자 directives(.subckt 등 여러 줄)는 하나의 TEXT 블록으로 묶어 순서를 보장
    user = [str(d) for d in spec.get("directives") or []]
    others = [d for d in build_directives(spec, for_asc=True) if d not in user]
    blocks = ([user] if user else []) + [[d] for d in others]
    for blk in blocks:
        out.append(f"TEXT 64 {ty} Left 2 !" + "\\n".join(blk))
        ty += 32 * len(blk)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 넷리스트 생성 (시뮬레이션용)
# ---------------------------------------------------------------------------
def comp_to_spice(c: Component) -> str:
    prefix = SYMBOLS[c.type]["prefix"]
    name = c.ref if c.ref.upper().startswith(prefix) else prefix + "_" + c.ref
    nodes = list(c.nodes)
    if c.type in ("nmos", "pmos"):
        nodes.append(nodes[2])            # 3단자 MOSFET: 바디 = 소스
    return f"{name} {' '.join(nodes)} {c.value}".rstrip()


def to_ngspice(line: str, has_op: bool) -> str:
    """LTspice 문법을 ngspice 문법으로 보정 (검증/대체 시뮬레이션용)"""
    low = line.lower()
    # .tran 0 10m ... : ngspice 는 TSTEP=0 을 허용하지 않음
    m = re.match(r"^\.tran\s+0\s+(\S+)(.*)$", line, flags=re.I)
    if m:
        line = f".tran {_eng(m.group(1)) / 1000:g} {m.group(1)}{m.group(2)}"
    elif re.match(r"^\.tran\s+\S+\s*$", line, flags=re.I):          # .tran 10m
        stop = line.split()[1]
        line = f".tran {_eng(stop) / 1000:g} {stop}"
    if low.startswith(".op") and has_op:
        return ".dc Vngdummy 0 1 1"
    if low.startswith(".meas"):
        line = re.sub(r"\b(AT|FROM|TO|TD)\s+(?!=)", r"\1=", line, flags=re.I)
        line = re.sub(r"\bI\((R\w*|C\w*|L\w*|D\w*)\)", lambda k: f"@{k.group(1).lower()}[i]", line, flags=re.I)
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


_SUFFIX = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6,
           "n": 1e-9, "p": 1e-12, "f": 1e-15}


def _eng(v: str) -> float:
    m = re.match(r"^([-+]?[\d.]+(?:e[-+]?\d+)?)(meg|[tgkmuµnpf])?", v.strip().lower())
    if not m:
        raise ValueError(f"숫자로 해석할 수 없습니다: {v}")
    return float(m.group(1)) * _SUFFIX.get(m.group(2) or "", 1.0)


def write_net(spec: dict, comps: list[Component], path: Path, param_override: dict | None, flavor: str) -> None:
    lines = [f"* {spec.get('name', path.stem)} (generated by ltgen.py)"]
    lines += [comp_to_spice(c) for c in comps]
    directives = build_directives(spec, param_override)
    if flavor == "ngspice":
        has_op = any(d.lower().startswith(".op") for d in directives)
        if has_op:
            lines.append("Vngdummy ngdummy 0 0")
        lines.append(".options savecurrents")
        lines += [to_ngspice(d, has_op) for d in directives]
        lines.append(".end")
    else:
        lines += directives + [".backanno", ".end"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 시뮬레이터 탐색/실행
# ---------------------------------------------------------------------------
def find_ltspice() -> list[str] | None:
    env = os.environ.get("LTSPICE_EXE")
    if env:
        return [env] if not env.lower().endswith(".exe") or os.name == "nt" else ["wine", env]
    candidates = [
        r"C:\Program Files\ADI\LTspice\LTspice.exe",
        r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\ADI\LTspice\LTspice.exe"),
        "/Applications/LTspice.app/Contents/MacOS/LTspice",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return [c]
    if shutil.which("wine"):
        home = Path.home()
        for pat in ["drive_c/Program Files/ADI/LTspice/LTspice.exe",
                    "drive_c/Program Files/LTC/LTspiceXVII/XVIIx64.exe",
                    "drive_c/users/*/AppData/Local/Programs/ADI/LTspice/LTspice.exe"]:
            hits = glob.glob(str(home / ".wine" / pat))
            if hits:
                return ["wine", hits[0]]
    return None


def read_text_any(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1:2] == b"\x00"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def parse_ltspice_log(text: str) -> dict[str, str]:
    # 예) vout_max: MAX(v(out))=4.96 FROM 0 TO 0.02
    #     t_rise=0.0011 FROM ... / gain: v(out)=(-3.01dB,-45°) at 1000
    res = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\w+)\s*:\s*.*?=\s*(\(.*?\)|" + NUM + ")", line) \
            or re.match(r"^\s*(\w+)\s*=\s*(\(.*?\)|" + NUM + ")", line)
        if m:
            res[m.group(1).lower()] = m.group(2)
    return res


def parse_ngspice_out(text: str) -> dict[str, str]:
    # 예) vout_max            =  4.96000e+00 at=  1.0e-02
    res = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\w+)\s*=\s*(" + NUM + ")", line)
        if m:
            res[m.group(1).lower()] = m.group(2)
    return res


def run_sim(net: Path, sim: str) -> tuple[dict[str, str], str]:
    if sim == "ltspice":
        exe = find_ltspice()
        cmd = exe + ["-b", str(net.name)]
        p = subprocess.run(cmd, cwd=net.parent, capture_output=True, text=True, timeout=600)
        log = net.with_suffix(".log")
        text = read_text_any(log) if log.exists() else p.stdout + p.stderr
        return parse_ltspice_log(text), text
    p = subprocess.run(["ngspice", "-b", str(net.name)], cwd=net.parent,
                       capture_output=True, text=True, timeout=600)
    text = p.stdout + "\n" + p.stderr
    net.with_suffix(".log").write_text(text, encoding="utf-8")
    return parse_ngspice_out(text), text


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
    if shutil.which("ngspice"):
        return "ngspice"
    return None


# ---------------------------------------------------------------------------
# 결과 txt
# ---------------------------------------------------------------------------
def meas_names(spec: dict) -> list[str]:
    names = []
    for m in spec.get("measure") or []:
        toks = re.sub(r"^\.meas(ure)?\s+", "", m, flags=re.I).split()
        # ".meas [TRAN|AC|DC|OP|NOISE] name ..." 
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


def write_txt(spec: dict, rows: list[tuple[dict, dict[str, str]]], sim: str, path: Path) -> None:
    names = meas_names(spec)
    sweep = spec.get("sweep")
    lines = [
        f"# {spec.get('title') or spec.get('name')}",
        f"# 생성 시각 : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"# 시뮬레이터: {sim}",
        f"# 해석      : {', '.join(_as_list(spec.get('analysis')))}",
        "",
    ]
    desc = spec.get("measure_desc") or {}
    if desc:
        lines.append("# 측정 항목 설명")
        for n in names:
            if n in desc:
                lines.append(f"#   {n}: {desc[n]}")
        lines.append("")

    if sweep:
        header = [sweep["param"]] + names
        lines.append("\t".join(header))
        for override, res in rows:
            vals = [str(override[sweep["param"]])] + [fmt_value(res.get(n.lower())) for n in names]
            lines.append("\t".join(vals))
    else:
        res = rows[0][1]
        w = max((len(n) for n in names), default=4)
        for n in names:
            lines.append(f"{n.ljust(w)} = {fmt_value(res.get(n.lower()))}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LTspice .asc 자동 생성 + 측정값 txt 추출")
    ap.add_argument("spec", type=Path, help="회로 사양 파일 (.yaml / .json)")
    ap.add_argument("-o", "--out", type=Path, default=None, help="출력 폴더 (기본: output/<name>)")
    ap.add_argument("--sim", choices=["auto", "ltspice", "ngspice"], default="auto")
    ap.add_argument("--no-sim", action="store_true", help="회로도/넷리스트만 생성")
    args = ap.parse_args(argv)

    spec = load_spec(args.spec)
    name = spec.get("name") or args.spec.stem
    out = args.out or Path("output") / name
    out.mkdir(parents=True, exist_ok=True)

    comps = parse_components(spec)
    auto_layout(comps)

    asc = out / f"{name}.asc"
    write_asc(spec, comps, asc)
    print(f"[회로도] {asc}")

    if args.no_sim:
        write_net(spec, comps, out / f"{name}.net", None, "ltspice")
        print(f"[넷리스트] {out / (name + '.net')}")
        return 0

    sim = pick_sim(args.sim)
    if sim is None:
        write_net(spec, comps, out / f"{name}.net", None, "ltspice")
        print("[알림] LTspice/ngspice 를 찾지 못해 시뮬레이션은 건너뜁니다. (.asc 를 LTspice 에서 직접 실행하세요)")
        return 0

    sweep = spec.get("sweep")
    overrides = [{sweep["param"]: v} for v in sweep["values"]] if sweep else [{}]
    rows = []
    for i, ov in enumerate(overrides):
        suffix = f"_step{i + 1}" if sweep else ""
        net = out / f"{name}{suffix}.net"
        write_net(spec, comps, net, ov, sim)
        res, _ = run_sim(net, sim)
        rows.append((ov, res))
        tag = f" ({sweep['param']}={ov[sweep['param']]})" if sweep else ""
        print(f"[시뮬레이션{tag}] {net.name} -> {len(res)}개 측정값")

    txt = out / f"{name}_results.txt"
    write_txt(spec, rows, sim, txt)
    print(f"[결과] {txt}")
    print(txt.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
