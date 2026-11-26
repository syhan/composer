#!/usr/bin/env python3
"""把外部的谱文件变成我们的 spec。

    ingest.py ref.musicxml               # 只读，报告"有什么、会丢什么"
    ingest.py ref.musicxml --json        # 同一份，机器读
    ingest.py ref.musicxml --dump tune   # 把某个声部摊成 tick 表（调试用）
    ingest.py ref.musicxml --as-spec out.yaml   # 生成 spec + 编制 + 乐器

名字叫 ingest 而不是 import_musicxml：它已经会读 MusicXML，将来还要读
Guitar Pro 的 gp 和别的格式。**格式差异只在一个"解析器"里**——文件先被变成
我们自己那份中间表示（`parse()` 返回的 dict：小节、声部、事件），之后的
report / dump / --as-spec 三步与格式无关。想加 gp，写一个 `parse_gp()`
产出同样的 dict 就行，后面三步不用动。

为什么第一步是只读的报告：任何格式的坑都不在"读"，在**读错了不会报错**。
MusicXML 里漏了 `<transpose>`，整条声部低一个大二度；多声部的 `<backup>`
没处理，第二声部整体后移；反复没展开，后面每一小节都错位。这些都不会抛异常，
只会安静地出一份"有点怪的"编曲。

所以第一步只回答一个问题：**这个文件里到底有什么，哪些我们会丢。**
一行 spec 都不生成，就不可能出错。

刻意用 stdlib + defusedxml，不引 music21：解析只读结构、不解释语义，
新格式进来也一样。等真要生成 spec 了，"理解得对不对"才开始重要。
"""

import json
import os
import subprocess
import sys
from math import lcm

# 用 defusedxml 而不是裸 xml.etree：后者会展开实体，解析不受信文件有 XXE 风险。
try:
    from defusedxml import ElementTree as ET
except ImportError:
    raise SystemExit("缺 defusedxml（pip install defusedxml）：解析 XML 不用裸的 xml.etree")

# 我们内部一律用 480 tick 一个四分音符（division: 480）。
# 但文件自己的 divisions 可能不是 480，而且**每个声部可以不同**。
HOME_DIV = 480

# 仓库根：本文件在 spikes/01/ 下
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def tag_of(el):
    """去掉命名空间。有些导出器会写 xmlns，有些不会。"""
    return el.tag.rsplit("}", 1)[-1]


def kids(el, name):
    return [c for c in el if tag_of(c) == name]


def kid(el, name):
    for c in el:
        if tag_of(c) == name:
            return c
    return None


def text(el, name, default=None):
    c = kid(el, name)
    if c is None or c.text is None:
        return default
    return c.text.strip()


def num(el, name, default=None):
    v = text(el, name)
    if v is None:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


STEP = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def midi_of(pitch):
    return 12 * (int(text(pitch, "octave", 4)) + 1) + STEP[text(pitch, "step", "C")] + int(
        text(pitch, "alter", 0)
    )


class Loss:
    """被丢掉或被改动的东西。每条都要能回答"那我该怎么办"。"""

    def __init__(self):
        self.items = {}

    def add(self, kind, where, note):
        e = self.items.setdefault(kind, {"n": 0, "where": [], "note": note})
        e["n"] += 1
        if len(e["where"]) < 6 and where not in e["where"]:
            e["where"].append(where)


def parse_measure(m, ctx, loss, div_note):
    """一小节 -> 若干 (声部, staff, voice) 的事件。

    时间轴用 `<backup>`/`<forward>` 显式挪动，不是"按出现顺序累加"——
    这是多声部谱表最容易被读错的地方：第二声部的音会被当第一声部后面的音。
    """
    pos, out = 0, []
    cursor_voice, cursor_staff = "1", 1
    for el in m:
        t = tag_of(el)
        if t == "backup":
            pos -= num(el, "duration", 0)
        elif t == "forward":
            pos += num(el, "duration", 0)
        elif t == "attributes":
            div = num(el, "divisions")
            if div:
                ctx["divisions"] = div
                div_note.add(div)
            tr = kid(el, "transpose")
            if tr is not None:
                ctx["transpose"] = num(tr, "chromatic", 0) or 0
            time = kid(el, "time")
            if time is not None:
                beats, unit = num(time, "beats"), num(time, "beat-type")
                if beats and unit:
                    ctx["meter"].append((ctx["bar"], beats, unit))
        elif t == "sound" or t == "direction":
            tempo = num(el, "tempo") if t == "sound" else None
            if tempo is None:
                met = kid(el, "metronome")
                if met is not None:
                    tempo = num(met, "per-minute")
            if tempo:
                ctx["tempos"].append((ctx["bar"], tempo))
        elif t == "harmony":
            root = kid(el, "root")
            kind = text(el, "kind", "")
            ctx["harmonies"].append(
                (
                    ctx["bar"],
                    text(root, "root-step", "C"),
                    num(root, "alter", 0),
                    kind,
                )
            )
        elif t == "barline":
            rep = kid(el, "repeat")
            if rep is not None:
                loss.add(
                    "反复/volta",
                    f"bar {ctx['bar']}",
                    "导入时展开成线性（我们的循环在播放器里，不在谱里），并报告展开后长度",
                )
            if kid(el, "ending") is not None:
                loss.add("反复/volta", f"bar {ctx['bar']}", "volta 会被展开，段标记不保留")
        elif t == "note":
            voice = text(el, "voice", cursor_voice)
            staff = num(el, "staff", cursor_staff) or 1
            cursor_voice, cursor_staff = voice, staff
            dur = num(el, "duration", 0)
            grace = kid(el, "grace") is not None
            is_chord = kid(el, "chord") is not None
            tm = kid(el, "time-modification")
            if tm is not None:
                ctx["tuplets"].append(
                    (ctx["bar"], num(tm, "actual-notes", 1), num(tm, "normal-notes", 1))
                )
            if grace:
                loss.add("装饰音", f"bar {ctx['bar']}", "会丢。要保留就得给它位置和时值，我们没有这个表示")
            pitch = kid(el, "pitch")
            if pitch is not None:
                out.append(
                    {
                        "part": ctx["part"],
                        "staff": staff,
                        "voice": voice,
                        "bar": ctx["bar"],
                        "tick": None,  # 下面按 divisions 归一化后再填
                        "pos": pos,
                        "dur": dur,
                        "midi": midi_of(pitch) + ctx["transpose"],
                        "chord": is_chord,
                        "grace": grace,
                    }
                )
            tie = kid(el, "tie")
            if tie is not None and tie.get("type") == "start":
                loss.add(
                    "连音线",
                    f"bar {ctx['bar']}",
                    "音高和时值都保留（我们本来就支持时值超过一小节），但'同一个音被写成两半'这件事不保留",
                )
            if not is_chord and not grace:
                pos += dur
    return out


def parse(path):
    try:
        root = ET.parse(path).getroot()
    except OSError as e:
        raise SystemExit(f"读不了 {path}: {e}") from e
    except ET.ParseError as e:
        raise SystemExit(f"{path} 不是合法的 XML: {e}") from e

    kind = tag_of(root)
    if kind != "score-partwise":
        raise SystemExit(
            f"只认 score-partwise（按小节组织的总谱），这个文件是 {kind}。"
            "timewise 极罕见，遇到再单独处理；MIDI 请直接转 MusicXML 而不是反过来。"
        )

    names = {}
    plist = kid(root, "part-list")
    if plist is not None:
        for sp in kids(plist, "score-part"):
            midi_prog = None
            mi = kid(sp, "midi-instrument")
            if mi is not None:
                midi_prog = num(mi, "midi-program")
            names[sp.get("id")] = (text(sp, "part-name", sp.get("id")), midi_prog)
    else:
        raise SystemExit("缺 part-list：没有声部名字，就没法给座位起名")

    loss = Loss()
    divs, parts, tempos, meters = set(), {}, [], []
    tuplets, harms = [], []
    for part_el in kids(root, "part"):
        pid = part_el.get("id")
        nm, midi_prog = names.get(pid, (pid, None))
        ctx = {
            "part": pid,
            "bar": 0,
            "divisions": None,
            "transpose": 0,
            "meter": [],
            "tempos": [],
            "harmonies": [],
            "tuplets": [],
        }
        evs = []
        for m in kids(part_el, "measure"):
            raw = (m.get("number") or "0").split(".")[0]
            try:
                ctx["bar"] = int(raw)
            except ValueError:
                raise SystemExit(f"第 {raw!r} 小节的小节号不是数字，没法定位") from None
            evs += parse_measure(m, ctx, loss, divs)
        parts[pid] = {
            "name": nm,
            "gm_program": midi_prog,
            "events": [e for e in evs if not e["grace"]],
            "grace": sum(1 for e in evs if e["grace"]),
            "transpose": ctx["transpose"],
            "divisions": ctx["divisions"],
            "bars": ctx["bar"],  # 最后一个小节的编号：空小节也算
        }
        tempos += ctx["tempos"]
        meters += ctx["meter"]
        tuplets += ctx["tuplets"]
        harms += ctx["harmonies"]
        if ctx["divisions"]:
            divs.add(ctx["divisions"])
        if ctx["transpose"]:
            loss.add(
                "移调乐器",
                f"{parts[pid]['name']}",
                f"写法比实音低/高 {ctx['transpose']} 半音，导入时按实音（is 已加到音高上）",
            )

    return {
        "path": path,
        "kind": kind,
        "parts": parts,
        "loss": loss.items,
        "divisions": sorted(divs),
        "tempos": tempos,
        "meters": meters,
        "tuplets": tuplets,
        "harmony": harms,
        "bars": max((p["bars"] for p in parts.values()), default=0),
    }


def norm_division(divs):
    """我们的 meta.division 是全局一个数；文件里每个声部可以不同。

    取 lcm：只有这样才能让所有声部同时落在整 tick 上。
    """
    if not divs:
        return HOME_DIV
    d = divs[0]
    for x in divs[1:]:
        d = lcm(d, x)
    return d


def loss_kind(kind):
    """三类：能无损带过来、会丢一部分、会改变语义。"""
    if kind in ("三连音",):
        return "ok"
    if kind in ("反复/volta", "多声部同谱表", "移调乐器"):
        return "语义"
    return "丢"


def report(sc):
    divs = sc["divisions"]
    sdiv = norm_division(divs)
    print(f"\n  {sc['path']}")
    print(f"    {sc['kind']}   {len(sc['parts'])} 个声部   {sc['bars']} 小节")
    print(f"    divisions {divs or '未声明'}  ->  建议 meta.division: {sdiv}")
    if divs and any(sdiv % d for d in divs):
        print("    ! 有除不尽的声部，位置会落到非整 tick 上")
    nh = len(sc["harmony"])
    if nh:
        print(f"    和声标记 {nh} 处（导入会按它填 harmony；没有标记的谱只能人工补）")
    else:
        print("    和声标记 0 处（**没有和声信息**——导入会留空，补之前不要加 pattern 声部）")
    if sc["meters"]:
        ms = sorted({(b, u) for _bar, b, u in sc["meters"]})
        print(f"    拍号 {', '.join(f'{b}/{u}' for b, u in ms)}")
    if sc["tempos"]:
        t = sorted({v for _b, v in sc["tempos"]})
        print(f"    速度 {', '.join(str(x) for x in t)}")

    print("\n  ── 声部")
    print(f"    {'名字':<24}{'小节':>5}{'音符':>6}{'音域':>12}{'移调':>5}")
    for p in sc["parts"].values():
        evs = p["events"]
        rng = "—"
        if evs:
            lo, hi = min(e["midi"] for e in evs), max(e["midi"] for e in evs)
            rng = f"{NAME[lo % 12]}{lo // 12 - 1}–{NAME[hi % 12]}{hi // 12 - 1}"
        tr = f"{p['transpose']:+d}" if p["transpose"] else "—"
        print(f"    {p['name'][:23]:<24}{p['bars']:>5}{len(evs):>6}{rng:>12}{tr:>5}")

    tp = sc["tuplets"]
    if tp:
        kinds = sorted({f"{a}:{b}" for _bar, a, b in tp})
        loss_set = sc["loss"]
        loss_set.setdefault(
            "三连音",
            {"n": len(tp), "where": list(dict.fromkeys(f"bar {b}" for b, _a, _n in tp)), "note": ""},
        )
        loss_set["三连音"]["note"] = (
            f"{'/'.join(kinds)}，共 {len(tp)} 处。**能无损保留**："
            "我们的 tick 是精确的（480 能整除 3 和 5），导入走绝对 tick，不经过格子"
        )

    print("\n  ── 会丢 / 会变的东西")
    if not sc["loss"]:
        print("    （没有发现）")
    for kind, e in sc["loss"].items():
        mark = {"ok": "·", "语义": "!", "丢": "-"}[loss_kind(kind)]
        where = ", ".join(e["where"])
        print(f"    {mark} {kind:<10}{e['n']:>4} 处   {where}")
        print(f"      {e['note']}")
    print(
        "\n    · 能无损带过来   ! 会改变语义（必须知道）   - 会丢（知道就行）"
        "\n    以上全是**报告**：这个命令不生成 spec，也不改任何文件。\n"
    )


NAME = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def dump(sc, want):
    """把某个声部摊成 tick 表。调试用：核对"读进来的到底是什么"。"""
    division = norm_division(sc["divisions"])
    hit = [p for p in sc["parts"].values() if want.lower() in p["name"].lower()]
    if not hit:
        raise SystemExit(f"没有名字含 {want!r} 的声部：{[p['name'] for p in sc['parts'].values()]}")
    p = hit[0]
    div = p["divisions"] or division
    print(f"\n  {p['name']}   divisions {div} -> tick {division}")
    for e in p["events"]:
        tick = e["pos"] * division // div
        dur = e["dur"] * division // div
        print(
            f"    bar {e['bar']:>3}  tick {tick:>6}  dur {dur:>5}  "
            f"{NAME[e['midi'] % 12]}{e['midi'] // 12 - 1:<3} staff {e['staff']} voice {e['voice']}"
        )
    print()



# ---------- 第二步：生成 spec ----------

# MusicXML 的 kind -> 我们的和弦音程 + 名字后缀
KINDS = {
    "major": ("", [0, 4, 7]),
    "minor": ("m", [0, 3, 7]),
    "diminished": ("dim", [0, 3, 6]),
    "augmented": ("aug", [0, 4, 8]),
    "dominant": ("7", [0, 4, 7, 10]),
    "major-seventh": ("maj7", [0, 4, 7, 11]),
    "minor-seventh": ("m7", [0, 3, 7, 10]),
    "diminished-seventh": ("dim7", [0, 3, 6, 9]),
    "major-sixth": ("6", [0, 4, 7, 9]),
    "minor-sixth": ("m6", [0, 3, 7, 9]),
    "suspended": ("sus", [0, 5, 7]),
    "suspended-second": ("sus2", [0, 2, 7]),
    "suspended-fourth": ("sus4", [0, 5, 7]),
    "power": ("5", [0, 7]),
    "fifth": ("5", [0, 7]),
}

PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def slugify(name):
    keep = "".join(c.lower() if (c.isascii() and c.isalnum()) else "-" for c in name)
    parts = [x for x in keep.split("-") if x]
    return "-".join(parts) or "part"


def chord_of(step, alter, kind):
    root = (STEP.get(step, 0) + alter) % 12
    suffix, ivs = KINDS.get(kind or "major", ("", [0, 4, 7]))
    if kind not in KINDS:
        suffix, ivs = ("?" + (kind or ""), [0, 4, 7])
    return f"{PITCH_NAMES[root]}{suffix}", [root] + [root + i for i in ivs[1:]], root


def split_seats(sc):
    """一个 MusicXML 声部可能藏着多个声部（多谱表 / 多 voice）。

    一个 (part, staff, voice) 一个座位——不是"一个 part 一个座位"。
    `<backup>` 的意义就在这里：它们是并行的时间线，不是先后。
    """
    seats = []
    for p in sc["parts"].values():
        buckets = {}
        for e in p["events"]:
            buckets.setdefault((e["staff"], e["voice"]), []).append(e)
        multi = len(buckets) > 1
        for (staff, voice), evs in sorted(buckets.items()):
            slug = slugify(p["name"]) + (f"-s{staff}v{voice}" if multi else "")
            seats.append(
                {
                    "slug": slug,
                    "name": p["name"] + (f" 谱表{staff} 声部{voice}" if multi else ""),
                    "staff": staff,
                    "voice": voice,
                    "events": evs,
                    "gm_program": p.get("gm_program"),
                    "transpose": p.get("transpose") or 0,
                }
            )
    # 同名 slug 加后缀，不要静默覆盖（座位 id 重复曾经把通道号全推错）
    seen = {}
    for seat in seats:
        n = seen.get(seat["slug"], 0)
        seen[seat["slug"]] = n + 1
        if n:
            seat["slug"] = f"{seat['slug']}-{n + 1}"
    return seats


def bar_ticks(meter_segs, division, bars):
    out = {}
    for i, (bar, beats, unit) in enumerate(meter_segs):
        end = meter_segs[i + 1][0] if i + 1 < len(meter_segs) else bars + 1
        ticks = division * 4 * beats // unit
        for b in range(bar, min(end, bars + 1)):
            out[b] = (ticks, beats, unit)
    return out


def as_spec(sc, out_path, root=None):
    root = root or ROOT
    division = norm_division(sc["divisions"])

    # 拍号：补齐"没写就是沿用上一段"，并要求从第 1 小节开始
    # 每个声部都会报一遍拍号，所以要按小节号归并再排序——
    # 直接按出现顺序追加会得到 [(1,…), (2,…), (1,…)]，然后报"段落顺序不对"。
    first_at = {}
    for bar, beats, unit in sc["meters"]:
        first_at.setdefault(bar, (beats, unit))
    segs = [(b, *first_at[b]) for b in sorted(first_at)]
    if not segs:
        raise SystemExit("文件里没有拍号。没有拍号就没有小节长度，导入无从下手")
    if segs[0][0] != 1:
        raise SystemExit(f"第一个拍号出现在 bar {segs[0][0]}，不是第 1 小节")
    bars = sc["bars"]
    if not bars:
        raise SystemExit("文件里没有音符")

    sub = 4
    while sub > 1 and division % sub:
        sub //= 2

    tempos = sorted({v for _b, v in sc["tempos"]})
    tempo = tempos[0] if tempos else 92

    # 和声：按小节沿用上一条标记；一条都没有就留空（并说清楚）
    harms = sorted(sc["harmony"])
    if harms:
        hmap = {}
        for bar, step, alter, kind in harms:
            hmap[bar] = chord_of(step, alter, kind)
        last = None
        harmony = []
        for b in range(1, bars + 1):
            if b in hmap:
                last = hmap[b]
            if last is None:
                raise SystemExit(f"bar 1 到 bar {b} 之间没有任何和声标记，第一小节必须有")
            # 每条都是新 dict：同一个和声重复出现时不要写成 YAML 锚点（能读，但很难看）
            harmony.append({"bar": b, "name": last[0], "chord": list(last[1]), "bass": last[2]})
    else:
        harmony = [{"bar": b, "name": "?", "chord": [], "bass": 0} for b in range(1, bars + 1)]

    seat_list = split_seats(sc)
    bticks = bar_ticks(segs, division, bars)

    insts, seats, material, parts = {}, [], [], []
    for seat in seat_list:
        slug = seat["slug"]
        prog = seat["gm_program"]
        lo = min(e["midi"] for e in seat["events"])
        hi = max(e["midi"] for e in seat["events"])
        insts[slug] = {
            "id": slug,
            "name": seat["name"],
            "family": "imported",
            "gm_program": prog if prog else 0,
            "polyphonic": True,
            "staves": 1,
            # range 是**从这份谱观察到的**，不是乐器事实。两者混在一起就是假事实。
            "range": [max(0, lo - 2), min(127, hi + 2)],
            "comfort": [max(0, lo - 2), min(127, hi + 2)],
            "stamina": {"max_continuous_beats": 240, "rest_beats": 1},
            "roles": [],
            "observed_range": [lo, hi],
            "note": "range 是从这份谱观察到的音域，不是这件乐器的绝对可演奏范围",
        }
        # 同一个 (bar, tick, dur) 上的音并成一个和弦事件
        merged = {}
        for e in seat["events"]:
            key = (e["bar"], e["pos"], e["dur"])
            merged.setdefault(key, []).append(e["midi"])
        notes = []
        for (bar, pos, dur), pitches in sorted(merged.items()):
            bt = bticks.get(bar, (None, None, None))[0]
            if bt is None:
                raise SystemExit(f"{seat['name']}: bar {bar} 没有拍号，算不出小节长度")
            if pos >= bt:
                raise SystemExit(
                    f"{seat['name']}: bar {bar} 里有音的位置 {pos} 超出了小节长度 {bt} tick"
                    f"（多半是 <backup>/<forward> 读错了，这正是不能静默跳过的地方）"
                )
            notes.append(
                {
                    "tick": sum(bticks[b][0] for b in range(1, bar)) + pos * division // (
                        sc["parts"][seat["events"][0]["part"]]["divisions"] or division
                    ),
                    "dur": dur * division // (
                        sc["parts"][seat["events"][0]["part"]]["divisions"] or division
                    ),
                    "pitch": pitches,
                }
            )
        material.append({"id": slug, "timeline": [{"bars": f"1-{bars}", "notes": notes}]})
        parts.append(
            {
                "instrument": slug,
                "origin": {"file": sc["path"], "part": seat["name"]},
                "carries": [{"bars": f"1-{bars}", "track": slug}],
            }
        )
        seats.append({"instrument": slug, "name": seat["name"]})

    spec = {
        "ensemble": None,  # 下面填
        "meta": {
            "title": f"{sc['path'].rsplit('/', 1)[-1]} 导入",
            "version": "imported",
            "tempo": tempo,
            "division": division,
            "subdivision": sub,
            "bars": bars,
            "meter": [{"bar": b, "beats": x, "unit": yy} for b, x, yy in segs],
        },
        "harmony": harmony,
        "material": material,
        "parts": parts,
    }
    return spec, insts, seats


def safe_name(name):
    """文件名/键名不能带路径分隔符——否则 `-o` 里的 `../x` 或声部名里的 `/` 会写到别处。"""
    bad = set(name) & set("/\\:")
    if bad:
        raise SystemExit(f"名字里有不能当文件名的字符 {sorted(bad)}: {name!r}")
    if name in (".", "..") or not name:
        raise SystemExit(f"名字无效: {name!r}")
    return name


def _yaml():
    """报告路径不需要 PyYAML，只有生成 spec 才要。缺了就报清楚，别抛 ImportError。"""
    try:
        import yaml
    except ImportError:
        raise SystemExit("缺 PyYAML（pip install pyyaml）：生成 spec 需要它，报告不需要") from None
    return yaml


def _dump_yaml(path, data):
    """path 来自 safe_name 校验过的 slug/stem，不是外部输入。"""
    y = _yaml()
    try:
        with open(path, "w") as f:
            y.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    except OSError as e:
        raise SystemExit(f"写不了 {path}: {e}") from e
    except y.YAMLError as e:
        raise SystemExit(f"{path} 的 YAML 写不出来: {e}") from e


def write_import(sc, out_path, root=None):
    root = root or ROOT
    y = _yaml()
    spec, insts, seats = as_spec(sc, out_path, root)
    stem = safe_name(os.path.splitext(os.path.basename(out_path))[0])
    ens_path = os.path.join(root, "ensembles", f"{stem}.yaml")
    # 谱在仓库里就写相对路径；写在外面的（/tmp 之类）用绝对路径——
    # 相对路径会绕出仓库，看起来不像个路径。
    out_abs = os.path.abspath(out_path)
    if out_abs.startswith(root + os.sep):
        spec["ensemble"] = os.path.relpath(ens_path, os.path.dirname(out_abs))
    else:
        spec["ensemble"] = ens_path

    try:
        os.makedirs(os.path.dirname(ens_path), exist_ok=True)
        os.makedirs(os.path.join(root, "instruments"), exist_ok=True)
    except OSError as e:
        raise SystemExit(f"建目录失败: {e}") from e
    for slug, d in insts.items():
        _dump_yaml(os.path.join(root, "instruments", f"{safe_name(slug)}.yaml"), d)
    _dump_yaml(ens_path, {"id": stem, "name": f"{stem}（导入）", "seats": seats})

    # 没有 GM 音源声明的声部会全部听起来像钢琴——这必须说出来，不能让它"看起来渲染成功了"
    missing = [d for d in insts.values() if not d["gm_program"]]
    if missing:
        print(
            f"  ! {len(missing)} 个声部没有声明 GM 音源（导入成 #0 钢琴）："
            f"{', '.join(d['name'] for d in missing)}"
        )
        print(f"    要换音色，改 {ens_path} 里对应座位的 gm_program，或改 instruments/<slug>.yaml")

    head = (
        f"# 由 importxml.py 从 {sc['path']} 生成——**不要把它当手写谱来回改**，\n"
        f"# 重新导入会覆盖。要改就在这里改，然后用 render.py 渲染。\n"
        f"# 损失报告见 `importxml.py {sc['path']}`。\n"
    )
    # out_path 是用户在命令行上自己给的，和 `--report` 的输入一样，属于受信输入。
    try:
        with open(out_path, "w") as f:
            f.write(head)
            y.safe_dump(spec, f, allow_unicode=True, sort_keys=False, width=200)
    except OSError as e:
        raise SystemExit(f"写不了 {out_path}: {e}") from e
    except y.YAMLError as e:
        raise SystemExit(f"{out_path} 的 YAML 写不出来: {e}") from e

    # 写完立刻渲染一次：不渲染就等于没有验证过（P8）
    r = subprocess.run(
        [
            sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "render.py"),
            os.path.abspath(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return spec, ens_path, r


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    flags = {a for a in argv if a.startswith("-")}
    if not args or "--help" in flags or "-h" in flags:
        print(__doc__)
        return 0
    sc = parse(args[0])
    if "--json" in flags:
        print(json.dumps({k: v for k, v in sc.items() if k != "loss"}, ensure_ascii=False, indent=2))
        print(json.dumps(sc["loss"], ensure_ascii=False, indent=2))
    elif "--as-spec" in flags:
        out = args[1] if len(args) > 1 else os.path.splitext(args[0])[0] + ".yaml"
        root = os.environ.get("INGEST_ROOT") or None
        spec, ens, r = write_import(sc, out, root)
        print(f"\n  写了 {out}")
        print(f"        {ens}")
        print(f"        {len(spec['material'])} 个 instruments/*.yaml")
        print(f"  验证：render.py -> {'通过' if r.returncode == 0 else '失败'}")
        if r.returncode != 0:
            print("  " + (r.stderr or r.stdout).strip().replace("\n", "\n  "))
            return 1
    elif "--dump" in flags:
        if len(args) < 2:
            raise SystemExit("--dump 要给声部名（可以是片段）")
        dump(sc, args[1])
    else:
        report(sc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))