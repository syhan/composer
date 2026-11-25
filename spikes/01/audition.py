#!/usr/bin/env python3
"""内环：听、比、改。

C10 的全部内容：让"听到 → 判断 → 改一点 → 再听"变便宜。
它不是播放器，是**比较器**。

    audition.py A.yaml                    # 听整曲
    audition.py A.yaml --bars 17-20 --loop 4
    audition.py A.yaml --solo violin      # 只留一件
    audition.py A.yaml --mute piano
    audition.py A.yaml B.yaml             # A/B，中间夹一段静默
    audition.py A.yaml B.yaml --order abab --blind
    audition.py A.yaml B.yaml --diff      # 顺带说清两版差在哪

设计上的三个硬决定：

1. **小节范围切的是音频，不是乐谱。** 从乐谱里抠第 17-20 小节会丢掉从前一小节
   拖过来的音的起音，听起来和真实的不一样。所以渲染 [start-1, end]，再按时间切。
   代价是多渲染一小节；好处是循环听起来和原曲一模一样。

2. **solo/mute 切的是乐谱。** 音频没法反混音。

3. **diff 只描述，不判断。** 说"钢琴的中位音程从 7 变成 14"，不说"这样更好"。
   分类按 C18：这里输出的全部是第三类（描述性测量），永远不会变成 lint。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np

import audio as audio_mod
import render

GAP = 0.35          # 两版之间的静默，够耳朵切换，又不会让人忘记上一版


# ---------- 载入 ----------

def load(path):
    spec = render.load_yaml(path, "spec")
    _ens, insts = render.load_ensemble(spec, path)
    meter = render.meter_of(spec)
    bars, parts, tracks, warns = render.expand(spec, insts, meter)
    bars = render.autoplace(bars, parts)
    bars, issues = render.apply_presence(spec, bars)
    return {"path": path, "spec": spec, "meter": meter, "bars": bars, "parts": parts,
            "tracks": tracks, "issues": issues, "warns": warns}


def focus(bars, solo, mute):
    """solo/mute 只能在乐谱层做——音频没法反混音。"""
    if solo:
        return {b: [e for e in evs if e[0]["id"] in solo] for b, evs in bars.items()}
    if mute:
        return {b: [e for e in evs if e[0]["id"] not in mute] for b, evs in bars.items()}
    return bars


# ---------- WAV ----------

def read_wav(path):
    with wave.open(path) as w:
        n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
        x = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, ch)
    return x, sr


def write_wav(path, x, sr):
    x = x if x.ndim == 2 else x.reshape(-1, 1)
    with wave.open(path, "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.astype("<i2").tobytes())
    return path


# ---------- 渲染 ----------

def render_version(v, bars, lo, hi, engine, tmpdir, tag):
    """渲染 [lo, hi] 这几小节，返回整段 WAV 的路径（时间轴从 lo 开始）。"""
    spec = v["spec"]
    sub = {b: bars.get(b, []) for b in range(lo, hi + 1)}
    mid = os.path.join(tmpdir, f"{tag}.mid")
    wav = os.path.join(tmpdir, f"{tag}.wav")
    data = render.build_midi(spec, sub, v["parts"], v["meter"], first=lo)
    try:
        with open(mid, "wb") as f:
            f.write(data)
    except OSError as e:
        raise SystemExit(f"写不出 MIDI: {mid}\n{e}") from e

    if engine == "gm":
        try:
            audio_mod.render_gm(mid, wav)
            return wav
        except audio_mod.GM_FAILURES as e:
            print(f"  ! gm 不可用（{e}），回落到 synth", file=sys.stderr)
            engine = "synth"
    # synth 只能整曲渲，反正它本来就是全谱
    audio_mod.render_synth(v["path"], wav)
    return wav


def take(x, sr, t0, t1):
    a, b = round(t0 * sr), round(t1 * sr)
    a, b = max(0, a), min(len(x), b)
    return x[a:b]


# ---------- 差异（只描述）----------

def flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = d
    return out


def spec_diff(a, b, limit=12):
    fa, fb = flatten(a), flatten(b)
    rows = []
    for k in sorted(set(fa) | set(fb)):
        if fa.get(k) != fb.get(k):
            rows.append((k, fa.get(k, "—"), fb.get(k, "—")))
    return rows[:limit], len(rows)


def event_diff(ba, bb):
    """按 (声部, 小节) 对比音符，合并成 (声部, 小节区间) 的 +/- 计数。"""
    def sig(bars):
        s = {}
        for bar, evs in bars.items():
            for (part, st, du, ps, vel) in evs:
                s.setdefault((part["id"], bar), []).append((st, du, tuple(ps), vel))
        return {k: sorted(v) for k, v in s.items()}

    sa, sb = sig(ba), sig(bb)
    per_part = {}
    for k in set(sa) | set(sb):
        pa, pb = sa.get(k, []), sb.get(k, [])
        if pa == pb:
            continue
        add = [e for e in pb if e not in pa]
        rem = [e for e in pa if e not in pb]
        pid, bar = k
        per_part.setdefault(pid, {}).setdefault("bars", set()).add(bar)
        per_part[pid]["add"] = per_part[pid].get("add", 0) + len(add)
        per_part[pid]["rem"] = per_part[pid].get("rem", 0) + len(rem)
        # 中位音程（同一小节内同时发声的音）最能说明"挤不挤"
        for _tag, evs, key in (("a", pa, "med_a"), ("b", pb, "med_b")):
            iv = [max(p) - min(p) for (_s, _d, p, _v) in evs if len(p) > 1]
            if iv:
                per_part[pid].setdefault(key, []).extend(iv)
    return per_part


def bars_str(bars):
    bars = sorted(bars)
    out, i = [], 0
    while i < len(bars):
        j = i
        while j + 1 < len(bars) and bars[j + 1] == bars[j] + 1:
            j += 1
        out.append(f"{bars[i]}" if i == j else f"{bars[i]}-{bars[j]}")
        i = j + 1
    return ",".join(out)


def print_diff(A, B, ba, bb, nbars):
    print("\n  改了哪些字段")
    rows, total = spec_diff(A["spec"], B["spec"])
    if not rows:
        print("    （spec 完全相同）")
    for k, va, vb in rows:
        print(f"    {k:<34} {str(va)[:18]:<18} → {str(vb)[:18]}")
    if total > len(rows):
        print(f"    …… 还有 {total - len(rows)} 处")

    print("\n  音符层面")
    pd = event_diff(ba, bb)
    if not pd:
        print("    （事件完全相同）")
    for pid in sorted(pd):
        d = pd[pid]
        bits = []
        if d.get("add"):
            bits.append(f"+{d['add']}")
        if d.get("rem"):
            bits.append(f"-{d['rem']}")
        med = ""
        if d.get("med_a") and d.get("med_b"):
            ma = int(np.median(d["med_a"]))
            mb = int(np.median(d["med_b"]))
            if ma != mb:
                med = f"   中位音程 {ma} → {mb} 半音"
        print(f"    {pid:<16} bars {bars_str(d['bars']):<12} {' '.join(bits)}{med}")


def rms_curve(x, sr, bar_secs):
    """逐小节 RMS。传的是一串小节时长——拍号变了它不是常数。"""
    out, t = [], 0.0
    for sec in bar_secs:
        seg = x[int(t * sr):int((t + sec) * sr)].astype(float) / 32768.0
        if not len(seg):
            break
        out.append(20 * np.log10(max(1e-9, float(np.sqrt((seg ** 2).mean())))))
        t += sec
    return out


# ---------- 播放 ----------

def play(path, seconds, timeout=30):
    """播一遍。注意：PipeWire 卡住时播放器会无限等，所以必须有超时。"""
    for cmd in (["pw-play"], ["paplay"], ["aplay", "-q"],
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            subprocess.run([exe] + cmd[1:] + [path], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
            return cmd[0]
        except subprocess.TimeoutExpired:
            print(f"  ! {cmd[0]} 没有响应（音频服务可能卡住了）。"
                  f"音频已经写好了，直接在桌面播这个文件。", file=sys.stderr)
            return None
    print("  ! 没找到播放器（pw-play / paplay / aplay / ffplay）", file=sys.stderr)
    return None


# ---------- 主 ----------

def parse_args(argv):
    o = {"specs": [], "bars": None, "loop": 1, "solo": set(), "mute": set(),
         "engine": "gm", "out": None, "play": False, "order": "ab",
         "blind": False, "diff": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--bars", "--loop", "--solo", "--mute", "--engine", "--out", "-o", "--order") and i + 1 < len(argv):
            v = argv[i + 1]
            i += 2
            if a == "--bars":
                lo, _, hi = v.partition("-")
                o["bars"] = (int(lo), int(hi or lo))
            elif a == "--loop":
                o["loop"] = max(1, int(v))
            elif a == "--solo":
                o["solo"] = {s.strip() for s in v.split(",") if s.strip()}
            elif a == "--mute":
                o["mute"] = {s.strip() for s in v.split(",") if s.strip()}
            elif a == "--engine":
                o["engine"] = v
            elif a in ("--out", "-o"):
                o["out"] = v
            elif a == "--order":
                o["order"] = v
            continue
        if a in ("--play", "--blind", "--diff"):
            o[a[2:]] = True
        elif a.startswith("-"):
            raise SystemExit(f"不认识的参数 {a}")
        else:
            o["specs"].append(a)
        i += 1
    if not o["specs"]:
        raise SystemExit(__doc__)
    return o


def main():
    o = parse_args(sys.argv[1:])
    versions = [load(p) for p in o["specs"]]
    if not versions:
        raise SystemExit("没有 spec")

    spec0 = versions[0]["spec"]
    total = len(spec0["harmony"])
    start, end = o["bars"] or (1, total)
    start, end = max(1, start), min(total, end)
    lo = max(1, start - 1)                  # preroll：保住从前一小节拖过来的音
    if o["engine"] == "synth":
        lo = 1                              # synth 只能整曲渲

    order = list(range(len(versions)))          # 播放顺序，元素是 versions 的下标
    if o["blind"]:
        np.random.default_rng().shuffle(order)
    seq = [versions[i] for i in order]
    labels = [chr(ord("A") + k) for k in range(len(seq))]   # 标签跟着**播放顺序**走

    tmpdir = tempfile.mkdtemp(prefix="audition-")
    try:
        chunks, curves, used_engine = [], [], o["engine"]
        sr = 0                                  # 由第一版渲染决定；后面必须一致
        for k, v in enumerate(seq):
            bars = focus(v["bars"], o["solo"], o["mute"])
            wav = render_version(v, bars, lo, hi=end, engine=o["engine"],
                                 tmpdir=tmpdir, tag=f"v{k}")
            x, sr_k = read_wav(wav)
            if sr and sr_k != sr:               # 采样率不一致就没法拼成一条时间轴
                raise SystemExit(f"采样率不一致：{sr} vs {sr_k}（{v['path']}）")
            sr = sr_k
            # 小节的秒数不是常数（7/8 比 4/4 短），所以只能按 tick 累加算秒。
            m = v["meter"]
            t_lo = m.secs(lo)
            seg = take(x, sr, m.secs(start) - t_lo,
                       m.secs(end) + m.bar_secs(end) - t_lo)
            curves.append(rms_curve(seg, sr, [m.bar_secs(b) for b in range(start, end + 1)]))
            if o["loop"] > 1:
                seg = np.tile(seg, (o["loop"], 1))
            chunks.append(seg)
        if not chunks:
            raise SystemExit("没有可播放的候选")
        gap = np.zeros((int(GAP * sr), chunks[0].shape[1]), dtype=chunks[0].dtype)

        if len(chunks) == 1:
            out = chunks[0]
        elif o["order"] == "abab":
            out = np.concatenate([chunks[0], gap, chunks[1], gap,
                                  chunks[0], gap, chunks[1]])
        else:
            pieces = []
            for i, c in enumerate(chunks):
                if i:
                    pieces.append(gap)
                pieces.append(c)
            out = np.concatenate(pieces)

        out_path = o["out"] or "audition.wav"
        write_wav(out_path, out, sr)

        # ---- 报告 ----
        scope = f"bar {start}-{end}" + (f" ×{o['loop']}" if o["loop"] > 1 else "")
        extra = []
        if o["solo"]:
            extra.append("solo " + ",".join(sorted(o["solo"])))
        if o["mute"]:
            extra.append("mute " + ",".join(sorted(o["mute"])))
        print(f"\n  {scope}   {len(out)/sr:.1f}s   engine={used_engine}"
              + (("   " + "  ".join(extra)) if extra else ""))

        if len(seq) > 1:
            if o["blind"]:
                key = os.path.abspath("audition-key.txt")
                with open(key, "w", encoding="utf-8") as f:
                    f.write("\n".join(
                        f"{lab}  {os.path.basename(v['path'])}"
                        for lab, v in zip(labels, seq, strict=True)) + "\n")
                print(f"    盲测：{len(seq)} 个候选按 {'→'.join(labels)} 播放")
                print(f"    先听，再打开 {key}")
                print("    （盲测时不打印 diff 与测量，否则等于提前告诉你答案）")
            else:
                for lab, v in zip(labels, seq, strict=True):
                    print(f"    {lab}  {os.path.basename(v['path'])}")
                if o["diff"]:
                    print_diff(seq[0], seq[1],
                               focus(seq[0]["bars"], o["solo"], o["mute"]),
                               focus(seq[1]["bars"], o["solo"], o["mute"]),
                               end - start + 1)
                # 描述性对比：每小节 RMS，不是判断
                print("\n  量出来（描述，不是判断）")
                head = "".join(f"{start+i:>7}" for i in range(len(curves[0])))
                print(f"    {'bar':<8}{head}")
                for lab, cv in zip(labels, curves, strict=True):
                    print(f"    {lab+' dB':<8}" + "".join(f"{v:>7.1f}" for v in cv))

        print(f"\n  {out_path}")
        if o["play"]:
            dur = len(out) / sr
            p = play(out_path, dur, timeout=int(max(30.0, dur + 20.0)))
            if p:
                print(f"  播放器 {p}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
