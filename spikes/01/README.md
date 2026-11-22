# spike 1｜spec → 可听 + 可量

只讲**怎么跑**。为什么这样设计，见仓库根目录的 `README.md`。

## 结构

```
instruments/*.yaml      一件乐器：gm_program / range / comfort / roles / idioms / drum_map
ensembles/band.yaml     这套编制：有哪些座位、谁有 overrides
farewell-v3.yaml        曲子：ensemble + 和声 + 在场路径 + 动态
                        + material（骨架，与谁演奏无关）+ parts（carries / segments）
render.py               spec → MIDI + 卷帘 + 血统 + lint + 演奏可行性
audio.py                spec → WAV（gm / synth 两种引擎）
synth.py                零依赖 numpy 合成器
measure.py              WAV → 每小节 RMS
lyrics.py               歌词逐字对到主题轨上
relations.py            成对关系指标
smoke.sh                所有入口跑一遍
```

## 命令

```bash
cd spikes/01

./smoke.sh                              # 先跑这个：所有入口 + 计时
python3 render.py farewell-v3.yaml      # MIDI + 卷帘 + lint
python3 render.py --selftest            # 变换算子的手算样例
python3 audio.py farewell-v3.yaml -o out.wav --engine gm
python3 measure.py out.wav 112 24
python3 lyrics.py farewell-v3.yaml
python3 relations.py farewell-v3.yaml
```

`audio.py` 的 `gm` 引擎失败会**如实回落**到 `synth` 并说明原因，不假装成功。

## 踩过的坑

1. **fluidsynth 会挂死**，不是慢，是不返回。两个独立原因：启动时去连 PipeWire/Pulse 的套接字，
   以及枚举 ALSA 设备——两者都在解析 `-a file` 之前发生。隔离办法（`audio.py` 里已做）：
   `XDG_RUNTIME_DIR`/`PIPEWIRE_RUNTIME_DIR`/`PULSE_RUNTIME_PATH` 指向空目录、
   `PULSE_SERVER`/`DBUS_SESSION_BUS_ADDRESS` 指向不存在、`ALSA_CONFIG_PATH=/dev/null`。
   不隔离时 10 次挂 2 次；隔离后 30/30。
   另外它的 stdout 是块缓冲，卡住时看起来像"没有任何输出"。
2. **`-F` 是追加写入**，不覆盖。永远写新文件再改名，否则两次渲染首尾相接，
   逐小节测量全错——而且错得很像"音乐有问题"。
3. **输出是立体声**，按帧算时间必须 `reshape(-1, nch)`，否则时长看起来是两倍。
4. **velocity 做不出段落级强弱**，必须靠 CC7 通道音量（spec 里的 `dynamics`）。
5. **外部进程一律带 timeout**。没有超时的那一次，一条永不返回的命令吃掉了一小时。

## 约定

- 工作渲染（`*.mid` / `*.wav` / `*.mp3`）不进仓库；`samples/` 下是"被接受的版本"，要进。
- 每个指标都要有一条小到能手算的样例（`render.py --selftest` 就是干这个的）。
