#!/usr/bin/env python3
"""spike 1: 把渲染结果量出来，而不是靠耳朵猜（P4）。

两个坑，踩过一次就够了：
  - fluidsynth 的 -F **追加**写入，不覆盖 → 渲染前必须 rm
  - fluidsynth 输出**立体声**，按帧算时间必须 reshape(-1, nch)
"""
import sys
import wave
import numpy as np


def load(path):
    w = wave.open(path)
    sr, nch = w.getframerate(), w.getnchannels()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32768
    return x.reshape(-1, nch).mean(axis=1), sr


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "farewell-v3-gm.wav"
    tempo = float(sys.argv[2]) if len(sys.argv) > 2 else 112
    bars = int(sys.argv[3]) if len(sys.argv) > 3 else 24
    x, sr = load(path)
    spb = 4 * (60.0 / tempo)                     # 4/4：一小节 4 拍
    print(f"{path}: {len(x)/sr:.1f}s, {bars} bars @ {tempo:g} BPM = {bars*spb:.1f}s of music")
    print("\n bar  RMS(dB)      Δ")
    prev = None
    for b in range(1, bars + 1):
        seg = x[int((b - 1) * spb * sr):int(b * spb * sr)]
        if not len(seg):
            break
        d = 20 * np.log10(max(1e-9, np.sqrt((seg ** 2).mean())))
        print(f" {b:3d} {d:8.1f}  {'—' if prev is None else f'{d-prev:+.1f}'}")
        prev = d


if __name__ == "__main__":
    main()
