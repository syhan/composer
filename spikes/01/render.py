#!/usr/bin/env python3
"""spike 1: spec (YAML) -> MIDI + ASCII piano roll.

Deliberately dependency-free apart from PyYAML. No MuseScore, no DAW,
no network: this is the inner loop, and it has to run headless.
"""
import sys
import yaml

PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR = [0, 2, 4, 5, 7, 9, 11]
DRUMS = {"kick": 36, "snare": 38, "hh": 42, "ohh": 46, "crash": 49, "tom": 45, "ride": 51}


# ---------- spec expansion ----------

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
    """简谱式：1..7 为 C 大调音级，8 = 高八度 1。"""
    semis = MAJOR[(deg - 1) % 7] + 12 * ((deg - 1) // 7)
    return (base_octave + 1) * 12 + semis + transpose


def resolve(pitch, h, part, bar, prev=None):
    """和弦音型 -> 具体音高集合。"""
    base = (part["base_octave"] + 1) * 12
    root_pc = h["chord"][0]
    bass_pc = h["bass"]
    root = base + root_pc
    if pitch == "root":
        p = (part["base_octave"] + 1) * 12 + bass_pc
        while p < base:
            p += 12
        return [p]
    if pitch == "nearest":
        # 声部进行：取离上一个音最近的弦内音（Tymoczko 的距离最小化）
        tones = [base + (pc - root_pc) % 12 + 12 * k
                 for pc in h["chord"] for k in (-1, 0, 1)]
        if prev is None:
            return [min(t for t in tones if t >= base - 6)]
        return [min(tones, key=lambda t: (abs(t - prev), t))]
    if pitch == "fifth":
        return [root + 7]
    if pitch == "third":
        return [root + h["chord"][1] - h["chord"][0]]
    if pitch == "octave":
        return [root + 12]
    if pitch == "power":
        return [root, root + 7]
    if pitch == "chord":
        return [base + (pc - root_pc) % 12 for pc in h["chord"]]
    if pitch == "arp":
        tones = [base + (pc - root_pc) % 12 for pc in h["chord"]]
        return tones
    raise ValueError(pitch)


def onsets(rhythm, grid):
    rhythm = (rhythm + "-" * grid)[:grid]
    return [i for i, c in enumerate(rhythm) if c == "x"]


def runs(rhythm, grid):
    """x 的位置 -> 到下一个 x 的步数（连奏长度）。"""
    ons = onsets(rhythm, grid)
    out = []
    for i, o in enumerate(ons):
        nxt = ons[i + 1] if i + 1 < len(ons) else grid
        out.append((o, nxt - o))
    return out


def expand(spec):
    grid = spec["meta"]["grid"]
    total = len(spec["harmony"])
    harmony = spec["harmony"]
    bars = {}          # bar -> list of (part, step, dur_steps, pitches, velocity)
    melody_cache = {}

    # 两轮：先展开所有本体声部，再展开 melody_of（对位/齐奏）
    order = [p for p in spec["parts"] if "melody_of" not in str(p.get("segments"))]
    order += [p for p in spec["parts"] if "melody_of" in str(p.get("segments"))]

    for part in order:
        is_drum = part.get("drum", False)
        for seg in part["segments"]:
            blist = parse_bars(seg["bars"], total)
            if seg.get("rest"):
                for b in blist:
                    bars.setdefault(b, [])
                continue

            if "melody_of" in seg:
                for b in blist:
                    src = melody_cache.get(seg["melody_of"], {}).get(b, [])
                    bars.setdefault(b, []).extend(
                        (part, st, du, [p + seg.get("unison", 0) for p in ps], v)
                        for (st, du, ps, v) in src
                    )
                continue

            for b in blist:
                h = chord_at(harmony, b)
                vel = seg.get("velocity", 80)
                tr = seg.get("transpose", 0)
                evs = []
                if "degrees" in seg:
                    step = 0
                    for tok in seg["degrees"].split():
                        deg, dur = tok.split(":")
                        evs.append((step, int(dur), [scale_degree(int(deg), part["base_octave"], tr)], vel))
                        step += int(dur)
                elif "pattern" in seg:
                    layers = [(seg["pattern"], seg.get("velocity", 80))]
                    for k in ("also", "extra"):
                        if k in seg:
                            layers.append((seg[k], seg.get("velocity", 80) if k == "also" else max(30, seg.get("velocity", 80) - 30)))
                    for pat, lvel in layers:
                        pitch, rhythm = pat["pitch"], pat["rhythm"]
                        if is_drum:
                            for st, du in runs(rhythm, grid):
                                evs.append((st, 1, [DRUMS[pitch]], lvel))
                        elif pitch == "arp":
                            tones = resolve("arp", h, part, b)
                            for st, du in runs(rhythm, grid):
                                evs.append((st, du, [tones[(st // 2) % len(tones)]], lvel))
                        else:
                            prev = part.setdefault("_prev", None)
                            ps = resolve(pitch, h, part, b, prev)
                            part["_prev"] = ps[0]
                            for st, du in runs(rhythm, grid):
                                evs.append((st, du, [p + tr for p in ps], lvel))
                bars.setdefault(b, []).extend((part, st, du, ps, v) for (st, du, ps, v) in evs)
                melody_cache.setdefault(part["name"], {}).setdefault(b, []).extend(
                    (st, du, ps, v) for (st, du, ps, v) in evs)

    return bars


# ---------- MIDI ----------

def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def track_chunk(events):
    """events: list of (abs_tick, order, bytes)"""
    events = sorted(events, key=lambda e: (e[0], e[1]))
    data, last = b"", 0
    for tick, _o, payload in events:
        data += vlq(tick - last) + payload
        last = tick
    data += vlq(0) + b"\xff\x2f\x00"
    return b"MTrk" + len(data).to_bytes(4, "big") + data


def build_midi(spec, bars, melody=True):
    meta = spec["meta"]
    div = meta["division"]
    grid = meta["grid"]
    tps = div // (grid // meta["meter"][0])

    conductor = []
    tempo = int(60_000_000 / meta["tempo"])
    conductor.append((0, 0, b"\xff\x51\x03" + tempo.to_bytes(3, "big")))
    conductor.append((0, 1, b"\xff\x58\x04" + bytes([meta["meter"][0], 2, 24, 8])))
    conductor.append((0, 2, b"\xff\x03" + bytes([len(meta["title"])]) + meta["title"].encode()))
    tracks = [track_chunk(conductor)]

    for part in spec["parts"]:
        if not melody and part.get("provisional"):
            tracks.append(track_chunk([(0, 0, b"\xff\x03" + bytes([9]) + b"(omitted)")]))
            continue
        ch = part["channel"]
        evs = [(0, 0, b"\xff\x03" + bytes([len(part["name"])]) + part["name"].encode()),
               (0, 1, bytes([0xC0 | ch, part["program"]]))]
        for bar in sorted(bars):
            for (p, st, du, pitches, vel) in bars[bar]:
                if p is not part:
                    continue
                t0 = (bar - 1) * grid * tps + st * tps
                t1 = t0 + du * tps
                for i, pitch in enumerate(pitches):
                    evs.append((t0, 2 + i, bytes([0x90 | ch, pitch & 0x7F, min(127, vel)])))
                    evs.append((t1, 2 + i, bytes([0x80 | ch, pitch & 0x7F, 0])))
        tracks.append(track_chunk(evs))

    header = b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big") + len(tracks).to_bytes(2, "big") + div.to_bytes(2, "big")
    return header + b"".join(tracks)


# ---------- ASCII roll ----------

def roll(spec, bars):
    grid = spec["meta"]["grid"]
    total = len(spec["harmony"])
    names = [p["name"] for p in spec["parts"]]
    print()
    for start in range(1, total + 1, 4):
        block = list(range(start, min(start + 4, total + 1)))
        head = "".join(f"|{chord_at(spec['harmony'], b)['name']:<16}" for b in block) + "|"
        print(f"  bar {block[0]:>2}-{block[-1]:<2}  {head}")
        for name in names:
            cells = []
            for b in block:
                line = ["."] * grid
                for (p, st, du, pitches, vel) in bars.get(b, []):
                    if p["name"] != name:
                        continue
                    for k in range(st, min(grid, st + du)):
                        line[k] = "#" if k == st else "|"
                cells.append("".join(line))
            label = name[:9]
            print(f"  {label:<10}" + " ".join(cells))
        print()


def presence(spec, bars):
    print("  bar  chord        n  parts")
    for b in range(1, len(spec["harmony"]) + 1):
        names = sorted({p["name"] for (p, *_r) in bars.get(b, [])})
        if not names:
            continue
        print(f"  {b:>3}  {chord_at(spec['harmony'], b)['name']:<12} {len(names)}  {', '.join(names)}")


def lint(spec, bars):
    """最小可用 lint：音域越界 + 段落重叠。（这正是 C1 说的“编译器报错”。）"""
    problems = []
    ranges = {p["name"]: p.get("range") for p in spec["parts"]}
    for b in sorted(bars):
        for (part, st, du, pitches, vel) in bars.get(b, []):
            rng = part.get("range")
            if not rng or part.get("drum"):
                continue
            for p in pitches:
                if p < rng[0] or p > rng[1]:
                    problems.append(
                        f"bar {b} {part['name']}: {note_name(p)} ({p}) "
                        f"超出实用音域 {note_name(rng[0])}–{note_name(rng[1])}")
    # 段落重叠（同一 part 被两个 segment 同时覆盖）
    for part in spec["parts"]:
        seen = {}
        for seg in part["segments"]:
            for b in parse_bars(seg["bars"], len(spec["harmony"])):
                if b in seen and not (seen[b].get("rest") and seg.get("rest")):
                    problems.append(f"bar {b} {part['name']}: 段落重叠（"
                                    f"{seen[b].get('bars')} 与 {seg.get('bars')}）")
                seen[b] = seg
    print("\n  lint:")
    if not problems:
        print("    (clean)")
    for p in problems:
        print("    - " + p)
    return problems


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "spikes/01/farewell-v3.yaml"
    spec = yaml.safe_load(open(path))
    bars = expand(spec)

    out = path.rsplit(".", 1)[0]
    midi = build_midi(spec, bars, melody=True)
    open(out + ".mid", "wb").write(midi)
    midi2 = build_midi(spec, bars, melody=False)
    open(out + "-nomelody.mid", "wb").write(midi2)

    print(f"wrote {out}.mid  ({len(midi)} bytes)")
    print(f"wrote {out}-nomelody.mid  ({len(midi2)} bytes)")
    roll(spec, bars)
    presence(spec, bars)
    lint(spec, bars)


if __name__ == "__main__":
    main()
