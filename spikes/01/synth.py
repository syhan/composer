#!/usr/bin/env python3
"""spike 1: spec -> WAV, 零依赖合成器（只要 numpy）。

存在的意义不是音色，而是**证明内环可以没有 MuseScore**：
spec -> 可听，全在一个 <2 秒的命令里。音色很糙，判结构够用。
"""
import sys
import wave

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from render import apply_presence, expand, load_ensemble, load_yaml

SR = 44100
KIND = {
    "percussion": "drums", "bass": "bass", "brass": "brass", "strings": "strings",
    "plucked": "pluck", "wind-synth": "reed", "keyboard": "pluck",
}
KIND_BY_ID = {"rhythm-guitar": "dist", "lead-guitar": "dist"}


def env_adsr(n, a=0.01, d=0.05, s=0.75, r=0.08):
    e = np.ones(n)
    ai, di, ri = int(a * SR), int(d * SR), int(r * SR)
    ai, di, ri = min(ai, n), min(di, n), min(ri, n)
    if ai:
        e[:ai] = np.linspace(0, 1, ai)
    if di:
        e[ai:ai + di] = np.linspace(1, s, di)
    e[ai + di:] = s
    if ri:
        e[-ri:] *= np.linspace(1, 0, ri)
    return e


def voice(kind, f, dur, vel):
    n = max(1, int(dur * SR))
    t = np.arange(n) / SR
    if kind == "bass":
        w = np.sin(2 * np.pi * f * t) + 0.45 * np.sin(4 * np.pi * f * t)
        e = env_adsr(n, 0.005, 0.12, 0.55, 0.06)
    elif kind == "brass":
        w = sum(np.sin(2 * np.pi * f * h * t) / h for h in range(1, 7))
        w *= 1 + 0.15 * np.sin(2 * np.pi * 5 * t)          # 一点点颤音
        e = env_adsr(n, 0.06, 0.1, 0.8, 0.12)
    elif kind == "strings":
        w = sum(np.sin(2 * np.pi * f * h * (1 + 0.0015 * k) * t) / h
                for k, h in enumerate(range(1, 8)))
        w *= 1 + 0.2 * np.sin(2 * np.pi * 5.5 * t)
        e = env_adsr(n, 0.05, 0.15, 0.85, 0.1)
    elif kind == "reed":
        w = sum(np.sin(2 * np.pi * f * h * t) / (h ** 1.4) for h in range(1, 9))
        e = env_adsr(n, 0.04, 0.08, 0.8, 0.08)
    elif kind == "pluck":
        w = sum(np.sin(2 * np.pi * f * h * t) / (h ** 1.7) for h in range(1, 6))
        e = np.exp(-t * 6.0)
    else:  # dist
        w = np.tanh(3.0 * sum(np.sin(2 * np.pi * f * h * t) / h for h in (1, 2, 3, 4, 5)))
        e = env_adsr(n, 0.004, 0.05, 0.9, 0.05)
    return w * e * (vel / 127.0)


def drum(kind_pitch, vel):
    n1 = int(0.18 * SR)
    if kind_pitch == 36:                                   # kick
        t = np.arange(int(0.22 * SR)) / SR
        f = np.linspace(130, 48, len(t))
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 16) * vel / 127
    if kind_pitch == 38:                                   # snare
        t = np.arange(n1) / SR
        nz = np.random.randn(len(t)) * np.exp(-t * 32)
        return (0.8 * nz + 0.3 * np.sin(2 * np.pi * 190 * t) * np.exp(-t * 26)) * vel / 127
    if kind_pitch == 42:                                   # closed hat
        t = np.arange(int(0.05 * SR)) / SR
        nz = np.diff(np.random.randn(len(t) + 1)) * np.exp(-t * 90)
        return nz * 0.35 * vel / 127
    t = np.arange(int(1.4 * SR)) / SR                      # crash
    return np.random.randn(len(t)) * np.exp(-t * 3.2) * 0.45 * vel / 127


def render(path, out=None):
    spec = load_yaml(path, "spec")
    _ens, insts = load_ensemble(spec, path)
    bars, parts, _tracks, _warns = expand(spec, insts)
    bars, _ = apply_presence(spec, bars)
    kind_of = {p["id"]: KIND_BY_ID.get(p["id"], KIND.get(p.get("family"), "pluck"))
               for p in parts}
    tempo, grid = spec["meta"]["tempo"], spec["meta"]["grid"]
    total_bars = len(spec["harmony"])
    step = (60.0 / tempo) / (grid / spec["meta"]["meter"][0])

    total = int(total_bars * grid * step * SR) + SR
    buf = np.zeros(total)

    for b in sorted(bars):
        for (part, st, du, pitches, vel) in bars[b]:
            t0 = int(((b - 1) * grid + st) * step * SR)
            kind = kind_of.get(part["id"], "pluck")
            if part.get("drum"):
                for p in pitches:
                    seg = drum(p, vel)
                    buf[t0:t0 + len(seg)] += seg[:max(0, total - t0)]
            else:
                for p in pitches:
                    f = 440.0 * 2 ** ((p - 69) / 12)
                    seg = voice(kind, f, du * step * 1.02, vel)
                    buf[t0:t0 + len(seg)] += seg[:max(0, total - t0)] * 0.22

    peak = np.max(np.abs(buf)) or 1.0
    buf = buf / peak * 0.92
    out = out or path.rsplit(".", 1)[0] + ".wav"
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((buf * 32767).astype("<i2").tobytes())
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "farewell-v3.yaml"
    print(f"wrote {render(path)}")


if __name__ == "__main__":
    main()
