#!/usr/bin/env python3
"""spike 1: spec (YAML) -> MIDI + ASCII piano roll + lint.

编制是输入（ensembles/*.yaml + instruments/*.yaml），不是代码里的常量。
除 PyYAML 外零依赖：内环必须能 headless 跑，2 秒内出声。
"""
import os
import sys
import yaml

PITCH_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
MAJOR = [0, 2, 4, 5, 7, 9, 11]


# ---------- 编制（输入）----------

def load_ensemble(spec, spec_path):
    """编制是输入参数：谁在场、什么音区、什么移调，全从 YAML 读。"""
    rel = spec.get("ensemble")
    if not rel:
        raise SystemExit("spec 里缺 ensemble: —— 编制必须显式声明，不能隐含")
    ens_path = os.path.join(os.path.dirname(os.path.abspath(spec_path)), rel)
    ens = yaml.safe_load(open(ens_path))
    inst_dir = os.path.join(os.path.dirname(ens_path), "..", "instruments")

    insts, ch, dup = {}, 0, []
    for seat in ens["seats"]:
        base = seat["instrument"]
        d = dict(yaml.safe_load(open(os.path.join(inst_dir, base + ".yaml"))))
        d.update(seat.get("overrides") or {})
        inst = seat.get("as") or base          # 座位可起别名：同一乐器坐两个人
        if inst in insts:
            dup.append(inst)
        d["id"] = inst
        d["name"] = seat.get("name") or d["name"]
        d["base"] = base
        if d.get("drum"):
            d["channel"] = 9
        else:
            ch += (ch == 9)
            d["channel"] = ch
            ch += 1
        d["seat"] = seat
        insts[inst] = d
    if dup:
        raise SystemExit(f"座位 id 重複（必须唯一）: {sorted(set(dup))}")
    return ens, insts


# ---------- spec 展开 ----------

def parse_bars(spec, total):
    if isinstance(spec, int):
        return [spec]
    s = str(spec)
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def chord_at(harmony, bar):
    for h in harmony:
        if h["bar"] == bar:
            return h
    raise KeyError(bar)


def note_name(p):
    return f"{PITCH_NAMES[p % 12]}{p // 12 - 1}"


def scale_degree(deg, base_octave, transpose=0):
    """简谱式：1..7 为 C 大调音级；8 = 高八度 1；0 = 低八度 7。"""
    semis = MAJOR[(deg - 1) % 7] + 12 * ((deg - 1) // 7)
    return (base_octave + 1) * 12 + semis + transpose


def resolve(pitch, h, part, prev=None):
    """和弦音型 -> 具体音高。注意：音程必须从**和弦根音**算，不是从 C。"""
    base = (part["base_octave"] + 1) * 12
    root_pc = h["chord"][0]
    root = base + root_pc
    iv = lambda pc: (pc - root_pc) % 12
    if pitch == "root":
        p = (part["base_octave"] + 1) * 12 + h["bass"]
        while p < base:
            p += 12
        return [p]
    if pitch == "fifth":
        return [root + 7]
    if pitch == "third":
        return [root + iv(h["chord"][1])]
    if pitch == "octave":
        return [root + 12]
    if pitch == "power":
        return [root, root + 7]
    if pitch == "chord":
        return [root + iv(pc) for pc in h["chord"]]
    if pitch == "wide":                     # 左手根音 + 右手和弦（贴着旋律音区，容易糊）
        return [root - 12] + [root + iv(pc) for pc in h["chord"]]
    if pitch == "frame":                    # 框：低音在下、和弦整体上一个八度，把中频让给旋律
        return [root - 12] + [root + 12 + iv(pc) for pc in h["chord"]]
    if pitch == "arp":
        return [root + iv(pc) for pc in h["chord"]]
    if pitch == "nearest":
        tones = [root + iv(pc) + 12 * k for pc in h["chord"] for k in (-1, 0, 1)]
        if prev is None:
            return [min(t for t in tones if t >= base - 6)]
        return [min(tones, key=lambda t: (abs(t - prev), t))]
    raise ValueError(pitch)


def onsets(rhythm, grid):
    return [i for i, c in enumerate((rhythm + "-" * grid)[:grid]) if c == "x"]


def runs(rhythm, grid):
    ons = onsets(rhythm, grid)
    return [(o, (ons[i + 1] if i + 1 < len(ons) else grid) - o) for i, o in enumerate(ons)]


# ---------- 变换算子 ----------
# 位移一律显式（C16）：register 只决定简谱音级落在哪个八度，不参与 use 的位移。

def _pairs(ops):
    """op 列表允许混写 `retrograde` 与 `{octave: -1}`。"""
    for op in ops:
        if isinstance(op, str):
            yield op, None
        elif isinstance(op, dict) and len(op) == 1:
            yield next(iter(op.items()))
        else:
            raise SystemExit(f"变换写法不对: {op!r}（应为 '名字' 或 {{名字: 参数}}）")


def _octave(seq, n):
    return [(a, d, [p + 12 * int(n) for p in ps], v) for (a, d, ps, v) in seq]


def _transpose(seq, n):
    return [(a, d, [p + int(n) for p in ps], v) for (a, d, ps, v) in seq]


def _retrograde(seq, _):
    if not seq:
        return seq
    end = max(a + d for a, d, _p, _v in seq)
    return sorted(((end - a - d, d, ps, v) for (a, d, ps, v) in seq), key=lambda e: e[0])


def _invert(seq, axis):
    """旋律倒影。axis 缺省 = 第一个音（手算得出来，不用猜调）。"""
    if not seq:
        return seq
    ax = seq[0][2][0] if axis in (None, "first") else int(axis)
    return [(a, d, [2 * ax - p for p in ps], v) for (a, d, ps, v) in seq]


def _augment(seq, k):
    k = int(k)
    return [(a * k, d * k, ps, v) for (a, d, ps, v) in seq]


def _diminish(seq, k):
    k = int(k)
    out = []
    for (a, d, ps, v) in seq:
        if a % k or d % k:
            raise SystemExit(f"diminish:{k} 在 {a}:{d} 上除不尽——网格太粗，这个变换写不出来")
        out.append((a // k, d // k, ps, v))
    return out


def _fragment(seq, rng):
    a, b = rng
    sub = seq[a:b + 1]
    if not sub:
        return []
    base = min(x[0] for x in sub)
    return sorted(((x[0] - base, x[1], x[2], x[3]) for x in sub), key=lambda e: e[0])


def _sequence(seq, arg):
    """模进：原样重复 times 次，每次上行 step 半音。"""
    if not seq:
        return seq
    step = int((arg or {}).get("step", 2))
    times = int((arg or {}).get("times", 3))
    span = max(a + d for a, d, _p, _v in seq)
    out = []
    for i in range(times):
        for (a, d, ps, v) in seq:
            out.append((a + i * span, d, [p + i * step for p in ps], v))
    return out


OPS = {"octave": _octave, "transpose": _transpose, "retrograde": _retrograde,
       "invert": _invert, "augment": _augment, "diminish": _diminish,
       "fragment": _fragment, "sequence": _sequence}


def apply_ops(seq, ops):
    """从左到右依次作用。顺序有意义：先 fragment 后 retrograde ≠ 反过来。"""
    for name, arg in _pairs(ops or []):
        fn = OPS.get(name)
        if fn is None:
            raise SystemExit(f"未知变换 {name!r}（可用: {', '.join(sorted(OPS))}）")
        seq = fn(seq, arg)
    return seq


def op_shift(ops):
    """从变换链里抽出纯位移：返回 (总半音, 是否只是位移)。"""
    semis, pure = 0, True
    for name, arg in _pairs(ops or []):
        if name == "octave":
            semis += 12 * int(arg)
        elif name == "transpose":
            semis += int(arg)
        else:
            pure = False
    return semis, pure


def build_material(spec):
    """素材轨：与声部无关的时间线。轨不知道自己由谁演奏。

    每个 timeline 段先展开成一条**扁平序列**（绝对步数），再过变换链，
    最后重新分配到小节。扁平化是必须的：倒影/扩增跨小节，逐小节做不了。
    """
    grid = spec["meta"]["grid"]
    tracks, warns = {}, []
    for tr in spec.get("material") or []:
        reg = tr.get("register", 4)
        ev, lineage = {}, []
        for seg in tr["timeline"]:
            target = parse_bars(seg["bars"], None)
            start_bar = target[0]
            ops = seg.get("op") or []

            if "use" in seg:
                src = tracks.get(seg["use"])
                if src is None:
                    raise SystemExit(f"素材 {tr['id']} 引用了尚未定义的轨 {seg['use']}")
                src_bars = parse_bars(seg.get("from", seg["bars"]), None)
                base = src_bars[0]
                seq = [((b - base) * grid + st, du, ps, v)
                       for b in src_bars for (st, du, ps, v) in src["events"].get(b, [])]
                semis, pure = op_shift(ops)
                lineage.append({"from": seg["use"], "from_bars": seg.get("from", seg["bars"]),
                                "bars": seg["bars"], "ops": ops,
                                "semitones": semis, "pure": pure, "tonal": semis % 12 != 0})
            elif "degrees" in seg:
                seq, step = [], 0
                for tok in seg["degrees"].split():
                    deg_s, dur = tok.split(":")
                    shift = 0
                    while deg_s and deg_s[-1] in "'\u2019,":
                        shift += 7 if deg_s[-1] in "'\u2019" else -7
                        deg_s = deg_s[:-1]
                    seq.append((step, int(dur), [scale_degree(int(deg_s) + shift, reg)],
                                seg.get("velocity", 80)))
                    step += int(dur)
                lineage.append({"from": None, "from_bars": None, "bars": seg["bars"],
                                "ops": ops, "semitones": 0, "pure": False, "tonal": False})
            else:
                raise SystemExit(f"素材 {tr['id']} 的 timeline 段既无 degrees 也无 use: {seg}")

            seq = apply_ops(seq, ops)

            # 变换会改变长度：装不下就报，不要默默截断（C14）
            if seq:
                need = -(-max(a + d for a, d, _p, _v in seq) // grid)
                if need > len(target):
                    raise SystemExit(
                        f"素材 {tr['id']} {seg['bars']}: 变换后需要 {need} 小节，"
                        f"但只给了 {len(target)} 小节")
                if need < len(target):
                    warns.append(f"素材 {tr['id']} {seg['bars']}: 变换只填满 {need}/{len(target)} 小节，"
                                 f"其余为静默")
            for (a, du, ps, v) in seq:
                if a < 0:
                    raise SystemExit(f"素材 {tr['id']} {seg['bars']}: 变换产生了负位置")
                ev.setdefault(start_bar + a // grid, []).append((a % grid, du, ps, v))

        tracks[tr["id"]] = {"register": reg, "events": ev,
                             "theme": bool(tr.get("theme")), "lineage": lineage}
    return tracks, warns


def expand(spec, insts):
    grid = spec["meta"]["grid"]
    total = len(spec["harmony"])
    harmony = spec["harmony"]
    bars = {}
    tracks, mat_warns = build_material(spec)

    parts = []
    for ps in spec["parts"]:
        d = dict(insts[ps["instrument"]])
        d.update({k: v for k, v in ps.items() if k != "instrument"})
        d["id"] = ps["instrument"]
        d.setdefault("base_octave", 4)
        parts.append(d)

    for part in parts:
        is_drum = part.get("drum", False)

        # 素材：这个声部演奏哪条轨。音区由轨决定，不在这里移八度（C16）。
        for seg in part.get("carries") or []:
            if part.get("place") == "auto":
                raise SystemExit(
                    f"{part['name']}: 承载素材的声部不能用 place: auto —— "
                    f"音区位移必须显式声明（C16）")
            tr = tracks.get(seg["track"])
            if tr is None:
                raise SystemExit(f"{part['name']}: 承载了不存在的素材轨 {seg['track']}")
            part["_carrier"] = True
            if tr["theme"]:
                part["_theme"] = True
            for b in parse_bars(seg["bars"], total):
                for (st, du, ps, v) in tr["events"].get(b, []):
                    bars.setdefault(b, []).append((part, st, du, ps, v))

        for seg in part.get("segments") or []:
            blist = parse_bars(seg["bars"], total)
            if seg.get("rest"):
                for b in blist:
                    bars.setdefault(b, [])
                continue

            for b in blist:
                h = chord_at(harmony, b)
                vel, tr = seg.get("velocity", 80), seg.get("transpose", 0)
                evs = []
                if "degrees" in seg:
                    step = 0
                    for tok in seg["degrees"].split():
                        deg_s, dur = tok.split(":")
                        shift = 0
                        while deg_s and deg_s[-1] in "'\u2019,":
                            shift += 7 if deg_s[-1] in "'\u2019" else -7
                            deg_s = deg_s[:-1]
                        evs.append((step, int(dur),
                                    [scale_degree(int(deg_s) + shift, part["base_octave"], tr)], vel))
                        step += int(dur)
                elif "pattern" in seg:
                    layers = [(seg["pattern"], seg.get("velocity", 80))]
                    if "also" in seg:
                        layers.append((seg["also"], seg.get("velocity", 80)))
                    if "extra" in seg:
                        layers.append((seg["extra"], max(30, seg.get("velocity", 80) - 30)))
                    for pat, lvel in layers:
                        pitch, rhythm = pat["pitch"], pat["rhythm"]
                        if is_drum:
                            dmap = part["drum_map"]
                            for st, du in runs(rhythm, grid):
                                evs.append((st, 1, [dmap[pitch]], lvel))
                        elif pitch == "arp":
                            tones = resolve("arp", h, part)
                            for st, du in runs(rhythm, grid):
                                evs.append((st, du, [tones[(st // 2) % len(tones)]], lvel))
                        else:
                            prev = part.get("_prev")
                            ps = resolve(pitch, h, part, prev)
                            part["_prev"] = ps[0]
                            for st, du in runs(rhythm, grid):
                                evs.append((st, du, [p + tr for p in ps], lvel))
                bars.setdefault(b, []).extend((part, st, du, ps, v) for (st, du, ps, v) in evs)

    return bars, parts, tracks, mat_warns


# ---------- 音区：推荐 / 人为覆盖 / 自动择位 ----------

def autoplace(bars, parts, grid):
    """place: auto —— 选一个八度，使材料尽量待在**舒适区**里（C1 的"推荐"）。"""
    for part in parts:
        if part.get("place") != "auto" or part.get("drum"):
            continue
        lo_r, hi_r = part["range"]
        lo_c, hi_c = part.get("comfort") or part["range"]
        best = None
        for shift in range(-24, 25, 12):
            cost = 0
            for b, evs in bars.items():
                for (p, st, du, ps, v) in evs:
                    if p is not part:
                        continue
                    for x in ps:
                        y = x + shift
                        cost += du * (100 if not (lo_r <= y <= hi_r) else
                                      (1 if not (lo_c <= y <= hi_c) else 0))
            if best is None or cost < best[0]:
                best = (cost, shift)
        shift = best[1]
        if shift:
            part["_shift"] = shift
            for b in bars:
                bars[b] = [(p, st, du, ([x + shift for x in ps] if p is part else ps), v)
                           for (p, st, du, ps, v) in bars[b]]
    return bars


# ---------- 在场路径 ----------

def apply_presence(spec, bars):
    ladder = spec.get("presence")
    if not ladder:
        return bars, []
    declared = {}
    for seg in ladder:
        for b in parse_bars(seg["bars"], len(spec["harmony"])):
            declared[b] = set(seg["parts"])
    out, issues = {}, []
    for b, evs in bars.items():
        allowed = declared.get(b)
        if allowed is None:
            out[b] = evs
            continue
        kept = [e for e in evs if e[0]["name"] in allowed]
        for (part, *_r) in evs:
            if part["name"] not in allowed:
                issues.append(f"bar {b}: {part['name']} 有音符但不在场路径里（已静音）")
        for name in sorted(allowed):
            if not any(e[0]["name"] == name for e in kept):
                issues.append(f"bar {b}: {name} 声明在场但没有音符")
        out[b] = kept
    return out, sorted(set(issues))


# ---------- 检查 ----------

def check_harmony(spec, bars, parts):
    problems = []
    skip = {p["id"] for p in parts if p.get("_carrier")}
    for b, evs in bars.items():
        h = chord_at(spec["harmony"], b)
        allowed = {pc % 12 for pc in h["chord"]} | {h["bass"] % 12}
        for (part, st, du, pitches, vel) in evs:
            if part.get("drum") or part["id"] in skip:
                continue
            for p in pitches:
                if p % 12 not in allowed:
                    problems.append(
                        f"bar {b} {part['name']}: {note_name(p)} 不属于 {h['name']}")
    return sorted(set(problems))


def lint(bars, parts):
    errors, warns = [], []
    for b in sorted(bars):
        for (part, st, du, pitches, vel) in bars[b]:
            if part.get("drum") or not part.get("range"):
                continue
            lo_r, hi_r = part["range"]
            lo_c, hi_c = part.get("comfort") or (lo_r, hi_r)
            for p in pitches:
                if not (lo_r <= p <= hi_r):
                    errors.append(f"bar {b} {part['name']}: {note_name(p)} 超出可演奏音域 "
                                  f"{note_name(lo_r)}–{note_name(hi_r)}")
                elif not (lo_c <= p <= hi_c):
                    warns.append(f"bar {b} {part['name']}: {note_name(p)} 超出舒适区 "
                                 f"{note_name(lo_c)}–{note_name(hi_c)}")
    for part in parts:
        seen = {}
        for seg in part.get("segments") or []:
            for b in parse_bars(seg["bars"], max(bars) or 1):
                if b in seen and not (seen[b].get("rest") and seg.get("rest")):
                    errors.append(f"bar {b} {part['name']}: 段落重叠"
                                  f"（{seen[b].get('bars')} 与 {seg.get('bars')}）")
                seen[b] = seg
    return sorted(set(errors)), sorted(set(warns))


# ---------- 素材血统 ----------

def _fmt_op(name, arg):
    if arg is None:
        return name
    if isinstance(arg, dict):
        return f"{name}({', '.join(f'{k}={v}' for k, v in arg.items())})"
    if isinstance(arg, list):
        return f"{name}[{', '.join(map(str, arg))}]"
    return f"{name}({arg})"


def lineage(tracks):
    """每个派生素材都要能说出“我是谁的什么变换”——否则 fidelity 无法追溯。"""
    print("\n  素材血统:")
    for tid, t in tracks.items():
        segs = [s for s in t["lineage"] if s["from"]]
        if not segs:
            print(f"    {tid:<12} —（原始）")
            continue
        for s in segs:
            ops = " ".join(_fmt_op(n, a) for n, a in _pairs(s["ops"])) or "—"
            if s["pure"]:
                tag = "移调" if s["tonal"] else "纯音区"
                note = f"{tag} {s['semitones']:+d}半音"
            else:
                note = "非纯位移（音级关系已变）"
            frm = f"{s['from']}[{s['from_bars']}]"
            print(f"    {tid:<12} ← {frm:<22} → [{s['bars']}]  {ops:<24} {note}")


# ---------- 自检 ----------

def selftest():
    """每个变换配一条小到能手算的样例（R7：测量本身会撒谎）。"""
    seq = [(0, 4, [60], 80), (4, 4, [64], 80), (8, 8, [67], 80)]
    cases = [
        (["retrograde"], [(0, 8, [67], 80), (8, 4, [64], 80), (12, 4, [60], 80)]),
        ([{"octave": -1}], [(0, 4, [48], 80), (4, 4, [52], 80), (8, 8, [55], 80)]),
        ([{"transpose": 2}], [(0, 4, [62], 80), (4, 4, [66], 80), (8, 8, [69], 80)]),
        ([{"invert": "first"}], [(0, 4, [60], 80), (4, 4, [56], 80), (8, 8, [53], 80)]),
        ([{"augment": 2}], [(0, 8, [60], 80), (8, 8, [64], 80), (16, 16, [67], 80)]),
        ([{"diminish": 2}], [(0, 2, [60], 80), (2, 2, [64], 80), (4, 4, [67], 80)]),
        ([{"fragment": [1, 2]}], [(0, 4, [64], 80), (4, 8, [67], 80)]),
        ([{"sequence": {"step": 2, "times": 2}}],
         [(0, 4, [60], 80), (4, 4, [64], 80), (8, 8, [67], 80),
          (16, 4, [62], 80), (20, 4, [66], 80), (24, 8, [69], 80)]),
        ([{"transpose": 2}, "retrograde"],
         [(0, 8, [69], 80), (8, 4, [66], 80), (12, 4, [62], 80)]),
    ]
    bad = 0
    for ops, want in cases:
        got = apply_ops(list(seq), ops)
        ok = got == want
        bad += not ok
        print(f"  {'✓' if ok else '✗'} {[ _fmt_op(n, a) for n, a in _pairs(ops)]}")
        if not ok:
            print(f"      want {want}\n      got  {got}")
    print(f"\n  {len(cases) - bad}/{len(cases)} 通过")
    raise SystemExit(1 if bad else 0)


# ---------- 演奏可行性（把 override 变成可量化的东西）----------

def report(bars, parts, grid=16):
    """每条都是可量的：舒适区时长占比、最长连续、大跳、最高音。"""
    steps_per_beat = grid / 4
    rows = []
    for part in parts:
        evs = [(b, st, du, ps) for b in sorted(bars)
               for (p, st, du, ps, v) in bars[b] if p is part]
        if not evs or part.get("drum"):
            continue
        iv = sorted(((b - 1) * grid + st, (b - 1) * grid + st + du, ps) for b, st, du, ps in evs)
        # 合并重叠 -> 连续演奏段
        runs, cur = [], None
        for a, z, ps in iv:
            if cur and a <= cur[1]:
                cur[1] = max(cur[1], z)
                cur[2].extend(ps)
            else:
                if cur:
                    runs.append(cur)
                cur = [a, z, list(ps)]
        if cur:
            runs.append(cur)
        longest = max((z - a) for a, z, _ in runs) / steps_per_beat if runs else 0
        total = sum(z - a for a, z, _ in runs) / steps_per_beat

        lo_r, hi_r = part["range"]
        lo_c, hi_c = part.get("comfort") or (lo_r, hi_r)
        out_c = sum((z - a) for a, z, ps in runs
                    if any(not (lo_c <= x <= hi_c) for x in ps)) / steps_per_beat
        out_r = sum((z - a) for a, z, ps in runs
                    if any(not (lo_r <= x <= hi_r) for x in ps)) / steps_per_beat

        leaps, top = 0, None
        if part.get("monophonic"):
            seq = [ps[0] for _b, _s, _d, ps in evs]
            leaps = sum(1 for x, y in zip(seq, seq[1:]) if abs(y - x) > 7)
        top = max((x for *_r, ps in evs for x in ps), default=None)

        cap = (part.get("stamina") or {}).get("max_continuous_beats")
        rows.append({
            "name": part["name"], "total": total, "longest": longest, "cap": cap,
            "over": longest > cap if cap else False,
            "comfort": 100 * (1 - out_c / total) if total else 100,
            "range_bad": out_r,
            "leaps": leaps, "top": note_name(top) if top is not None else "—",
        })
    if not rows:
        return
    print("\n  演奏可行性（每一条都是量的，不是形容词）:")
    print(f"    {'声部':<16}{'发声':>6}{'连续/上限':>12}{'舒适区':>8}{'越域':>7}{'大跳':>6}{'最高':>7}")
    for r in rows:
        cap = f"{r['longest']:.0f}/{r['cap']:.0f}" if r["cap"] else f"{r['longest']:.0f}/—"
        flag = " ✗" if r["over"] else (" !" if r["comfort"] < 90 else "")
        print(f"    {r['name'][:15]:<16}{r['total']:>5.0f}b{cap:>12}"
              f"{r['comfort']:>7.0f}%{r['range_bad']:>6.0f}b{r['leaps']:>6}{r['top']:>7}{flag}")


# ---------- MIDI ----------

def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def meta_text(text):
    b = text.encode()
    return b"\xff\x03" + vlq(len(b)) + b


def track_chunk(events):
    events = sorted(events, key=lambda e: (e[0], e[1]))
    data, last = b"", 0
    for tick, _o, payload in events:
        data += vlq(tick - last) + payload
        last = tick
    data += vlq(0) + b"\xff\x2f\x00"
    return b"MTrk" + len(data).to_bytes(4, "big") + data


def dynamics_map(spec):
    lvl = {}
    for seg in spec.get("dynamics", []):
        for b in parse_bars(seg["bars"], len(spec["harmony"])):
            lvl[b] = seg["level"]
    return lvl


def build_midi(spec, bars, parts, melody=True, first=1):
    """first = 输出时间轴上的第 1 小节对应原谱的哪一小节。

    内环只想要第 17-20 小节时，传 first=16（多留一小节 preroll，
    让从前一小节拖过来的音保留起音），然后按时间切音频。
    """
    meta = spec["meta"]
    div, grid = meta["division"], meta["grid"]
    tps = div // (grid // meta["meter"][0])

    conductor = [(0, 0, b"\xff\x51\x03" + int(60_000_000 / meta["tempo"]).to_bytes(3, "big")),
                 (0, 1, b"\xff\x58\x04" + bytes([meta["meter"][0], 2, 24, 8])),
                 (0, 2, meta_text(meta["title"]))]
    tracks = [track_chunk(conductor)]
    lvl = dynamics_map(spec)

    for part in parts:
        if not melody and part.get("_theme"):
            tracks.append(track_chunk([(0, 0, meta_text("(omitted)"))]))
            continue
        ch = part["channel"]
        evs = [(0, 0, meta_text(part["name"])), (0, 1, bytes([0xC0 | ch, part["gm_program"]]))]
        for bar in sorted(bars):
            if bar in lvl:
                evs.append(((bar - first) * grid * tps, 1, bytes([0xB0 | ch, 7, int(lvl[bar] * 127)])))
            for (p, st, du, pitches, vel) in bars[bar]:
                if p is not part:
                    continue
                t0 = (bar - first) * grid * tps + st * tps
                t1 = t0 + du * tps
                for i, pitch in enumerate(pitches):
                    evs.append((t0, 2 + i, bytes([0x90 | ch, pitch & 0x7F, min(127, vel)])))
                    evs.append((t1, 2 + i, bytes([0x80 | ch, pitch & 0x7F, 0])))
        tracks.append(track_chunk(evs))

    header = (b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big")
              + len(tracks).to_bytes(2, "big") + div.to_bytes(2, "big"))
    return header + b"".join(tracks)


# ---------- 输出 ----------

def roll(spec, bars, parts):
    grid = spec["meta"]["grid"]
    total = len(spec["harmony"])
    print()
    for start in range(1, total + 1, 4):
        block = list(range(start, min(start + 4, total + 1)))
        head = "".join(f"|{chord_at(spec['harmony'], b)['name']:<16}" for b in block) + "|"
        print(f"  bar {block[0]:>2}-{block[-1]:<2}  {head}")
        for part in parts:
            cells = []
            for b in block:
                line = ["."] * grid
                for (p, st, du, pitches, vel) in bars.get(b, []):
                    if p is not part:
                        continue
                    for k in range(st, min(grid, st + du)):
                        line[k] = "#" if k == st else "|"
                cells.append("".join(line))
            print(f"  {part['name'][:9]:<10}" + " ".join(cells))
        print()


def presence(spec, bars, parts):
    print("  bar  chord        n")
    for b in range(1, len(spec["harmony"]) + 1):
        names = sorted({p["name"] for (p, *_r) in bars.get(b, [])})
        print(f"  {b:>3}  {chord_at(spec['harmony'], b)['name']:<12} {len(names)}  {', '.join(names)}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "farewell-v3.yaml"
    if path == "--selftest":
        selftest()
    spec = yaml.safe_load(open(path))
    ens, insts = load_ensemble(spec, path)

    bars, parts, tracks, mat_warns = expand(spec, insts)
    bars = autoplace(bars, parts, spec["meta"]["grid"])
    bars, pis = apply_presence(spec, bars)

    out = path.rsplit(".", 1)[0]
    for name, with_mel in ((out + ".mid", True), (out + "-nomelody.mid", False)):
        m = build_midi(spec, bars, parts, melody=with_mel)
        open(name, "wb").write(m)
        print(f"wrote {name}  ({len(m)} bytes)")

    print(f"\n  编制: {ens.get('name','')}  座位 {len(parts)}")
    for p in parts:
        flag = "  ← 待确认" if p["seat"].get("confirm") else ""
        sh = f"  自动择位 {p['_shift']:+d}" if p.get("_shift") else ""
        print(f"    ch{(p['channel'] if p['channel'] < 9 else p['channel']+1):>2} "
              f"#{p['gm_program']:<3} {p['name']:<16} "
              f"音域 {note_name(p['range'][0]) if p.get('range') else '—'}"
              f"–{note_name(p['range'][1]) if p.get('range') else '—'}  "
              f"舒适 {note_name(p['comfort'][0]) if p.get('comfort') else '—'}"
              f"–{note_name(p['comfort'][1]) if p.get('comfort') else '—'}{sh}{flag}")

    if pis:
        print("\n  在场路径检查:")
        for m in pis:
            print("    - " + m)
    lineage(tracks)
    for m in mat_warns:
        print("    ! " + m)
    spelling = check_harmony(spec, bars, parts)
    print("\n  和声拼写自检:")
    print("    (clean)" if not spelling else "")
    for m in spelling[:20]:
        print("    - " + m)
    errors, warns = lint(bars, parts)
    print("\n  lint:")
    for m in errors:
        print("    ✗ " + m)
    for m in warns[:15]:
        print("    ! " + m)
    if not errors and not warns:
        print("    (clean)")
    elif not errors:
        print(f"    （{len(warns)} 条提示，无错误）")

    report(bars, parts, spec["meta"]["grid"])
    roll(spec, bars, parts)
    presence(spec, bars, parts)


if __name__ == "__main__":
    main()
