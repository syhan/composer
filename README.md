# Composer

一个通过自然语言描述驱动 MuseScore 进行编曲的 AI 辅助工具。面向有固定乐队编制的音乐人，在排练前快速验证编曲想法。

## 项目定位

**不是通用 MuseScore SDK**，而是一个**编曲草稿验证工具**。

核心流程：
```
脑中的编曲想法 → 自然语言/简谱描述 → AI 翻译为乐谱操作 → MuseScore 实时渲染 → 试听 → 迭代调整
```

### 解决的问题

- 乐队排练前，想快速验证一个编曲 idea 是否可行
- 不想手动在 MuseScore 里一个音符一个音符点
- 需要同时听到多乐器合奏的效果
- 迭代要快："贝斯太密了，换成跟着 kick 走的四分音符" → 立刻听到变化

### 目标用户画像

有自己乐队的音乐人，乐队编制包含：鼓、长号、原声吉他、电吉他、小提琴、贝斯、电吹管等。需要在排练前把脑中的想法落地成可听的多轨 demo。

---

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    MuseScore Studio 4                     │
│  ┌───────────────────────────────────────────────────┐  │
│  │  WebSocket Server Plugin (QML)                     │  │
│  │  - 监听 ws://localhost:8765                        │  │
│  │  - 接收 JSON 命令                                  │  │
│  │  - 调用 MuseScore Plugin API                       │  │
│  │  - 支持: addNote, addRest, addLyrics, setTempo,   │  │
│  │    addInstrument, clearScore, undo, navigation...  │  │
│  └───────────────────────────────────────────────────┘  │
└──────────────────────────┬──────────────────────────────┘
                           │ WebSocket (JSON messages)
                           │
┌──────────────────────────┴──────────────────────────────┐
│                     Python 层                             │
│                                                          │
│  Layer 1: WebSocket Client                               │
│  - 连接管理、自动重连                                     │
│  - JSON 消息收发                                         │
│                                                          │
│  Layer 2: 编曲语义 API (未来)                             │
│  - score.violin.play("C4:q D4:q E4:h")                  │
│  - score.guitar.arp("Am", pattern="1-3-5-3")            │
│  - score.bass.root_motion("Am F C G", rhythm="half")    │
│  - score.drums.pattern("ballad")                        │
│                                                          │
│  Layer 3: CLI (未来)                                     │
│  - $ composer connect                                    │
│  - $ composer clear                                      │
│  - $ composer write "..." --staff violin                 │
│                                                          │
│  Layer 4: AI Agent (当前 - Claude 直接驱动)              │
│  - 自然语言 → Python 脚本 → WebSocket → MuseScore       │
│  - "给我编一个送别的曲谱，小提琴主旋律，吉他分解"         │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### 当前状态（已验证可用）

- MuseScore 端 QML 插件已部署，WebSocket 通信已通
- Python 通过 websockets 库直接发送命令
- Claude Code 作为 AI 大脑，将用户描述翻译为 Python 脚本执行
- 已成功实现：多轨写入、歌词添加、乐器配置、速度设置、清空乐谱

---

## 技术细节

### MuseScore 插件 (QML)

文件位置：`~/Documents/MuseScore4/Plugins/musescore-mcp-websocket.qml`

基于 [ghchen99/mcp-musescore](https://github.com/ghchen99/mcp-musescore) 项目修改。

#### 支持的命令

| 命令 | 参数 | 说明 |
|------|------|------|
| `ping` | - | 连接测试 |
| `getScore` | - | 获取乐谱完整信息（staff/measure/元素） |
| `goToBeginningOfScore` | - | 光标回到开头 |
| `nextStaff` / `prevStaff` | - | 切换 staff |
| `nextElement` / `prevElement` | numElements? | 光标移动 |
| `goToMeasure` | measure | 跳转到指定小节 |
| `addNote` | pitch, duration, advanceCursorAfterAction | 写入音符 |
| `addRest` | duration, advanceCursorAfterAction | 写入休止符 |
| `addTuplet` | ratio, duration, advanceCursorAfterAction | 写入连音 |
| `addLyrics` | lyrics[], verse? | 批量添加歌词 |
| `addInstrument` | instrumentId | 添加乐器轨道 |
| `setTempo` | bpm | 设置速度 |
| `setTimeSignature` | numerator, denominator | 设置拍号 |
| `appendMeasure` | count? | 追加小节 |
| `insertMeasure` | - | 插入小节 |
| `deleteSelection` | measure? | 删除选区 |
| `selectCustomRange` | startTick, endTick, startStaff, endStaff | 选择范围 |
| `clearScore` | - | 全选删除，一键清空所有音符 |
| `undo` | - | 撤销 |
| `processSequence` | sequence[] | 批量执行命令序列 |

#### 音符参数格式

- **pitch**: MIDI 音高数值 (60 = C4 中央 C)
- **duration**: `{"numerator": 分子, "denominator": 分母}`
  - 全音符: `{1, 1}`, 二分: `{1, 2}`, 四分: `{1, 4}`, 八分: `{1, 8}`
  - 附点四分: `{3, 8}`, 附点二分: `{3, 4}`

#### MIDI 音高参考

```
C2=36  D2=38  E2=40  F2=41  G2=43  A2=45  B2=47
C3=48  D3=50  E3=52  F3=53  G3=55  A3=57  B3=59
C4=60  D4=62  E4=64  F4=65  G4=67  A4=69  B4=71
C5=72  D5=74  E5=76  F5=77  G5=79  A5=81  B5=83
```

升降半音: C#/Db = +1, D#/Eb = +1, F#/Gb = +1, G#/Ab = +1, A#/Bb = +1

### 乐队模板

文件位置：`~/Documents/MuseScore4/Scores/band_template.mscx`

预配置 5 轨乐队编制（MSCX 格式），确保 staff 顺序固定：

| Staff Index | 乐器 | MIDI Program | 谱号 |
|-------------|------|-------------|------|
| 0 | Violin | 40 (Violin) | 高音谱 |
| 1 | Acoustic Guitar | 25 (Acoustic Guitar) | 高音谱 |
| 2 | Electric Bass | 33 (Electric Bass) | 低音谱 |
| 3 | Trombone | 57 (Trombone) | 低音谱 |
| 4 | Drum Kit | 0 (Channel 10) | 打击乐谱 |

**为什么需要模板？** 通过 `addInstrument` API 动态添加乐器后，staff 的实际索引顺序不可预测。使用预制模板可以保证 staff 编号始终与乐器对应关系一致。

### 工作流

1. 打开 MuseScore → 加载 `band_template.mscx`
2. Plugins 菜单 → 运行 `musescore-mcp-websocket`
3. 在 Claude Code 中描述编曲需求
4. Claude 生成 Python 脚本通过 WebSocket 写入
5. MuseScore 实时显示，按空格试听
6. 不满意 → 描述修改 → `clearScore` + 重新写入
7. 满意 → File → Save / Export 分谱

---

## 已知问题与限制

### 已解决

| 问题 | 解决方案 |
|------|---------|
| undo 有上限，无法彻底回退 | 添加 `clearScore` 命令（select-all + delete） |
| `addNote` 报错 `elements.some is not a function` | 修复插件中 elements 类型判断逻辑 |
| `addInstrument` 后 staff 顺序不确定 | 使用预制 MSCX 模板 |
| 每次打开新文件需要重新运行插件 | 已知行为，需手动 Plugins → 运行 |

### 待解决

| 问题 | 状态 | 备注 |
|------|------|------|
| 除鼓以外的乐器可能没有声音 | 待排查 | 可能是模板 MSCX 中乐器定义不完整，MuseScore 未加载对应 SoundFont |
| 鼓谱的 MIDI pitch 映射 | 部分验证 | kick=36, snare=38, hihat=42 可用，其他鼓件待测试 |
| 不支持和弦（同时多个音） | 待实现 | `addNote` 的 `addToChord=true` 参数可以叠加音符 |
| 不支持连线(tie)、连音线(slur) | 待实现 | 需要扩展插件命令 |
| 不支持力度/表情记号 | 待实现 | 需要扩展插件命令 |
| 不支持调号/升降号 | 部分支持 | 可通过 pitch 的半音偏移实现，但不会显示调号 |
| 音频采样输入 | 未开始 | 远期目标，需要音频转录（basic-pitch / whisper） |

---

## 未来规划

### Phase 1: 稳定基础能力（当前）

- [x] WebSocket 通信打通
- [x] 多轨音符写入
- [x] 歌词添加
- [x] 乐队模板
- [x] clearScore 一键清空
- [ ] 修复声音输出问题
- [ ] 支持和弦写入（同时多个音）
- [ ] 支持更多乐器模板（可扩展编制）

### Phase 2: MIDI 键盘双向交互

**核心价值：你只管弹你最擅长的乐器，AI 帮你把其他声部补齐。**

```
MIDI 键盘 → MuseScore (录入旋律)
                ↓
       工具读取已录入的音符 (getScore / selectCustomRange)
                ↓
       AI 分析: 调性、和弦、节奏 pattern
                ↓
       自动编排其他声部 → 写入 MuseScore
                ↓
       用户试听合奏效果 → 迭代
```

#### 使用场景

1. **弹一段旋律，AI 自动配和声**
   - 你在 Violin 轨用 MIDI 键盘弹入主旋律
   - 告诉 AI："根据我弹的，帮我编排其他声部"
   - AI 读取你的旋律 → 推断和弦 → 生成吉他/贝斯/鼓

2. **弹一段和弦进行，AI 编写旋律**
   - 你在吉他轨弹入和弦
   - AI 根据和弦进行生成一段旋律给小提琴

3. **监听模式（高级）**
   - 工具轮询检测乐谱变化
   - 你每弹完一个乐句，其他声部自动跟上

#### 技术可行性

- ✅ MuseScore MIDI input 已内置，无需额外开发
- ✅ `getScore` API 可以读取任何轨道上的音符（含 pitch、duration、tick 位置）
- ✅ `selectCustomRange` 可以精确读取指定范围的内容
- 🔲 需要实现：差异检测（对比前后状态，识别新增音符）
- 🔲 需要实现：和弦/调性推断算法（或直接用 AI 分析）

### Phase 3: 编曲语义层

封装高级 API，将乐理概念映射为底层操作：

```python
# 和弦进行 + 节奏 pattern
score.guitar.arp("Am F C G", pattern="1-3-5-3", rhythm="eighth", measures=4)
score.bass.root_motion("Am F C G", rhythm="half", measures=4)
score.drums.pattern("8beat", measures=4)

# 旋律输入（字符串语法）
score.violin.play("A4:q C5:q B4:q A4:q | E4:q G4:q A4:h")

# 和弦标记
score.set_chords("Am | F | C | G7")
```

### Phase 4: CLI 工具

```bash
composer connect              # 连接 MuseScore
composer clear                # 清空乐谱
composer template my-band     # 加载乐队模板
composer write violin "C4:q D4:q E4:h"
composer chord "Am F C G" --measures 4
composer tempo 120
composer play                 # 触发播放
```

### Phase 5: MCP Server 集成

将编曲能力封装为 MCP Server，接入 Claude Desktop 等 AI 客户端：

```json
{
  "mcpServers": {
    "composer": {
      "command": "python",
      "args": ["composer/mcp_server.py"]
    }
  }
}
```

### Phase 6: 音频输入

- 哼唱 → 音高检测 → 旋律转录
- 参考录音 → 节奏/和弦提取 → 基于此编排其他声部
- 乐器演奏片段 → 识别和弦进行 → 自动配器

---

## 相关项目

| 项目 | 关系 |
|------|------|
| [ghchen99/mcp-musescore](https://github.com/ghchen99/mcp-musescore) (62⭐) | QML 插件基础，我们的插件基于此修改 |
| [JordanSucher/musescore-mcp](https://github.com/JordanSucher/musescore-mcp) (25⭐) | 类似思路的简化版 |
| [Josef-Friedrich/mscxyz](https://github.com/Josef-Friedrich/mscxyz) (35⭐) | Python 操作 MSCX 文件（文件层面，非实时） |
| [johentsch/ms3](https://github.com/johentsch/ms3) (56⭐) | Python 解析 MuseScore 文件为 DataFrame |
| [music21](https://github.com/cuthbertLab/music21) (2000+⭐) | MIT 计算音乐学库，可辅助乐理计算 |

---

## 开发环境

```bash
cd ~/Workspace/syhan/Composer
source .venv/bin/activate

# 依赖
pip install websockets

# 测试连接
python -c "
import asyncio, websockets, json
async def t():
    async with websockets.connect('ws://localhost:8765') as ws:
        await ws.send(json.dumps({'action':'ping','params':{}}))
        print(await ws.recv())
asyncio.run(t())
"
```

### 前置条件

- MuseScore Studio 4.x 已安装
- QML 插件已复制到 `~/Documents/MuseScore4/Plugins/`
- Python 3.10+ with `websockets` 包
