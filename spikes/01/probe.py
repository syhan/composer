#!/usr/bin/env python3
"""探针：把一段音乐摊开成可以对着看的分量。

它**不理解你说的话**。它只是把该看的摆出来，让你（或者我）自己去连
"单薄" 和 "160 Hz 以下只有 3.9%" 这两件事。

    probe.py farewell-v3.yaml --bars 17-20
    probe.py farewell-v3.yaml --bars 17-20 --solo violin
    probe.py farewell-v3.yaml --no-audio          # 只算乐谱层，不渲染

存在的理由：第一次真实跑通内环时，"句子 → 数字" 这一步是我临场写的
Python。如果那一步是整条通路里最值钱的，它就不该靠每次现编。

⚠️ 这里输出的全部是 C18 的第三类：描述性测量。
   没有一行是警告，也不会变成 lint。"这段太挤" 不在这里，也不该在这里。
"""
import os
import shutil
import sys
import tempfile
import wave

import numpy as np
import yaml

import render

NAME = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
BANDS = [(20, 80), (80, 160), (160, 320), (320, 640),
         (640, 1280), (1280, 2560), (2560, 5120), (5120, 12000)]


def nm(p):
    return f"{NAME[p % 12]}{p // 12 - 1}"


def _w(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


def pad(s, n):
    """按显示宽度补空格（中文算两格），否则表头永远对不齐。"""
    return str(s) + " " * max(0, n - _w(s))


def rpad(s, n):
    return " " * max(0, n - _w(s)) + str(s)


def bar_seconds(spec):
    m = spec["meta"]
    return m["meter"][0] * 60.0 / m["tempo"]


def collect(bars, parts, lo, hi):
    """每个声部在这一段里实际发了什么音（时长加权）。"""
    out = {}
    for part in parts:
        ps, iv = [], []
        for b in range(lo, hi + 1):
            for (p, st, du, pitches, v) in bars.get(b, []):
                if p is not part:
                    continue
                ps += list(pitches) * du
                if len(pitches) > 1:
                    iv.append(max(pitches) - min(pitches))
        if ps:
            out[part["name"]] = {"ps": ps, "iv": iv}
    return out


def section_register(rows):
    print("\n  音区塔（按中位排序；发声时长加权）")
    print("    " + pad("声部", 18) + rpad("最低", 6) + rpad("最高", 6)
          + rpad("中位", 6) + rpad("发声拍", 8) + rpad("和弦内音程", 12))
    for name, d in sorted(rows.items(), key=lambda kv: int(np.median(kv[1]["ps"]))):
        ps = d["ps"]
        med_iv = f"{int(np.median(d['iv']))} 半音" if d["iv"] else "—"
        print("    " + pad(name[:17], 18) + nm(min(ps)).rjust(6) + nm(max(ps)).rjust(6)
              + nm(int(np.median(ps))).rjust(6) + f"{len(ps)/4:.0f}".rjust(8)
              + med_iv.rjust(12))


def section_octaves(rows):
    hist = {}
    for d in rows.values():
        for x in d["ps"]:
            hist[x // 12 - 1] = hist.get(x // 12 - 1, 0) + 1
    if not hist:
        return
    top = max(hist.values())
    print("\n  八度分布（时长加权，1 单位 = 一个十六分音符的时长）")
    for oct in range(min(hist), max(hist) + 1):
        v = hist.get(oct, 0)
        print(f"    C{oct:<3}{v:>6}  {'#' * int(40 * v / top)}")


def section_spectrum(x, sr):
    x = x.astype(float).mean(axis=1) / 32768.0
    if not len(x):
        return
    X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / sr)
    P, tot = X ** 2, (X ** 2).sum() or 1.0
    print("\n  频谱分带（渲染后文件里实际有多少能量）")
    for lo, hi in BANDS:
        p = P[(f >= lo) & (f < hi)].sum() / tot
        print(f"    {lo:>5}-{hi:<5} Hz  {100*p:>5.1f}%  {'#' * int(60 * p)}")
    low = 100 * P[f < 160].sum() / tot
    mid = 100 * P[(f >= 160) & (f < 1280)].sum() / tot
    high = 100 * P[f >= 1280].sum() / tot
    print(f"\n    低频 <160 Hz {low:.1f}%    中频 160–1.3k {mid:.1f}%    高频 >1.3k {high:.1f}%")


def section_bars(bars, harmony, lo, hi):
    chord_of = {h["bar"]: h["name"] for h in harmony}
    print("\n  每小节")
    print("    " + pad("bar", 6) + rpad("音符数", 8) + rpad("在场件数", 10)
          + rpad("发声拍", 8) + "  和弦")
    for b in range(lo, hi + 1):
        evs = bars.get(b, [])
        names = {p["id"] for (p, *_r) in evs}
        beats = sum(du for (_p, _st, du, _ps, _v) in evs) / 4
        print("    " + pad(b, 6) + f"{len(evs)}".rjust(8) + f"{len(names)}".rjust(10)
              + f"{beats:.0f}".rjust(8) + "  " + chord_of.get(b, "—"))


def parse(argv):
    o = {"spec": None, "bars": None, "solo": set(), "mute": set(),
         "engine": "gm", "audio": True}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--bars", "--solo", "--mute", "--engine") and i + 1 < len(argv):
            v = argv[i + 1]
            i += 2
            if a == "--bars":
                lo, _, hi = v.partition("-")
                o["bars"] = (int(lo), int(hi or lo))
            elif a == "--solo":
                o["solo"] = {s.strip() for s in v.split(",") if s.strip()}
            elif a == "--mute":
                o["mute"] = {s.strip() for s in v.split(",") if s.strip()}
            else:
                o["engine"] = v
            continue
        if a == "--no-audio":
            o["audio"] = False
        elif a.startswith("-"):
            raise SystemExit(f"不认识的参数 {a}")
        else:
            o["spec"] = a
        i += 1
    if not o["spec"]:
        raise SystemExit(__doc__)
    return o


def main():
    o = parse(sys.argv[1:])
    import audition

    v = audition.load(o["spec"])
    spec = v["spec"]
    total = len(spec["harmony"])
    lo, hi = o["bars"] or (1, total)
    lo, hi = max(1, lo), min(total, hi)
    bars = audition.focus(v["bars"], o["solo"], o["mute"])
    spb = bar_seconds(spec)

    scope = f"bar {lo}-{hi}"
    if o["solo"]:
        scope += "  solo " + ",".join(sorted(o["solo"]))
    if o["mute"]:
        scope += "  mute " + ",".join(sorted(o["mute"]))
    print(f"\n  探针  {os.path.basename(o['spec'])}   {scope}")

    rows = collect(bars, v["parts"], lo, hi)
    if not rows:
        print("\n  （这一段没有任何声部在发声）")
        return
    section_register(rows)
    section_octaves(rows)
    section_bars(bars, spec["harmony"], lo, hi)

    if o["audio"]:
        tmpdir = tempfile.mkdtemp(prefix="probe-")
        try:
            pre = max(1, lo - 1)
            wav = audition.render_version(v, bars, pre, hi, o["engine"], tmpdir, "probe")
            w, sr = audition.read_wav(wav)
            seg = audition.take(w, sr, (lo - pre) * spb, (hi - pre + 1) * spb)
            section_spectrum(seg, sr)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    print()


if __name__ == "__main__":
    main()
