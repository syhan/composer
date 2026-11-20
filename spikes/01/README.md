# spike 1｜spec → 可听 + 可量

零依赖（numpy / PyYAML），全流程 < 1 秒，不需要 MuseScore / DAW / 网络。

```bash
cd spikes/01

# 1) spec -> MIDI + 钢琴卷帘 + 在场路径检查 + lint
python3 render.py farewell-v3.yaml

# 2) 真音源（FluidR3_GM）：MIDI -> 音频
rm -f farewell-v3-gm.wav                      # ← fluidsynth 是**追加**写入，必须先删
fluidsynth -ni -F farewell-v3-gm.wav /usr/share/soundfonts/FluidR3_GM.sf2 farewell-v3.mid

# 3) 量出来（不要靠耳朵猜）
python3 measure.py farewell-v3-gm.wav 112 24

# 4) 备用：零依赖糙合成器（没有 soundfont 时也能听结构）
python3 synth.py farewell-v3.yaml
```

## 三个已经踩过的坑

1. **fluidsynth 的 `-F` 追加写入**，不覆盖。不 `rm` 就会得到两段音频首尾相接，
   然后所有逐小节测量全错（而且错得很像"音乐有问题"）。
2. **fluidsynth 输出立体声**，按帧算时间必须 `reshape(-1, nch)`，否则时长看起来是两倍。
3. **velocity 做不出段落级强弱**。音符力度只管音符内部；`ff` 与 `mf` 的差别必须靠
   **CC7 通道音量自动化**（spec 里的 `dynamics`）。第一版没有 CC7，量出来的曲线是平的。

## 现在的实测曲线

```
bar  1    3    5    9    13   17   23   24
   -70  -56  -45  -40  -34  -23  -47  -20  dB
                            ↑全员      ↑拆人 ↑最强一击
```

单调上升 → 第 17 小节 +10.9 dB（全员进来）→ 第 23 小节 **-24.3 dB**（拆人）→
第 24 小节 **+27.2 dB**（最后一击）。设计意图现在是可以核对的数字。

## 待办

- 旋律：`Violin.provisional: true`，等真实简谱替换（只改那 20 行 `degrees`）。
