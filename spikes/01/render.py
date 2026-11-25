#!/usr/bin/env python3
"""spike 1: spec (YAML) -> MIDI + ASCII piano roll + lint.

编制是输入（ensembles/*.yaml + instruments/*.yaml），不是代码里的常量。
除 PyYAML 外零依赖：内环必须能 headless 跑，2 秒内出声。
"""
import bisect
import itertools
import os
import sys
from typing import Any

import yaml

PITCH_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
MAJOR = [0, 2, 4, 5, 7, 9, 11]


def load_yaml(path, what) -> Any:
    """读一个 YAML 文件，出错时说明白是哪个文件、找的是什么。

    返回 Any：YAML 本来就没有静态类型，与其假装有，不如让检查器闭嘴。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(f"找不到{what}: {path}") from None
    except yaml.YAMLError as e:
        raise SystemExit(f"{what} 不是合法 YAML: {path}\n{e}") from e


# ---------- 编制（输入）----------

def load_ensemble(spec, spec_path):
    """编制是输入参数：谁在场、什么音区、什么移调，全从 YAML 读。"""
    rel = spec.get("ensemble")
    if not rel:
        raise SystemExit("spec 里缺 ensemble: —— 编制必须显式声明，不能隐含")
    ens_path = os.path.join(os.path.dirname(os.path.abspath(spec_path)), rel)
    ens = load_yaml(ens_path, "编制文件")
    inst_dir = os.path.join(os.path.dirname(ens_path), "..", "instruments")

    insts, ch, dup = {}, 0, []
    for seat in ens["seats"]:
        base = seat["instrument"]
        d = dict(load_yaml(os.path.join(inst_dir, base + ".yaml"), f"乐器 {base}"))
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


# ---------- 拍号：小节是有长度的坐标，不是序号 ----------
# 4/4 里 `tick = (bar - 1) * grid * tps` 恒成立，所以写死了也没人发现。
# 一旦有 7/8 这个等式就是错的——这不是加复杂度，是把一条假等式删掉。

UNITS = (2, 4, 8, 16)          # 拍号分母：MIDI 只能表示 2 的幂


class Bar:
    """一小节的绝对时间坐标。拍号变了，长度和步长都跟着变。"""

    # 不按字母排：这个顺序是“时间 → 拍号 → 推导量”，读的时候能顺着算一遍。
    __slots__ = (  # noqa: RUF023
        "index", "start_tick", "ticks", "beats", "unit",
        "grid", "step_ticks", "eighth", "group")

    def __init__(self, index, start_tick, beats, unit, grid, step_ticks, eighth, group):
        self.index = index
        self.start_tick = start_tick
        self.beats = beats
        self.unit = unit
        self.grid = grid                 # 格子数 = beats * subdivision
        self.step_ticks = step_ticks     # 一格多少 tick
        self.ticks = grid * step_ticks   # 一小节多少 tick（推导，不写死）
        self.eighth = eighth             # 一个八分音符占几格（arp 之类要用）
        self.group = group               # 小节内分组 [3, 2, 3]，没有就是 None

    def __repr__(self):
        return f"<bar {self.index} {self.beats}/{self.unit} @{self.start_tick}>"


class Meter:
    """全曲时间坐标。按小节号、按 tick、按秒都查得到——时间的唯一真源。"""

    def __init__(self, bars, tempo=None, division=None):
        self.bars = bars
        self._starts = [b.start_tick for b in bars]
        self.tempo = tempo
        self.division = division

    def __len__(self):
        return len(self.bars)

    def __contains__(self, index):
        return isinstance(index, int) and 1 <= index <= len(self.bars)

    def __getitem__(self, index):
        if not 1 <= index <= len(self.bars):
            raise SystemExit(f"第 {index} 小节不存在（全曲 {len(self.bars)} 小节）")
        return self.bars[index - 1]

    def all(self):
        return list(self.bars)

    def at_tick(self, tick):
        i = bisect.bisect_right(self._starts, tick) - 1
        if i < 0:
            raise SystemExit(f"tick {tick} 落在第 1 小节之前")
        return self.bars[i]

    # --- 秒 ---
    # 小节的秒数不是常数（7/8 比 4/4 短），所以“第 n 小节在几秒”只能累加 tick。
    def _spb(self):
        if not self.tempo or not self.division:
            raise SystemExit("Meter 没有 tempo/division，算不了秒")
        return 60.0 / (self.tempo * self.division)

    def secs(self, bar):
        """第 bar 小节起点的秒数（从第 1 小节算）。"""
        return self[bar].start_tick * self._spb()

    def bar_secs(self, bar):
        """第 bar 小节的秒数。"""
        return self[bar].ticks * self._spb()

    def total_secs(self):
        """全曲总长（乐谱长度，不含渲染尾音）。"""
        return sum(b.ticks for b in self.bars) * self._spb()


def meter_of(spec):
    """把 meta.meter 段落展开成 Meter。网格和 tick 全是推导的，不手写。"""
    meta = spec["meta"]
    total = len(spec["harmony"])
    div = int(meta["division"])
    sub = int(meta.get("subdivision", 4))
    if sub < 1:
        raise SystemExit(f"meta.subdivision={sub} 必须 ≥ 1")
    if div % sub:
        raise SystemExit(f"meta.division={div} 不能被 subdivision={sub} 整除")
    segs = meta.get("meter") or [{"bar": 1, "beats": 4, "unit": 4}]
    if (isinstance(segs, list) and len(segs) == 2
            and all(isinstance(x, int) for x in segs)):
        # 旧写法 meter: [4, 4] —— 只能表达单一拍号，等价于一段。
        segs = [{"bar": 1, "beats": segs[0], "unit": segs[1]}]
    if not isinstance(segs, list) or not all(isinstance(s, dict) for s in segs):
        raise SystemExit(
            f"meta.meter 要是一串段落，比如 [{{bar: 1, beats: 4, unit: 4}}]，"
            f"拿到 {segs!r}。只写一行会被当成 dict。")

    blocks = []
    for seg in segs:
        for key in ("bar", "beats", "unit"):
            if key not in seg:
                raise SystemExit(f"拍号段落缺 {key}: {seg}")
        b0, beats, unit = int(seg["bar"]), int(seg["beats"]), int(seg["unit"])
        if unit not in UNITS:
            raise SystemExit(f"bar {b0}: unit={unit} 不合法（MIDI 的拍号分母只能是 {UNITS}）")
        if beats < 1:
            raise SystemExit(f"bar {b0}: beats={beats} 必须 ≥ 1")
        if div * 4 % (unit * sub):
            raise SystemExit(
                f"bar {b0}: division={div} 表示不了 {beats}/{unit} 的第 {sub} 刀"
                f"——{div * 4} 不能被 {unit * sub} 整除。换 division 或 subdivision。")
        group = seg.get("group")
        if group is not None:
            if not isinstance(group, list) or any(
                    not isinstance(g, int) or g < 1 for g in group):
                raise SystemExit(f"bar {b0}: group={group!r} 要是正整数的列表，比如 [3, 2, 3]")
            if sum(group) != beats:
                raise SystemExit(
                    f"bar {b0}: group {group} 加起来是 {sum(group)} 拍，但这一小节有 {beats} 拍")
        step = div * 4 // (unit * sub)
        blocks.append((b0, beats, unit, group, step, beats * sub, max(1, (div // 2) // step)))

    if blocks[0][0] != 1:
        raise SystemExit(f"拍号必须从第 1 小节开始，第一段写的是 bar {blocks[0][0]}")
    for i in range(1, len(blocks)):
        if blocks[i][0] <= blocks[i - 1][0]:
            raise SystemExit(
                f"拍号段落顺序不对或重叠：bar {blocks[i][0]} 排在 bar {blocks[i - 1][0]} 之后")

    out, tick = [], 0
    for i, (b0, beats, unit, group, step, grid, eighth) in enumerate(blocks):
        if b0 > total:
            raise SystemExit(f"拍号段落 bar {b0} 超出了全曲 {total} 小节")
        b1 = blocks[i + 1][0] - 1 if i + 1 < len(blocks) else total
        for b in range(b0, b1 + 1):
            out.append(Bar(b, tick, beats, unit, grid, step, eighth, group))
            tick += grid * step
    # 旧谱里写的 grid 现在是推导出来的。对不上就说明它还藏着别的意思，不能装没看见。
    if "grid" in meta:
        grids = sorted({b.grid for b in out})
        if grids != [int(meta["grid"])]:
            raise SystemExit(
                f"meta.grid={meta['grid']} 和推导出来的格子数 {grids} 不一致——"
                f"grid 现在由 beats × subdivision 推导，删掉它（或改 subdivision）")
    return Meter(out, tempo=int(meta["tempo"]), division=div)


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
    def iv(pc):
        return (pc - root_pc) % 12
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


def onsets(rhythm, grid, where="节奏串"):
    """节奏串的长度必须正好等于这一小节的格子数。

    以前是 (rhythm + "-" * grid)[:grid]：短了补齐、长了截断，一声不响。
    统一网格下只是烦人；每小节网格不同之后，它会变成“后半小节全空”而且不报错。
    """
    if len(rhythm) != grid:
        raise SystemExit(
            f"{where}: 节奏串 {rhythm!r} 有 {len(rhythm)} 格，这一小节要 {grid} 格"
            f"（Diff 用 '-' 补齐或删短）")
    bad = sorted(set(rhythm) - set("xX-."))
    if bad:
        raise SystemExit(f"{where}: 节奏串里有不认识的字符 {bad}（只认 x / - / .）")
    return [i for i, c in enumerate(rhythm) if c in "xX"]


def runs(rhythm, grid, where="节奏串"):
    ons = onsets(rhythm, grid, where)
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


def _int_arg(name, arg, default=None):
    """变换参数：缺省值可有可无，但给了就必须是整数。

    以前写成 int(arg)，于是 `- octave`（漏了数字）会抛 TypeError 加一屏堆栈，
    报错里看不出是哪个算子。P8：失败要指名道姓。
    """
    if arg is None:
        if default is None:
            raise SystemExit(f"{name} 需要一个整数，比如 - {name}: 1")
        return default
    try:
        return int(arg)
    except (TypeError, ValueError):
        raise SystemExit(f"{name} 的参数要是整数，拿到 {arg!r}") from None


def _octave(seq, n):
    n = _int_arg("octave", n)
    return [(a, d, [p + 12 * n for p in ps], v) for (a, d, ps, v) in seq]


def _transpose(seq, n):
    n = _int_arg("transpose", n)
    return [(a, d, [p + n for p in ps], v) for (a, d, ps, v) in seq]


def _retrograde(seq, _):
    if not seq:
        return seq
    end = max(a + d for a, d, _p, _v in seq)
    return sorted(((end - a - d, d, ps, v) for (a, d, ps, v) in seq), key=lambda e: e[0])


def _invert(seq, axis):
    """旋律倒影。axis 缺省 = 第一个音（手算得出来，不用猜调）。"""
    if not seq:
        return seq
    ax = seq[0][2][0] if axis in (None, "first") else _int_arg("invert", axis)
    return [(a, d, [2 * ax - p for p in ps], v) for (a, d, ps, v) in seq]


def _augment(seq, k):
    k = _int_arg("augment", k)
    return [(a * k, d * k, ps, v) for (a, d, ps, v) in seq]


def _diminish(seq, k, step=1):
    k = _int_arg("diminish", k)
    out = []
    for (a, d, ps, v) in seq:
        # 除不尽指的是**格数**除不尽，不是 tick 数——tick 总是能被小整数整除，
        # 光看 tick 会把这个拒绝变成“总能通过”，静默产生网格外的音。
        if (a // step) % k or (d // step) % k:
            raise SystemExit(f"diminish:{k} 在 {a // step}:{d // step} 格上除不尽——"
                             f"网格太粗，这个变换写不出来")
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
    if not isinstance(arg, dict):
        raise SystemExit(f"sequence 的参数要是 {{step: 半音, times: 次数}}，拿到 {arg!r}")
    step = _int_arg("sequence.step", arg.get("step"), default=2)
    times = _int_arg("sequence.times", arg.get("times"), default=3)
    span = max(a + d for a, d, _p, _v in seq)
    out = []
    for i in range(times):
        for (a, d, ps, v) in seq:
            out.append((a + i * span, d, [p + i * step for p in ps], v))
    return out


OPS = {"octave": _octave, "transpose": _transpose, "retrograde": _retrograde,
       "invert": _invert, "augment": _augment, "diminish": _diminish,
       "fragment": _fragment, "sequence": _sequence}


def apply_ops(seq, ops, step=1):
    """从左到右依次作用。顺序有意义：先 fragment 后 retrograde ≠ 反过来。

    step 是源的格宽（tick/格）：序列已经用 tick 表示，只有 diminish 需要它来判断“除不尽”。
    """
    for name, arg in _pairs(ops or []):
        fn = OPS.get(name)
        if fn is None:
            raise SystemExit(f"未知变换 {name!r}（可用: {', '.join(sorted(OPS))}）")
        seq = fn(seq, arg, step) if name == "diminish" else fn(seq, arg)
    return seq


def op_shift(ops):
    """从变换链里抽出纯位移：返回 (总半音, 是否只是位移)。"""
    semis, pure = 0, True
    for name, arg in _pairs(ops or []):
        if name == "octave":
            semis += 12 * _int_arg("octave", arg)
        elif name == "transpose":
            semis += _int_arg("transpose", arg)
        else:
            pure = False
    return semis, pure


def _bar_span(meter, target, end):
    """从 target[0] 起，装下 end 个 tick 需要几个小节。"""
    base = meter[target[0]].start_tick
    for i, b in enumerate(target):
        if meter[b].start_tick + meter[b].ticks >= base + end:
            return i + 1
    return len(target)


def build_material(spec, meter):
    """素材轨：与声部无关的时间线。轨不知道自己由谁演奏。

    每个 timeline 段先展开成一条**扁平序列**（绝对 tick），再过变换链，
    最后重新分配到小节。扁平化是必须的：倒影/扩增跨小节，逐小节做不了。
    用 tick 而不是格数也是必须的：格数只在单一拍号里可比。
    """
    tracks, warns = {}, []
    for tr in spec.get("material") or []:
        reg = tr.get("register", 4)
        ev, lineage = {}, []
        for seg in tr["timeline"]:
            target = parse_bars(seg["bars"], None)
            start = meter[target[0]]
            ops = seg.get("op") or []
            grids = sorted({meter[b].grid for b in target})
            if len(grids) > 1 and ("degrees" in seg or seg.get("repeat")):
                what = "用 repeat 的" if seg.get("repeat") else "写 degrees 的"
                raise SystemExit(
                    f"素材 {tr['id']} {seg['bars']}: 这段跨了不同拍号（格子数 {grids}）——"
                    f"{what}段只能落在同一种拍号里，请拆成两段")

            if "use" in seg:
                src = tracks.get(seg["use"])
                if src is None:
                    raise SystemExit(f"素材 {tr['id']} 引用了尚未定义的轨 {seg['use']}")
                src_bars = parse_bars(seg.get("from", seg["bars"]), None)
                sbase = meter[src_bars[0]].start_tick
                seq = [((meter[b].start_tick - sbase) + st * meter[b].step_ticks,
                        du * meter[b].step_ticks, ps, v)
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
                    span = int(dur) * start.step_ticks
                    if int(deg_s) == 0:            # 0 = 休止：只推进时间
                        step += span
                        continue
                    seq.append((step, span, [scale_degree(int(deg_s) + shift, reg)],
                                seg.get("velocity", 80)))
                    step += span
                if seg.get("repeat"):
                    # 一小节素材，逐小节重复（嗡鸣、固定音型）。
                    # 没有这一条就只能写 32 行一模一样的时间线。
                    seq = [(a + (meter[b].start_tick - start.start_tick), du, ps, v)
                           for b in target for (a, du, ps, v) in seq]
                lineage.append({"from": None, "from_bars": None, "bars": seg["bars"],
                                "ops": ops, "semitones": 0, "pure": False, "tonal": False})
            else:
                raise SystemExit(f"素材 {tr['id']} 的 timeline 段既无 degrees 也无 use: {seg}")

            seq = apply_ops(seq, ops, start.step_ticks)

            # 变换会改变长度：装不下就报，不要默默截断（C14）
            if seq:
                need = _bar_span(meter, target, max(a + d for a, d, _p, _v in seq))
                if need > len(target):
                    raise SystemExit(
                        f"素材 {tr['id']} {seg['bars']}: 变换后需要 {need} 小节，"
                        f"但只给了 {len(target)} 小节")
                if need < len(target):
                    warns.append(f"素材 {tr['id']} {seg['bars']}: 变换只填满 {need}/{len(target)} 小节，"
                                 f"其余为静默")
            base = start.start_tick
            for (a, du, ps, v) in seq:
                if a < 0:
                    raise SystemExit(f"素材 {tr['id']} {seg['bars']}: 变换产生了负位置")
                bar = meter.at_tick(base + a)
                off = base + a - bar.start_tick
                if off % bar.step_ticks or du % bar.step_ticks:
                    raise SystemExit(
                        f"素材 {tr['id']} {seg['bars']}: 落在 bar {bar.index}"
                        f"（{bar.beats}/{bar.unit}，一格 {bar.step_ticks} tick）上的音不在格上"
                        f"（偏移 {off}、时值 {du} tick）")
                ev.setdefault(bar.index, []).append(
                    (off // bar.step_ticks, du // bar.step_ticks, ps, v))

        tracks[tr["id"]] = {"register": reg, "events": ev,
                             "theme": bool(tr.get("theme")), "lineage": lineage}
    return tracks, warns


def expand(spec, insts, meter):
    total = len(spec["harmony"])
    harmony = spec["harmony"]
    bars = {}
    tracks, mat_warns = build_material(spec, meter)

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
                bar = meter[b]
                where = f"{part['name']} bar {b}"
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
                        if int(deg_s) == 0:        # 0 = 休止
                            step += int(dur)
                            continue
                        evs.append((step, int(dur),
                                    [scale_degree(int(deg_s) + shift, part["base_octave"], tr)], vel))
                        step += int(dur)
                    if step > bar.grid:
                        raise SystemExit(
                            f"{where}: degrees 占了 {step} 格，但这一小节只有 {bar.grid} 格"
                            f"（{bar.beats}/{bar.unit}）")
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
                            for st, _du in runs(rhythm, bar.grid, where):
                                evs.append((st, 1, [dmap[pitch]], lvel))
                        elif pitch == "arp":
                            tones = resolve("arp", h, part)
                            for st, du in runs(rhythm, bar.grid, where):
                                evs.append((st, du, [tones[(st // bar.eighth) % len(tones)]], lvel))
                        else:
                            prev = part.get("_prev")
                            ps = resolve(pitch, h, part, prev)
                            part["_prev"] = ps[0]
                            for st, du in runs(rhythm, bar.grid, where):
                                evs.append((st, du, [p + tr for p in ps], lvel))
                bars.setdefault(b, []).extend((part, st, du, ps, v) for (st, du, ps, v) in evs)

    return bars, parts, tracks, mat_warns


# ---------- 音区：推荐 / 人为覆盖 / 自动择位 ----------

def autoplace(bars, parts):
    """place: auto —— 选一个八度，使材料尽量待在**舒适区**里（C1 的“推荐”）。"""
    for part in parts:
        if part.get("place") != "auto" or part.get("drum"):
            continue
        lo_r, hi_r = part["range"]
        lo_c, hi_c = part.get("comfort") or part["range"]
        best = None
        for shift in range(-24, 25, 12):
            cost = 0
            for evs in bars.values():
                for (p, _st, du, ps, _v) in evs:
                    if p is not part:
                        continue
                    for x in ps:
                        y = x + shift
                        cost += du * (100 if not (lo_r <= y <= hi_r) else
                                      (1 if not (lo_c <= y <= hi_c) else 0))
            if best is None or cost < best[0]:
                best = (cost, shift)
        if best is None:                 # 这个座位一个小节都没发声：没什么可择位的
            continue
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
    silent = {}          # 声明在场却一小节没响：报钟、报时这类乐器本来就该这样
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
            if any(e[0]["name"] == name for e in kept):
                continue
            silent.setdefault(name, []).append(b)
        out[b] = kept

    # 连续 4 小节以上"在场但没响"才值得说一句——那通常是漏写，不是休止。
    # 报钟敲一下、静三小节，是音乐，不是错误（C18：描述性测量不进 lint）。
    for name, bs in sorted(silent.items()):
        run = []
        for b in bs + [None]:
            if run and (b is None or b != run[-1] + 1):
                if len(run) >= 4:
                    issues.append(f"bar {run[0]}-{run[-1]}: {name} 声明在场但连续 {len(run)} 小节没有音符")
                run = []
            if b is not None:
                run.append(b)
    return out, sorted(set(issues))


# ---------- 检查 ----------

def check_harmony(spec, bars, parts):
    problems = []
    skip = {p["id"] for p in parts if p.get("_carrier")}
    for b, evs in bars.items():
        h = chord_at(spec["harmony"], b)
        allowed = {pc % 12 for pc in h["chord"]} | {h["bass"] % 12}
        for (part, _st, _du, pitches, _vel) in evs:
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
        for (part, _st, _du, pitches, _vel) in bars[b]:
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
    # 只渲染后半段时不能发出负 tick：负 delta-time 会得到畸形 MIDI，
    # fluidsynth 读它的时候不报错、不返回，就那么挂着（踩过一次）。
    spec = {"meta": {"tempo": 120, "division": 480, "subdivision": 4,
                     "meter": [{"bar": 1, "beats": 4, "unit": 4}], "title": "t"},
            "harmony": [{"name": "C5"}] * 4,
            "color": [{"bars": "1-4", "reverb": [40, 120], "brightness": 90}]}
    evs = color_events(spec, 3, meter_of(spec))
    ok = bool(evs) and min(t for (t, _c, _v) in evs) >= 0 and min(t for (t, _c, _v) in evs) == 0
    bad += not ok
    print(f"  {'✓' if ok else '✗'} 只渲染后半段：CC 不发负 tick，且窗口起点有值")

    # 奇数拍：手算得出来，所以能验。4/4 四小节（1920 tick）→ 7/8 四小节（1680）→
    # 5/4（2400）→ 11/8（2640）。格子数分别是 16 / 28 / 20 / 44。
    odd = {"meta": {"tempo": 92, "division": 480, "subdivision": 4,
                    "meter": [{"bar": 1, "beats": 4, "unit": 4},
                              {"bar": 5, "beats": 7, "unit": 8, "group": [2, 2, 3]},
                              {"bar": 9, "beats": 5, "unit": 4, "group": [3, 2]},
                              {"bar": 13, "beats": 11, "unit": 8, "group": [3, 2, 3, 3]}],
                    "title": "t"},
           "harmony": [{"name": "C5"}] * 16}
    m = meter_of(odd)
    want = [(1, 0, 1920, 16, 120), (5, 7680, 1680, 28, 60),
            (9, 14400, 2400, 20, 120), (13, 24000, 2640, 44, 60)]
    got = [(b.index, b.start_tick, b.ticks, b.grid, b.step_ticks)
           for b in (m[1], m[5], m[9], m[13])]
    ok = got == want
    bad += not ok
    print(f"  {'✓' if ok else '✗'} 拍号 4/4→7/8→5/4→11/8：起点 tick / 小节点数 / 格子数 / 步长")
    if not ok:
        print(f"      want {want}\n      got  {got}")
    # 小节长度不等时，按 tick 反查必须找对小节（7/8 和 5/4 边界最容易差一格）。
    # 4/4 四小节 0..7679，7/8 四小节 7680..14399，5/4 四小节 14400..23999，11/8 24000..
    probes = [(0, 1), (1919, 1), (1920, 2), (5760, 4), (7679, 4),
              (7680, 5), (9359, 5), (9360, 6), (14399, 8),
              (14400, 9), (16799, 9), (16800, 10), (23999, 12),
              (24000, 13), (26639, 13), (26640, 14), (31919, 15), (31920, 16)]
    wrong = [(t, b.index) for t, want_b in probes for b in [m.at_tick(t)] if b.index != want_b]
    bad += len(wrong) > 0
    print(f"  {'✓' if not wrong else '✗'} 按 tick 反查小节（边界差一格会全错）")
    for t, got_b in wrong:
        print(f"      tick {t} 落在 bar {got_b}")

    # 拍号写错必须报错，而不是凑合：这几条都是真会写错的形式。
    rejects = [
        ("group 加不起来", [{"bar": 1, "beats": 7, "unit": 8, "group": [2, 2, 2]}]),
        ("分母不是 2 的幂", [{"bar": 1, "beats": 3, "unit": 3}]),
        ("不从第 1 小节开始", [{"bar": 2, "beats": 4, "unit": 4}]),
        ("段落起点重复", [{"bar": 1, "beats": 4, "unit": 4},
                     {"bar": 1, "beats": 3, "unit": 4}]),
    ]
    for label, segs in rejects:
        try:
            meter_of({"meta": {"tempo": 92, "division": 480, "meter": segs},
                      "harmony": [{"name": "C5"}] * 8})
        except SystemExit:
            print(f"  ✓ 拒绝：{label}")
        else:
            bad += 1
            print(f"  ✗ 没有拒绝：{label}")

    # 节奏串长度不对必须拒绝：以前会静默补齐/截断。
    for label, r, g in (("短了", "x" * 12, 16), ("长了", "x" * 20, 16),
                        ("乱字符", "x" * 15 + "z", 16), ("正好", "x" * 14 + "--", 16)):
        try:
            runs(r, g, "selftest")
        except SystemExit:
            ok = label != "正好"
            bad += not ok
            print(f"  {'✓' if ok else '✗'} 节奏串{label}（{g} 格）")
        else:
            ok = label == "正好"
            bad += not ok
            print(f"  {'✓' if ok else '✗'} 节奏串{label}（{g} 格）没有拒绝")

    # guide 只校验结构，不校验内容。结构错了就是错了：锚点越界、不递增、空句子。
    gspec = {"meta": {"tempo": 92, "division": 480, "meter": [4, 4]},
             "harmony": [{"name": "C5"}] * 8}
    grejects = [
        ("锚点越界", [{"bar": 9, "text": "x"}]),
        ("不递增", [{"bar": 5, "text": "x"}, {"bar": 5, "text": "y"}]),
        ("空句子", [{"bar": 3, "text": "   "}]),
    ]
    for label, beats in grejects:
        try:
            guide_of({**gspec, "guide": {"beats": beats}})
        except SystemExit:
            print(f"  ✓ 拒绝：guide {label}")
        else:
            bad += 1
            print(f"  ✗ 没有拒绝：guide {label}")
    gok = {**gspec, "guide": {"summary": " s ", "intension": [" i "],
                               "beats": [{"bar": 1, "text": " a "}, {"bar": 5, "text": " b "}]}}
    ok = guide_of(gok) == ("s", ["i"], [(1, "a"), (5, "b")])
    bad += not ok
    print(f"  {'✓' if ok else '✗'} guide 正常读取（首尾空白去掉，顺序保留）")

    total = len(cases) + 2 + 4 + len(rejects) + 4 + len(grejects) + 1
    print(f"\n  {total - bad}/{total} 通过")
    raise SystemExit(1 if bad else 0)


# ---------- 演奏可行性（把 override 变成可量化的东西）----------

def report(bars, parts, meter):
    """每条都是可量的：舒适区时长占比、最长连续、大跳、最高音。"""
    rows = []
    for part in parts:
        evs = [(b, st, du, ps) for b in sorted(bars)
               for (p, st, du, ps, v) in bars[b] if p is part]
        if not evs or part.get("drum"):
            continue
        # tick 而不是格数：拍号一变，一格的长度就变了，累加格数会得到假的拍数。
        iv = sorted((meter[b].start_tick + st * meter[b].step_ticks,
                     meter[b].start_tick + (st + du) * meter[b].step_ticks, ps)
                    for b, st, du, ps in evs)
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

        def beats(t):
            """tick 长度 -> 拍数（按它开头所在小节算，拍号内一致）。"""
            bar = meter.at_tick(t)
            return t / (bar.ticks / bar.beats)

        longest = max((beats(z - a) for a, z, _ in runs), default=0)
        total = sum(beats(z - a) for a, z, _ in runs)

        lo_r, hi_r = part["range"]
        lo_c, hi_c = part.get("comfort") or (lo_r, hi_r)
        out_c = sum(beats(z - a) for a, z, ps in runs
                    if any(not (lo_c <= x <= hi_c) for x in ps))
        out_r = sum(beats(z - a) for a, z, ps in runs
                    if any(not (lo_r <= x <= hi_r) for x in ps))

        leaps, top = 0, None
        if part.get("monophonic"):
            seq = [ps[0] for _b, _s, _d, ps in evs]
            leaps = sum(1 for x, y in itertools.pairwise(seq) if abs(y - x) > 7)
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
        # 0.5 拍别显示成 0：机器音型每两格就停一次，最长连续真的只有半拍。
        def _b(v):
            return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"
        cap = f"{_b(r['longest'])}/{_b(r['cap'])}" if r["cap"] else f"{_b(r['longest'])}/—"
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


def color_events(spec, first, meter):
    """CC 自动化：混响 91 / 合唱 93 / 亮度 74。

    值可以是常数，也可以是 [起, 止] —— 后者在区间内按拍线性插值，
    于是“混响慢慢推上去”是一条曲线，不是一级一级的阶梯（§8 D5）。
    """
    total = len(spec["harmony"])
    t0 = meter[first].start_tick
    out = []
    for seg in spec.get("color") or []:
        bs = parse_bars(seg["bars"], total)
        # 采样点：每四格一个。序号用来算插值比例，tick 用来定位——
        # 拍号一变两者就不再成比例，所以必须分开记。
        marks, k = [], 0
        for bar in bs:
            b = meter[bar]
            for j in range(0, b.grid, 4):
                marks.append((b.start_tick + j * b.step_ticks, k + j))
            k += b.grid
        n = k
        for key, cc in (("reverb", 91), ("chorus", 93), ("brightness", 74)):
            if key not in seg:
                continue
            v = seg[key]
            va, vb = (v[0], v[1]) if isinstance(v, (list, tuple)) else (v, v)
            pre = None
            for tick, idx in marks:
                frac = idx / max(1, n - 1)
                val = max(0, min(127, round(va + (vb - va) * frac)))
                t = tick - t0
                if t < 0:
                    # 渲染窗口之前：记住最后一档，压到 0 号 tick 上，
                    # 否则只渲染后半段时混响会从 0 突然跳起来。
                    pre = val
                    continue
                if pre is not None:
                    out.append((0, cc, pre))
                    pre = None
                out.append((t, cc, val))
    return out


def build_midi(spec, bars, parts, meter, melody=True, first=1):
    """first = 输出时间轴上的第 1 小节对应原谱的哪一小节。

    内环只想要第 17-20 小节时，传 first=16（多留一小节 preroll，
    让从前一小节拖过来的音保留起音），然后按时间切音频。
    """
    meta = spec["meta"]
    div = meta["division"]
    t0 = meter[first].start_tick

    conductor = [(0, 0, b"\xff\x51\x03" + int(60_000_000 / meta["tempo"]).to_bytes(3, "big")),
                 (0, 2, meta_text(meta["title"]))]
    # 拍号可以变：每个变拍的小节都发一条（同拍号不重复）。
    last = None
    for b in meter.all():
        sig = (b.beats, b.unit)
        if sig == last:
            continue
        last = sig
        conductor.append((max(0, b.start_tick - t0), 1,
                          b"\xff\x58\x04" + bytes([b.beats, b.unit.bit_length() - 1, 24, 8])))
    tracks = [track_chunk(conductor)]
    lvl = dynamics_map(spec)
    colors = color_events(spec, first, meter)

    for part in parts:
        if not melody and part.get("_theme"):
            tracks.append(track_chunk([(0, 0, meta_text("(omitted)"))]))
            continue
        ch = part["channel"]
        evs = [(0, 0, meta_text(part["name"])), (0, 1, bytes([0xC0 | ch, part["gm_program"]]))]
        for (t, cc, val) in colors:
            evs.append((t, 1, bytes([0xB0 | ch, cc, val])))
        # 只渲染后半段时，窗口开始前最后一条力度要压到 0 号 tick，
        # 不然前几小节会用音源的默认音量（100），听起来像突然一记重音。
        prior = [b for b in lvl if b < first]
        if prior:
            evs.append((0, 1, bytes([0xB0 | ch, 7, int(lvl[max(prior)] * 127)])))
        for bar in sorted(bars):
            b = meter[bar]
            if bar in lvl and bar >= first:
                evs.append((b.start_tick - t0, 1, bytes([0xB0 | ch, 7, int(lvl[bar] * 127)])))
            for (p, st, du, pitches, vel) in bars[bar]:
                if p is not part:
                    continue
                s = b.start_tick - t0 + st * b.step_ticks
                e = s + du * b.step_ticks
                for i, pitch in enumerate(pitches):
                    evs.append((s, 2 + i, bytes([0x90 | ch, pitch & 0x7F, min(127, vel)])))
                    evs.append((e, 2 + i, bytes([0x80 | ch, pitch & 0x7F, 0])))
        tracks.append(track_chunk(evs))

    header = (b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big")
              + len(tracks).to_bytes(2, "big") + div.to_bytes(2, "big"))
    return header + b"".join(tracks)


# ---------- 输出 ----------

def _sounding(bars, meter, b):
    """第 b 小节响着的声部（含从前一小节拖过来的音）。

    一个音可以比它所在的小节还长（跨小节连线）。这种音在 MIDI 里是对的，
    但如果只看 `bars[b]` 就会在下一小节“消失”——听起来有、图上没有。
    """
    names = {p["name"] for (p, *_r) in bars.get(b, [])}
    if b > 1:
        prev = meter[b - 1]
        for (p, st, du, *_r) in bars.get(b - 1, []):
            if st + du > prev.grid:
                names.add(p["name"])
    return names


def roll(spec, bars, parts, meter):
    total = len(spec["harmony"])
    print()
    for start in range(1, total + 1, 4):
        block = list(range(start, min(start + 4, total + 1)))
        head = "".join(f"|{chord_at(spec['harmony'], b)['name']:<16}" for b in block) + "|"
        print(f"  bar {block[0]:>2}-{block[-1]:<2}  {head}")
        # 分组线：group [2,2,3] 的重音落在哪几格。只在有 group 的地方画。
        if any(meter[b].group for b in block):
            cells = []
            for b in block:
                bar = meter[b]
                line = [" "] * bar.grid
                if bar.group:
                    k = 0
                    for g in bar.group:
                        line[k] = "'"
                        k += g * (bar.grid // bar.beats)
                cells.append("".join(line))
            print(f"  {'group':<10}" + " ".join(cells))
        for part in parts:
            cells = []
            for b in block:
                grid = meter[b].grid
                line = ["."] * grid
                # 先画从前一小节越过来的那段（起音不在这一小节）
                if b > 1:
                    prev = meter[b - 1]
                    for (p, st, du, _pitches, _vel) in bars.get(b - 1, []):
                        if p is not part or st + du <= prev.grid:
                            continue
                        over = (st + du - prev.grid) * prev.step_ticks // meter[b].step_ticks
                        for k in range(min(grid, over)):
                            line[k] = "|"
                for (p, st, du, _pitches, _vel) in bars.get(b, []):
                    if p is not part:
                        continue
                    for k in range(st, min(grid, st + du)):
                        line[k] = "#" if k == st else "|"
                cells.append("".join(line))
            print(f"  {part['name'][:9]:<10}" + " ".join(cells))
        print()


def guide_of(spec):
    """纲要：一句话的整首说明 + 若干条锚在小节上的旁白。

    这里只校验**结构**（锚点存在、递增、非空），不校验内容。
    用 NLP 去判断“变空”和 RMS 方向是否一致，是替自己编理解——
    矩阵是权威，句子是注释（C18 第三类：描述性，永不进 lint）。

    写作规则（可检验）：**一句旁白，只有在能说出“否则你会听成什么”时才值得写。**
    说不出那个 X 的，要么归到 `summary`，要么删掉（“这里钢琴进来了”不过——矩阵已经说了）。

    `intension` 是内涵级句子：**对两份完全不同的外延都成立**。
    它们不是摘要，是用来回答“新编曲完全不像旧编曲时，怎么判断它还服务同一个意图”。
    """
    total = len(spec["harmony"])
    g = spec.get("guide") or {}
    summary = (g.get("summary") or "").strip()
    intension = [str(x).strip() for x in (g.get("intension") or [])]
    beats, last = [], 0
    for item in g.get("beats") or []:
        if "bar" not in item or "text" not in item:
            raise SystemExit(f"guide.beats 每一条都要有 bar 和 text: {item!r}")
        b = int(item["bar"])
        text = str(item["text"]).strip()
        if not 1 <= b <= total:
            raise SystemExit(f"guide 锚在 bar {b}，但全曲只有 {total} 小节")
        if b <= last:
            raise SystemExit(f"guide 的小节必须递增：bar {b} 排在 bar {last} 之后")
        if not text:
            raise SystemExit(f"guide bar {b} 的 text 是空的")
        beats.append((b, text))
        last = b
    return summary, intension, beats


def guide(spec, meter, first=1, last=None):
    """打印纲要。first/last 给定时只打这一段的旁白（audition 用）。"""
    summary, intension, beats = guide_of(spec)
    if not summary and not intension and not beats:
        return
    whole = first == 1 and last is None
    if whole and summary:
        print(f"\n  纲要  {summary}")
    for b, text in beats:
        if last is not None and not first <= b <= last:
            continue
        s = meter.secs(b)
        print(f"    {int(s // 60)}:{int(s % 60):02d}  bar {b:<3} {text}")
    if whole and intension:
        print("\n  内涵（对两份不同的编曲都成立，所以它不是编曲细节）:")
        for x in intension:
            print(f"    · {x}")


def presence(spec, bars, parts, meter):
    print("  bar  meter  chord        n")
    for b in range(1, len(spec["harmony"]) + 1):
        bar = meter[b]
        names = sorted(_sounding(bars, meter, b))
        print(f"  {b:>3}  {bar.beats}/{bar.unit}  {chord_at(spec['harmony'], b)['name']:<12} "
              f"{len(names)}  {', '.join(names)}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "farewell-v3.yaml"
    if path == "--selftest":
        selftest()
    spec = load_yaml(path, "spec")
    ens, insts = load_ensemble(spec, path)
    meter = meter_of(spec)

    bars, parts, tracks, mat_warns = expand(spec, insts, meter)
    bars = autoplace(bars, parts)
    bars, pis = apply_presence(spec, bars)

    out = path.rsplit(".", 1)[0]
    for name, with_mel in ((out + ".mid", True), (out + "-nomelody.mid", False)):
        m = build_midi(spec, bars, parts, meter, melody=with_mel)
        try:
            with open(name, "wb") as f:
                f.write(m)
        except OSError as e:
            raise SystemExit(f"写不出 {name}\n{e}") from e
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

    report(bars, parts, meter)
    guide(spec, meter)
    roll(spec, bars, parts, meter)
    presence(spec, bars, parts, meter)


if __name__ == "__main__":
    main()
