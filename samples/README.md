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
| `farewell-steampunk.mp3` | 《送别》· 蒸汽机，全长 40 小节 / 104 秒 |
| `farewell-steampunk.mid` | 同上，事件层 |

> 蒸汽机版的 `.wav` 没入库：19.6 MB，一条命令可重建（见下）。

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

---

## 蒸汽机版（另一个读法）

同一首《送别》，换一套编制、换一套和声，旋律一个字没改。
目标是**冷**，不是悲伤。

### 是什么

40 小节 / 104 秒 @92 BPM。8 件全是电子 / 键盘 / 打击，**一件摇滚乐器都没有**——
加这套编制没改 `render.py` 一行代码，这就是 C1（编制是输入）的直接检验。

全曲只用**空五度** `[0,7]`：没有三度就没有大小调色彩，只剩骨架。

```text
 1-4    启动：只有嗡鸣 + 齿轮 + 时钟
 5-8    机器开始哼：先只会两个音（5 3），第 7 小节换一句（6 5）
 9-12   机器学唱：5 3 5 反复三次，第三次"记错"升到高音 do
13-16   长亭外古道边芳草碧连天        人声进来，裸的
17-20   晚风拂柳笛声残夕阳山外山      低音进来
21-24   天之涯地之角知交半零落        最满
25-28   一壶浊酒尽余欢今宵别梦寒      和声转到 Am/G 上打转，不肯落地
29-32   冷高潮：第一句整句升八度，低音 + 人声 + 底鼓全撤
33-35   上紧：5 3 5 → 6 4 6 模进
36      停摆
37-40   只剩齿轮 + 嗡鸣，淡出
```

实测：−54 → −34(25-28) → **−39**(29-32) → −36(33-35) → −48(36) → −69(40) dB。

### 这一版唯一想证明的事，和它没证明的部分

冷高潮原本的设计是"撤低音 = 变薄变高"。量下来不成立：

| | 25-28（最满） | 29-32 只撤低音 | 29-32 撤底鼓 |
| --- | --- | --- | --- |
| <160 Hz | 34.4% | 30.7% | **21.7%** |
| >1.3 kHz | 1.7% | 25.3% | 29.0% |

撤低音只掉了 3.7 个点；**撑着低频的从头到尾是底鼓**。
所以这一版最后撤的是底鼓，不是贝斯（贝斯也撤了，但那不是起作用的那一下）。

这和长号降八度那次盲测是同一类错误：**改的不是我以为我在改的东西。**

### 蒸汽机版怎么重建

```bash
cd spikes/01
python3 render.py farewell-steampunk.yaml
python3 audio.py farewell-steampunk.yaml -o farewell-steampunk.wav   # engine=gm
python3 measure.py farewell-steampunk.wav 92 40
python3 probe.py farewell-steampunk.yaml --bars 29-32                # 频谱分带
ffmpeg -y -i farewell-steampunk.wav -c:a libmp3lame -q:a 4 farewell-steampunk.mp3
```

### 蒸汽机版已知未解决

- 前 10 秒（1-4 小节）低频占 47.5%，偏闷。music box 从第 5 小节提前进来只补了 1.4 个点
- 33-36 的"上紧 → 停摆"是硬切（−36 直掉 −48），没有铺垫
- 29-32 现在够"轻"了，但够不够"凉"只有耳朵知道
