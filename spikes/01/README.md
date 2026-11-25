# spike 1｜spec → 可听 + 可量

只讲**怎么跑**。为什么这样设计，见仓库根目录的 `README.md`。

## 结构

```
instruments/*.yaml      一件乐器：gm_program / range / comfort / roles / idioms / drum_map
ensembles/band.yaml     这套编制：有哪些座位、谁有 overrides
farewell-v3.yaml        曲子：ensemble + 和声 + 在场路径 + 动态
                        + material（骨架，与谁演奏无关）+ parts（carries / segments）
meter-selftest.yaml     拍号自检：4/4 → 7/8 [2,2,3] → 5/4 [3,2]，同一句旋律三种写法
render.py               spec → MIDI + 卷帘 + 血统 + lint + 演奏可行性
audio.py                spec → WAV（gm / synth 两种引擎）
audition.py             内环：循环 / solo / mute / A-B / 盲测 / diff
probe.py                把一段摊成可以对着看的分量（音区塔 / 八度 / 频谱）
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
python3 render.py --selftest            # 变换算子 + 拍号的手算样例
python3 audio.py farewell-v3.yaml -o out.wav --engine gm
python3 measure.py out.wav farewell-v3.yaml   # 小节边界从谱面读，不假设 4/4
python3 lyrics.py farewell-v3.yaml
python3 relations.py farewell-v3.yaml

# 内环：听、比、改
python3 audition.py farewell-v3.yaml --bars 17-20 --loop 4 --play
python3 audition.py farewell-v3.yaml --bars 17-20 --solo violin
python3 audition.py farewell-v3.yaml --bars 17-20 --mute piano
python3 audition.py A.yaml B.yaml --bars 17-20 --diff        # A/B + 说清差在哪
python3 audition.py A.yaml B.yaml --bars 17-20 --blind       # 盲测，顺序写进 audition-key.txt

# 探针：把一段摊开（它不理解你的话，只把该看的摆出来）
python3 probe.py farewell-v3.yaml --bars 17-20
python3 probe.py farewell-v3.yaml --bars 17-20 --solo violin
python3 probe.py farewell-v3.yaml --bars 17-20 --no-audio     # 只看乐谱层
```

`audio.py` 的 `gm` 引擎失败会**如实回落**到 `synth` 并说明原因，不假装成功。

## 踩过的坑

1. **fluidsynth 会卡在音频设备发现上**，而渲染到文件并不需要音频设备。它启动时先连 PipeWire/Pulse、
   再枚举 ALSA，两者都在解析 `-a file` 之前。音频栈一不健康，它就无限等；stdout 是块缓冲，
   所以看起来像"没有任何输出"。

   这次的真病因是**PipeWire 整个卡死**：`pw-play` / `pactl` / `pw-cli` 全部不退出。
   重启 `systemctl --user restart pipewire pipewire-pulse wireplumber` 后恢复正常。

   但 `audio.py` 仍然隔离（`XDG_RUNTIME_DIR` 指空目录、`ALSA_CONFIG_PATH=/dev/null` 等）：
   渲染不应该依赖音频服务是否健康——你在开会、在放音乐、或者服务又卡了，都不该影响内环。
   隔离后 30/30 成功，4 小节 80ms。
2. **`-F` 是追加写入**，不覆盖。永远写新文件再改名，否则两次渲染首尾相接，
   逐小节测量全错——而且错得很像"音乐有问题"。
3. **输出是立体声**，按帧算时间必须 `reshape(-1, nch)`，否则时长看起来是两倍。
4. **velocity 做不出段落级强弱**，必须靠 CC7 通道音量（spec 里的 `dynamics`）。
5. **外部进程一律带 timeout**。没有超时的那一次，一条永不返回的命令吃掉了一小时。
6. **PipeWire 卡住时，所有音频客户端都会无限等**：`pw-play` / `paplay` / `pactl` 全部挂住，
   连 `pw-cli info 0` 都是拿到 core 信息后不退出。修法是
   `systemctl --user restart pipewire pipewire-pulse wireplumber`。
   但 `--play` 仍然必须带超时，否则一次卡死就把整个内环拖死；音频文件总是能写出来的。

## 约定

- 工作渲染（`*.mid` / `*.wav` / `*.mp3`）不进仓库；`samples/` 下是"被接受的版本"，要进。
- 每个指标都要有一条小到能手算的样例（`render.py --selftest` 就是干这个的）。
