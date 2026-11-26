#!/usr/bin/env python3
"""MusicXML 导入 —— 第一步：只读，只报告。

    importxml.py ref.musicxml                # 人读的报告
    importxml.py ref.musicxml --json         # 同一份东西，机器读
    importxml.py ref.musicxml --dump tune    # 把某个声部摊成 tick 表（调试用）

为什么先只做报告：MusicXML 的坑不在"读"，在**读错了不会报错**。
移调乐器漏了 `<transpose>`，整条声部低一个大二度；多声部的 `<backup>`
没处理，第二声部整体后移；反复没展开，后面每一小节都错位。这些都不会抛异常，
只会安静地出一份"有点怪的"编曲。

所以第一步只回答一个问题：**这个文件里到底有什么，哪些我们会丢。**
一行 spec 都不生成，就不可能出错。

刻意用 stdlib（xml.etree），不引 music21：这一步只读结构、不解释语义。
等真要生成 spec 了，再把 music21 关在 `--as-spec` 那条路上——那时候
"理解得对不对"才开始重要。
"""

import json
import sys
import xml.etree.ElementTree as ET
from math import lcm

# 我们内部一律用 480 tick 一个四分音符（division: 480）。
# 但文件自己的 divisions 可能不是 480，而且**每个声部可以不同**。
HOME_DIV = 480


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
            ctx["harmony"] += 1
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
            names[sp.get("id")] = text(sp, "part-name", sp.get("id"))
    else:
        raise SystemExit("缺 part-list：没有声部名字，就没法给座位起名")

    loss = Loss()
    divs, parts, tempos, meters = set(), {}, [], []
    tuplets, harmony = [], 0
    for part_el in kids(root, "part"):
        pid = part_el.get("id")
        ctx = {
            "part": pid,
            "bar": 0,
            "divisions": None,
            "transpose": 0,
            "meter": [],
            "tempos": [],
            "harmony": 0,
            "tuplets": [],
        }
        evs = []
        for m in kids(part_el, "measure"):
            ctx["bar"] = int(m.get("number", "0").split(".")[0] or 0)
            evs += parse_measure(m, ctx, loss, divs)
        parts[pid] = {
            "name": names.get(pid, pid),
            "events": [e for e in evs if not e["grace"]],
            "grace": sum(1 for e in evs if e["grace"]),
            "transpose": ctx["transpose"],
            "divisions": ctx["divisions"],
            "bars": ctx["bar"],  # 最后一个小节的编号：空小节也算
        }
        tempos += ctx["tempos"]
        meters += ctx["meter"]
        tuplets += ctx["tuplets"]
        harmony += ctx["harmony"]
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
        "harmony": harmony,
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
    nd = norm_division(divs)
    print(f"\n  {sc['path']}")
    print(f"    {sc['kind']}   {len(sc['parts'])} 个声部   {sc['bars']} 小节")
    print(f"    divisions {divs or '未声明'}  ->  建议 meta.division: {nd}")
    if divs and any(nd % d for d in divs):
        print("    ! 有除不尽的声部，位置会落到非整 tick 上")
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
    elif "--dump" in flags:
        if len(args) < 2:
            raise SystemExit("--dump 要给声部名（可以是片段）")
        dump(sc, args[1])
    else:
        report(sc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))