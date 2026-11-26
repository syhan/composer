# AGENTS.md

项目的代理约定。**宪章在 `README.md`，动手之前先读它**（尤其 §2 原则、§4 约束、§8 现状）。
这份文件只写"每次启动都要知道、每次都会踩"的东西，不重复 README 的论证。

## 0. 这个项目是什么

把抽象音乐意图（「悲伤」「像某首歌」「副歌改得恢宏」）变成**立刻可听、可见、可自验**的
编曲草稿。目标不是产出音符，是产出**可持续、可迭代、可尝试、允许犯错的编曲模式**。

- **闭环是产品，表示层是资产**（P1）。AI 生成音符没有价值，IR 才是。
- **可逆 + 可指认 > 更接近**（P2）。收敛不是唯一的迭代形态，所以每次改动必须能指认到
  "是哪一下"，并且能撤回。
- **诚实抽象**（P8）。不返回假成功。宁可清楚失败，不做"看得见但没效果"的操作。
- **人是最终裁决者**（P10）。测量只用来定位，不用来评分（P4）。

## 1. 硬约束（改代码时不能破）

- **内环 ≤ 2 秒、完全离线、无重型 GUI**。MuseScore / Guitar Pro / DAW 只作**出口**，
  绝不进运行时依赖（C2）。
- **资产，不是对话**（P7）。编曲是仓库里可 diff、可复现、可分享的文件。
- **只维护一个 README.md**（除本文件外不新增文档）。有结论就写进 README，不另开小文件。
- **不改语义就改不动**：spec 是**有稳定路径的树**，一次修改 = 「路径 + 新值」，
  不是文本重写（C4）。文本重写的东西不能当真源。
- **记谱是值域，不是载体**。绝不在记谱语法上设计任何东西，只在出口做无损编码。
- **权重是词条级变量，不是全局常数**（C11）。没有"哪一层更重要"的全局常数。
- **约束挂在"对"上，不挂在层级上**（C13）。不设"人声优先"、不声明某轨主导；
  唯一硬规则是碰撞涉及承载文本的声部（理由是**信息**，不是审美）。
- **量词只在有单位的轴上**（C15）。"稍许/更多"只能落在半音、dB、音符数；感受轴没有刻度。
- **音域适配不得自动**（C16）。够不着就报差距，由人显式声明位移。

## 2. 改代码的规矩

1. **三首现有谱的 MIDI 必须逐字节不变**，除非这次改动**就是**要改它们：
   - `spikes/01/farewell-v3.mid` → `0b27025c47ee5eb4136b27c9b4ca389e`
   - `spikes/01/farewell-steampunk.mid` → `2a47647d0d7435ec3f0db62d6d67f698`
   - `spikes/01/meter-selftest.mid` → `67ccc747061b0d3e0c9fafedfb503066`

   改完照一下 MD5。地基动过（拍号、tick、坐标）时这是第一条验收，**没有例外**。
2. **新增入口必须加进 `spikes/01/smoke.sh`**，并**给每首谱都跑一遍**。
   一个入口只被一首谱覆盖过，等于没被覆盖（`synth.py` 就是这样坏了三天）。
3. **报错要能指认，不许静默跳过**。"听起来有点怪"之前先想：这里会不会静默产生
   一个错误但能渲染的结果。宁可 SystemExit。
4. **测量分三类，永不混淆**（C18）：硬错误 / 风险提示 / **描述性测量永不进 lint**。
   「旋律太平永远不是警告」。不要拿描述性输出当判断。
5. **不引入重依赖**。核心链路零依赖（PyYAML + numpy 是仅有的两个）。
   要用 music21 之类，必须关在一个文件里，并说明为什么核心链路不能依赖它。
6. **改公共函数签名/返回值时，先 grep 所有调用者**。`expand()` 那次四个文件都改了、
   漏了 `synth.py`，因为它没被 smoke 覆盖。
7. **不要用 `git checkout -- <file>` 覆盖可能有未提交改动的文件**。先 `git status`。
   （2026-11-26 因此冲掉过整个未提交的 `--as-spec`，靠字节码回放才找回来。）
8. **批量改名 / `re.sub` 时注意变量名碰撞**：`du` 改成 `_du` 而后面真的用到了，
   编译不报错、运行才炸；先 grep 再改。
9. **新东西要能手算验证**。如果不能手算，就得先想办法把它变得能手算——
   否则只有"跑通了"这个信号，而这个信号在本项目已经误导过至少三次。

## 3. 每次改完必须跑

```bash
cd spikes/01
./smoke.sh                 # 61 项，必须全绿（含三首谱 × 全套入口 + 导入往返）
/usr/bin/python3 render.py --selftest     # 27 条手算：变换算子 + 拍号
/usr/bin/python3 follow.py --selftest    # 22 条手算：键位表 + 状态机
md5sum farewell-v3.mid farewell-steampunk.mid meter-selftest.mid   # 见上面的不变量
/home/syhan/.pi-lens/pip-tools/bin/ruff check --no-cache *.py
/home/syhan/.pi-lens/pip-tools/bin/yamllint -c ../.yamllint *.yaml
```

端到端手验一条（改动涉及音频时）：

```bash
/usr/bin/python3 audio.py farewell-v3.yaml -o /tmp/a.wav -g
/usr/bin/python3 measure.py /tmp/a.wav farewell-v3.yaml
/usr/bin/python3 audition.py farewell-v3.yaml --bars 17-20 --loop 2 --play
/usr/bin/python3 follow.py farewell-v3.yaml --draw --no-audio --cols 96
```

## 4. 踩过的坑（先看，别重踩）

#### 静默失败类 —— 本项目最大的敌人，因为每一个都"能渲染、看起来合理"

- `(rhythm + "-" * grid)[:grid]` 把长度不对的节奏串**静默补齐/截断**。网格不等长之后
  后半小节会全空且不报错。现在长度不符直接 SystemExit。
- 事件坐标不裁剪渲染窗口 → 负 tick → 畸形 MIDI → **fluidsynth 永久挂死且不报错**。
  修法：`color_events` / `dynamics` 都要裁 `t < 0`，并把窗口前最后一档压到 tick 0。
- MIDI 轨名 meta 用**字符数**不是字节数 → 中文标题损坏音轨流 → fluidsynth 渲染 0 采样。
- 和弦音程从 **C** 而不是和弦根音算 → `Am` 渲染成 C 小调、`Ab` 成 C 大调，**不报错**，
  只是"听起来有点怪"。已有 `check_harmony()` 兜底，别绕过。
- `fluidsynth -F` 是**追加**不是覆盖 → 两次渲染拼成一条，逐小节测量全错，
  看起来像一个音乐问题。
- `fluidsynth` 输出是**立体声** → 不 `reshape(-1, nch)` 时长会翻倍。
- velocity **做不出段落级动态** → 要 CC7 通道音量。
- 重复座位 id **静默覆盖** → 后面所有通道号错位（现改为 SystemExit）。
- `relations.py` 跨整小节配对 → 三行密度列全是 3.6，**合理、自洽、不报错**（R7 的原型）。
- 没有 `theme` 轨的谱上 `lyrics.py` 抛 `StopIteration` → 现在说"没有歌词可对"并正常返回。

#### 播放器 / 视图类

- 播放器起不来是**静默**的：stderr 进 `DEVNULL`，外面看就是"按了没反应"。spawn 后
  必须查 `poll()`，把 stderr 打出来。
- `pw-play` 喂原始 PCM 必须 `--raw --format=s16`（`s16le` 是 `paplay` 的格式名）。
- 播放位置用"已喂入量" → pw-play 一上来抽干管道 → 进度条冲到底声音还在放。
  用 `min(墙钟, 已喂)`。
- 显示位置读 `st["bar"]` 而它**只在按键时更新** → 播放时整张图冻住。
  从 `st["t"]` 现算。
- `resolve_key` 把不在 `KEYMAP` 里的键**全吞掉** → s/m/l/q/[/] 全死、界面照常刷新，
  看起来就像"这些快捷键没用"。键位表本身必须被测。
- solo/mute 重渲了 MIDI 但 `Player.x` 还是旧的 → **灯是对的、耳朵是错的**。
- 画面行首宽度（`LABEL`）算错 → 小节号/▲/A/B 条整体左偏一列。
- `--play` 的超时不能写死，要按音频时长算。

#### 解析 / 数据类

- `division` **每个声部可以不同** → 取 lcm 才能都落在整 tick 上。
- 拍号**硬编码 4/4**（`spb = 4 * 60/tempo`）→ 对 4/4 一直是对的，**从来没人发现**，
  直到第一首 7/8。`measure.py` 的签名因此改成收 spec。
- `meta.grid` 和 `beats × subdivision` 是同一事实写两遍 → 现在只写 beats，
  `grid` 推导；写了不一致就报错。
- 拍号段落按**出现顺序**追加 → 顺序错。按小节号归并再排序。
- `use:` 复制素材时又乘了一次 `step_ticks`（素材事件已经是 tick）→
  拍数 508、卷帘整行连成一片，**图上看不出错在哪，只看得出很密**。
- 素材长度和格子数是两件事：`augment: 2` 在 7/8 上几乎总是拒绝。
  **"装不下"是音乐决定，不该由算术替人做**，宁可拒绝。
- 三连音是**精确**的，不是近似：`division: 480 = 2⁵·3·5` 能整除 3 和 5，
  一拍三分正好 160 tick。靠的是事件坐标从**格**改成**tick**。

#### 工程纪律类

- 改公共签名只改一半调用者 → 坏三天没人知道（见第 2 条规矩）。
- 导入没有 `<midi-program>` 的谱 → 所有声部听起来像钢琴。**必须打印出来**，
  不能让"渲染成功"冒充"导入成功"。
- 观察来的音域要和人工声明的**分开存**（`observed_range`），混在一起就是假事实。

## 5. 环境

- **`python3` 被 `/home/syhan/.pi-lens/pip-tools/bin/python3` 遮住，里面没有 PyYAML/numpy**。
  用 `/usr/bin/python3`（3.14.9，PyYAML 6.0.3，numpy 2.5.4）。
  `smoke.sh` 自带探测，别盲信 PATH。
- ruff / yamllint 在 `/home/syhan/.pi-lens/pip-tools/bin/`（`/usr/bin/python3 -m ruff` 不存在）。
- `pyrightconfig.json` 的 `extraPaths` 指向 `/usr/lib/python3.14/site-packages`（为了让检查器找到 numpy）。
- `spikes/01/ruff.toml` 声明 `known-first-party`：**没有它时 `import audio` 的分类取决于
  从哪个目录跑 ruff**，同一行 import 永远有一边报 I001。
- 音源：`/usr/share/soundfonts/FluidR3_GM.sf2`，`fluidsynth 2.6.2`。
  **GM 的低音区不可判**——长号在 B2/C3 没有可用基频，能量全跑到高次谐波，
  规格上是低音、听起来是高音。任何"低音够不够厚"的问题这套音源答不了。
- PipeWire 卡死时 `fluidsynth` 会无限等待（启动时先连 PipeWire/Pulse、再枚举 ALSA，
  **都在解析 `-a file` 之前**）。`audio.py` 里用空的 `XDG_RUNTIME_DIR` /
  `ALSA_CONFIG_PATH=/dev/null` 等做隔离，**不要去掉**。

## 6. 未完成（别当已完成）

- `clone:` / 槽位级 `--ab`（把 A/B 从文件级降到同一份谱里的两条轨，自动 mute 原轨）——**未实现**。
- 导出 MusicXML（要写 `<time-modification>`）——**未实现**。
- `audition.py` 的 **A/B 显著性预检**（分带能量差，低于阈值提示"这个改动可能听不出来"）——
  两次盲测都指向它，**未实现**。
- 蒸汽机版**前 10 秒偏闷**（1-4 小节低频占 47.5%）；**33-36「上紧→停摆」是硬切**（−36 直掉 −48，没铺垫）。
- 盲测结论已验证（见 README §8 D），但"给没听过的人测纲要 vs 矩阵哪个更准"这个实验**还没做**。
- `intension` 目前只是字符串列表，**不能机检**；要能反驳得先拆成必须/不许，那需要 fidelity 曲线先落地。

## 7. 什么时候应该停下来问人

- 需要引入核心链路的新依赖。
- 需要把"音乐决定"自动化（自动降八度、自动补和弦、自动选承载者）——C14/C16 都不许。
- 需要新增文档（README + 本文件之外）。
- 改动会让上面三个 MD5 变（除非这次就是要改那三首谱）。
