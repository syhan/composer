#!/usr/bin/env python3
"""spike 1: 把渲染结果量出来，而不是靠耳朵猜（P4）。

两个坑，踩过一次就够了：
  - fluidsynth 的 -F **追加**写入，不覆盖 → 渲染前必须 rm
  - fluidsynth 输出**立体声**，按帧算时间必须 reshape(-1, nch)

小节边界不再假设 4/4：从谱面读 meter，逐小节算秒数。
以前这里是 `spb = 4 * (60/tempo)`——拍号一换，所有小节都会错位，
而且错得很安静（表还是能打出来，数字也像模像样）。
"""
import sys
import wave

import numpy as np

import render


def load(path):
    with wave.open(path) as w:
        sr, nch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32768
    return x.reshape(-1, nch).mean(axis=1), sr


def main():
    if len(sys.argv) < 3:
        raise SystemExit("用法: measure.py <wav> <spec.yaml>")
    path, spec_path = sys.argv[1], sys.argv[2]
    spec = render.load_yaml(spec_path, "谱面")
    meter = render.meter_of(spec)
    total = len(spec["harmony"])
    x, sr = load(path)

    print(f"{path}: {len(x)/sr:.1f}s, {total} 小节 @ {meter.tempo} BPM "
          f"= {meter.total_secs():.1f}s of music")
    print("\n bar  meter   RMS(dB)      Δ")
    prev = None
    for b in range(1, total + 1):
        t0 = meter.secs(b)
        t1 = t0 + meter.bar_secs(b)
        seg = x[int(t0 * sr):int(t1 * sr)]
        if not len(seg):
            break
        d = 20 * np.log10(max(1e-9, np.sqrt((seg ** 2).mean())))
        bar = meter[b]
        print(f" {b:3d}  {bar.beats}/{bar.unit}   {d:8.1f}  "
              f"{'—' if prev is None else f'{d-prev:+.1f}'}")
        prev = d


if __name__ == "__main__":
    main()
