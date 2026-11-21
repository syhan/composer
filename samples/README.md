# Samples

**约定：这里只放"被接受的版本"，不放每次渲染的工作产物。**
工作渲染留在 `spikes/01/`（已 gitignore）。一次迭代 6 条命令，随时重建。

**v3 与 v4 可以直接 A/B**，两者只差一处改动（见下）。

| 文件 | 说明 |
|---|---|
| `farewell-epic-march-v3.mp3` | FluidR3_GM 真音源，全曲 52 秒 |
| `farewell-epic-march-v3.wav` | 同上，无损（10.4 MB，可从 `.mid` 精确重建） |
| `farewell-epic-march-v3.mid` | 事件层，可丢进任何 DAW / MuseScore |
| `farewell-epic-march-v3-no-melody.mp3` | 去掉小提琴旋律：只听配器与动态 |
| `farewell-epic-march-v3-toy-synth.mp3` | 零依赖合成器版（没有 soundfont 时的备份） |
| `farewell-epic-march-v4.*` | **v4 = 修掉钢琴与旋律的音区碰撞** |

## v3 → v4 只改了一处

钢琴原本是 `wide`（左手根音 + 右手和弦贴着旋律音区），右手正压在小提琴身上：

| 一对 | v3 中位音程 | v4 中位音程 |
|---|---|---|
| Piano × Violin | **7 半音**（纯五度，糊） | **14 半音** |
| Acoustic Guitar × Piano | **4 半音** | **12 半音** |

改成 `frame`：低音留在下面，和弦整体上一个八度，把中频让给旋律。
其余一个音都没动——所以 A/B 时听到的差别，就是"中频让开"这一件事。

## 这一版是什么

《送别》20 小节简谱 + 4 小节引子 = 24 小节，112 BPM 恒定，9 人编制：
鼓 / 贝斯 / 长号 / 钢琴 / 木吉他 / 节奏吉他 / 主音吉他 / 电吹管 / 小提琴。

```
bar  1    3    5    9    13   17   21   23   24
   -73  -56  -45  -40  -32  -24  -21  -45  -20  dB
                        ↑节奏吉他 ↑全员 ↑顶点 ↑拆人 ↑最强
```

在场路径：1 人 → 3 → 5 → 6 → 7 → 9 → 3（第 23 小节拆人）→ 9（最后一击，落 C 空五度）。

## 重建

```bash
cd spikes/01
python3 render.py farewell-v3.yaml
rm -f farewell-v3-gm.wav                      # fluidsynth 是追加写入，必须先删
fluidsynth -ni -F farewell-v3-gm.wav /usr/share/soundfonts/FluidR3_GM.sf2 farewell-v3.mid
ffmpeg -y -i farewell-v3-gm.wav -t 52 -c:a libmp3lame -q:a 4 farewell-v3-gm.mp3
python3 measure.py farewell-v3-gm.wav 112 24
```

## 已知未解决

- 长号 / 电吹管**全程没有休息**（连续 80 / 24 拍，上限 8）——需要小节内的呼吸
- 等音拼写是全局降号，未按和声上下文决定
- 主音吉他现在只是把主题低八度加倍，没有独立对句
