# spike 1｜spec → 可听

三个文件，零依赖（除 numpy / PyYAML）：

```bash
python3 spikes/01/render.py spikes/01/farewell-v3.yaml   # MIDI + 钢琴卷帘 + lint
python3 spikes/01/synth.py  spikes/01/farewell-v3.yaml   # WAV（合成器很糙，判结构够用）
```

产出（已 gitignore，随时重建）：

- `farewell-v3.mid` / `farewell-v3-nomelody.mid`
- `farewell-v3.wav`

时间预算：合成 0.9 秒。**这是"内环 ≤2 秒"的第一次实测。**

> 旋律是占位猜测（`Violin.provisional: true`）。换掉它只改 YAML 里那 20 行 `degrees`，
> 其余全部不动——这就是"pin 一个声部、生成其余"的样子。
