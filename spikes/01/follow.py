#!/usr/bin/env python3
"""follow.py —— 走带 + 热力图。内环的"我在哪、现在响的是谁"。

为什么不是又一个播放器：**MIDI 播放器不能 seek。** fluidsynth 的交互 shell 只有
`load / play / pause / resume / stop`，没有 seek，只能从头重放；mpg123 有 `f`/`b`
但我们控制不了它跳到哪，而且 LAME 的编码器延迟（≈576 采样）会让"小节号 → 时间"
整体平移、**安静地错**。

所以走带的实现是：**整曲渲染一次成 numpy 数组，走带 = 数组下标。**
帧精确，前进后退一样快，不生成中间文件，显示位置就是音频位置。

热力图一行一个座位，一列一段**时长**（不是"一格一小节"——7/8 和 4/4 一样宽就是错的）。
灯只表示"谱面在这一小节有这个声部的音"，**不代表混音里可闻**：GM 的长号在 B2 没有
可用基频，规格上是低音、听起来是高音（踩过一次）。标题栏写死这句。

C19：视图只显示谱面之外的假设。MuseScore 能显示的最多作坐标系——
所以这里没有音量、没有音符、没有导出。只有：谁在场、谁进来、谁走、我在哪。

不能交互时（没有 tty、或者在脚本里）用两个自测入口，它们走的是同一套状态机：
    follow.py spec.yaml --draw                      打一帧就退
    follow.py spec.yaml --keys "fast-right,fast-right,s,draw"
"""

import bisect
import contextlib
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np

import audio as audio_mod
import render

LIGHT = "█"  # 这一小节这个声部有音
DARK = "░"  # 没有
LABEL = 11  # 行首宽度：1 个标记 + 9 个名字 + 1 个空格


# ---------- 读谱 ----------


def load(path):
    spec = render.load_yaml(path, "spec")
    _ens, insts = render.load_ensemble(spec, path)
    meter = render.meter_of(spec)
    bars, parts, _tracks, _warns = render.expand(spec, insts, meter)
    bars = render.autoplace(bars, parts)
    bars, _issues = render.apply_presence(spec, bars)
    return {"path": path, "spec": spec, "meter": meter, "bars": bars, "parts": parts}


def focus(bars, solo, mute):
    """solo/mute 切**乐谱**（音频没法反混音）。和 audition.py 一致。"""
    return {
        b: [(p, *r) for (p, *r) in evs if (not solo or p["name"] in solo) and p["name"] not in mute]
        for b, evs in bars.items()
    }


# ---------- 布局：宽度 ∝ 时长 ----------


def layout(meter, n, cols):
    """每小节占多少列。

    **宽度按小节时长分，不按小节数均分。** 一行 40 格、每格一小节，在 4/4 里
    勉强成立；一旦有 7/8，"一格 = 一小节"就把短小节拉长了——图和声音对不上，
    而且不报错。所以先给每小节 1 列保底，剩下的按 tick 数用最大余数法分。
    """
    cols = max(cols, n)  # 每小节至少一格，否则会静默丢小节
    ticks = [meter[b].ticks for b in range(1, n + 1)]
    total = sum(ticks)
    extra = cols - n
    raw = [extra * t / total for t in ticks]
    add = [int(r) for r in raw]
    order = sorted(range(n), key=lambda i: add[i] - raw[i])  # 余数大的先补
    for i in order[: extra - sum(add)]:
        add[i] += 1
    pos, c = {}, 0
    for i in range(n):
        pos[i + 1] = (c, 1 + add[i])
        c += 1 + add[i]
    return pos, c


# ---------- 一帧 ----------


def frame(v, st, cols):
    meter, n = v["meter"], len(v["spec"]["harmony"])
    pos, total = layout(meter, n, cols)
    seats = [p["name"] for p in v["parts"]]
    on = {b: render._sounding(v["bars"], meter, b) for b in range(1, n + 1)}
    bar = st["bar"]

    out = []
    title = v["spec"]["meta"].get("title", os.path.basename(v["path"]))
    seen, meters = set(), []
    for b in range(1, n + 1):
        m = f"{meter[b].beats}/{meter[b].unit}"
        if m not in seen:
            seen.add(m)
            meters.append(m)
    out.append(f"  {title}   {meter.tempo} BPM   {n} 小节   {' / '.join(meters)}")
    out.append("  灯 = 谱面有此音，不代表混音里可闻")

    # 小节号：能放下才放，而且左右各留一列空（不留就会出「2324」这种
    # 分不清是两小节还是一个数的东西）。放不下的跳过，不截断。
    ruler = [" "] * total
    last_end = -1
    for b in range(1, n + 1):
        s = pos[b][0]
        t = str(b)
        if s <= last_end or s + len(t) >= total:
            continue
        ruler[s : s + len(t)] = list(t)
        last_end = s + len(t)
    out.append(" " * LABEL + "".join(ruler))

    cur = [" "] * total
    cur[pos[bar][0]] = "▲"
    out.append(" " * LABEL + "".join(cur))

    for i, name in enumerate(seats):
        mark = "S" if name in st["solo"] else ("M" if name in st["mute"] else " ")
        cells = []
        for b in range(1, n + 1):
            s, w = pos[b]
            lit = name in on[b]
            if name in st["mute"]:
                ch = "╌"
            elif st["solo"] and name not in st["solo"]:
                ch = "·"
            else:
                ch = LIGHT if lit else DARK
            cells.append(ch * w)
        lead = "▸" if i == st["sel"] else " "
        out.append(f"{lead}{mark}{name[:9]:<9} " + "".join(cells))

    # A/B：整条走带就是选择器
    ab = [" "] * total
    for key, ch in (("a", "["), ("b", "]")):
        if st[key]:
            ab[pos[st[key]][0]] = ch
    if st["loop"] and st["a"] and st["b"]:
        for c in range(pos[min(st["a"], st["b"])][0], pos[max(st["a"], st["b"])][0]):
            if ab[c] == " ":
                ab[c] = "─"
    out.append(" " * LABEL + "".join(ab))

    # 接下来谁进谁走——这条 MuseScore 显示不了，所以它是这个视图的理由
    if bar < n:
        enter = sorted(on[bar + 1] - on[bar])
        leave = sorted(on[bar] - on[bar + 1])
        if enter or leave:
            bits = [f"+{x}" for x in enter] + [f"-{x}" for x in leave]
            out.append(f"  接下来 bar {bar + 1}:  {' '.join(bits)}")

    secs = [meter.secs(b) for b in range(1, n + 1)]
    t = st["t"]
    tot = meter.total_secs()
    mark = "▶" if st["playing"] else "‖"
    ab_txt = f"A {st['a'] or '-'}  B {st['b'] or '-'}  loop {'on' if st['loop'] else 'off'}"
    out.append(
        f"  {mark} bar {bar}/{n}   {int(t // 60)}:{int(t % 60):02d}"
        f" / {int(tot // 60)}:{int(tot % 60):02d}   {ab_txt}"
    )
    out.append(
        "  [space] 停/续  [←→] 小节  [fast] 四小节  [,/.] 一拍  "
        "[ ][ ] A/B  [l] 循环  [↑↓] 选座位  [s] solo  [m] mute  [g] 清  [q] 退出"
    )
    if st.get("note"):
        out.append(f"  \x1b[31m{st['note']}\x1b[0m")
    return out, secs


# ---------- 播放：整曲一次渲成数组，走带 = 下标 ----------

PLAYERS = (
    # 每个播放器的"原始 PCM"开关和格式名都不一样，别想着统一：
    #   pw-play 默认把参数当**文件**交给 libsndfile，喂 stdin 必须加 --raw，
    #           否则它在 open 上就失败；而格式名是 s16 不是 s16le。
    #   paplay 的格式名才是 s16le。
    # 写错一个字节的表现是"按空格没声音"——静默、不报错。
    #
    # 不列 aplay：它在 PipeWire 的 ALSA 插件下会**无限阻塞**（`-` 读 stdin 读到 EOF
    # 也不退），而阻塞和正在播放从外面看不出区别。宁可少一个备选，不要一个会挂住的备选。
    (["pw-play", "--raw"], ["--format=s16", "--channels={ch}", "--rate={sr}", "-"]),
    (["paplay", "--raw"], ["--format=s16le", "--channels={ch}", "--rate={sr}"]),
)

# 方向键和 shift+方向键。终端送的是多字节转义序列，所以得先攒齐再查表。
KEYMAP = {
    " ": "space",
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
    "\x1b[1;2C": "fast-right",
    "\x1b[1;2D": "fast-left",
}
KEY_SEQS = frozenset(KEYMAP)


class Player:
    """往播放器的 stdin 喂 PCM。seek = 停掉进程，从新位置重新起一个。

    不做混音、不做重采样：喂的就是渲染出来的那份采样，所以"显示位置 = 音频位置"。
    """

    def __init__(self, x, sr):
        self.x, self.sr = x, sr
        self.proc = None
        self.thread = None
        self.fed = 0.0
        self.started = 0.0
        self.t0 = 0.0
        self.lock = threading.Lock()

    def _spawn(self):
        ch = self.x.shape[1]
        tried = []
        for cmd, tail in PLAYERS:
            exe = shutil.which(cmd[0])
            if not exe:
                tried.append(f"{cmd[0]}: 没装")
                continue
            args = [exe, *cmd[1:], *(a.format(ch=ch, sr=self.sr) for a in tail)]
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            # 起不来的播放器（参数写错、音频服务不健康）会**立刻**退出。
            # 不查的话，它安静地死掉，人只看到"按了没反应"——这正是刚才那次。
            # 进程已退出时 stderr 已到 EOF，read 不会阻塞。
            time.sleep(0.12)
            if proc.poll() is not None:
                msg = ""
                if proc.stderr is not None:
                    msg = proc.stderr.read().decode("utf-8", "replace").strip()
                first = msg.splitlines()[0] if msg else f"退出码 {proc.returncode}"
                tried.append(f"{cmd[0]}: {first}")
                continue
            return proc
        raise RuntimeError("没有能用的播放器：\n  " + "\n  ".join(tried))

    def start(self, t):
        self.stop()
        self.fed = t
        self.started = t
        self.proc = self._spawn()
        # t0 要在 spawn 之后才取：_spawn 里有 120ms 的“看它死没死”的等待，
        # 算进去的话进度条一开始就比声音快 0.12 秒。
        self.t0 = time.monotonic()
        self.thread = threading.Thread(target=self._feed, args=(t,), daemon=True)
        self.thread.start()

    def _feed(self, t):
        i = max(0, int(t * self.sr))
        buf = self.x[i:]
        step = max(1, self.sr // 20)  # 50ms 一块：够细，又不至于忙等
        proc = self.proc
        stdin = proc.stdin if proc is not None else None
        if stdin is None:
            return
        try:
            for k in range(0, len(buf), step):
                if proc is None or proc.poll() is not None:
                    return
                stdin.write(buf[k : k + step].astype("<i2").tobytes())
                with self.lock:
                    self.fed = (i + k) / self.sr
        except (BrokenPipeError, ValueError, OSError):
            return  # 播放器被关掉是正常操作
        finally:
            with contextlib.suppress(OSError):
                stdin.close()

    def stop(self):
        p, self.proc = self.proc, None
        self.thread = None
        if p is not None:
            with contextlib.suppress(OSError):
                p.kill()

    def pos(self):
        """播放位置：墙钟和“已经喂进去多少”取小的那个。

        只看喂进去多少会**跑在前面**：pw-play 一上来就把管道抽干（它自己还有
        100ms 的缓冲），2 秒的音频 0.4 秒就喂完了，于是进度条冲到终点而声音还在放。
        只看墙钟又会撒谎：管道堵住、播放器卡死时时间照走。
        取 min 两个毛病都没有：平时跟墙钟，卡住时停在已喂到的位置。
        """
        with self.lock:
            if self.proc is None:
                return self.fed
            clock = self.started + (time.monotonic() - self.t0)
            return min(clock, self.fed)


# ---------- 状态机 ----------


def make_state(v):
    return {
        "t": 0.0,
        "bar": 1,
        "playing": False,
        "a": None,
        "b": None,
        "loop": False,
        "sel": 0,
        "solo": set(),
        "mute": set(),
    }


def bar_at(secs, t):
    return min(len(secs), bisect.bisect_right(secs, t))


def beat_secs(meter, b):
    return meter.bar_secs(b) / meter[b].beats


def step(v, st, key, secs, player, engine):
    """执行一个键。返回 True 表示退出。

    `--keys` 走的就是这个函数，所以自测覆盖的是真的那条路径。
    """
    meter, n = v["meter"], len(secs)
    bar = bar_at(secs, st["t"])
    if key == "right":
        st["t"] = secs[bar] if bar < n else secs[-1]
    elif key == "left":
        st["t"] = secs[max(0, bar - 2)]
    elif key == "fast-right":
        st["t"] = secs[min(n - 1, bar + 3)]
    elif key == "fast-left":
        st["t"] = secs[max(0, bar - 5)]
    elif key == "beat-right":
        st["t"] = min(meter.total_secs(), st["t"] + beat_secs(meter, bar))
    elif key == "beat-left":
        st["t"] = max(0.0, st["t"] - beat_secs(meter, bar))
    elif key in ("[", "]"):
        st["a" if key == "[" else "b"] = bar
    elif key == "l":
        st["loop"] = not st["loop"]
    elif key == "up":
        st["sel"] = (st["sel"] - 1) % len(v["parts"])
    elif key == "down":
        st["sel"] = (st["sel"] + 1) % len(v["parts"])
    elif key in ("s", "m"):
        name = v["parts"][st["sel"]]["name"]
        box = st["solo"] if key == "s" else st["mute"]
        (box.discard if name in box else box.add)(name)
        apply_audio(v, st, engine, player)
    elif key == "g":
        st["solo"], st["mute"] = set(), set()
        apply_audio(v, st, engine, player)
    elif key == "space":
        st["playing"] = not st["playing"]
    elif key == "q":
        return True
    st["note"] = ""
    if player is not None:
        try:
            if st["playing"]:
                player.start(st["t"])
            else:
                player.stop()
        except RuntimeError as e:
            # 声音出不来就当场说出来，不要留在“按了没反应”的状态里。
            st["playing"] = False
            st["note"] = str(e).replace("\n", "\n  ")
            player.stop()
    st["bar"] = bar_at(secs, st["t"])
    return False


def apply_audio(v, st, engine, player):
    """solo/mute 必须真的改声音，否则就是"看得见、没效果"（P8）。

    改完要把**播放器的缓冲区也换掉**。否则声音已经重渲了，走带还在放旧的那份——
    而且灯是对的，耳朵是错的，两边都很自信。
    """
    if engine == "none":
        raise SystemExit("--no-audio 时 solo/mute 没有意义：它改的是声音，不是灯")
    bars = focus(v["bars"], st["solo"], st["mute"])
    mid = os.path.join(v["tmpdir"], "follow.mid")
    wav = os.path.join(v["tmpdir"], "follow.wav")
    try:
        with open(mid, "wb") as f:
            f.write(render.build_midi(v["spec"], bars, v["parts"], v["meter"]))
    except OSError as e:
        raise SystemExit(f"写不出 MIDI: {mid}\n{e}") from e
    if engine == "gm":
        try:
            audio_mod.render_gm(mid, wav)
        except audio_mod.GM_FAILURES as e:
            raise SystemExit(f"gm 不可用（{e}），solo/mute 改不了声音——不假装改过了") from e
    else:
        raise SystemExit("synth 引擎不支持 solo/mute（它按整谱渲染）")
    x, sr = read_wav(wav)
    v["x"], v["sr"] = x, sr
    if player is not None:
        player.stop()
        player.x, player.sr = x, sr


def read_wav(path):
    with wave.open(path) as w:
        n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
        x = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, ch)
    return x, sr


# ---------- 主 ----------


def parse_args(argv):
    o = {
        "spec": "farewell-v3.yaml",
        "cols": 0,
        "engine": "gm",
        "keys": "",
        "draw": False,
        "loop": 1,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--draw":
            o["draw"] = True
        elif a == "--no-audio":
            o["engine"] = "none"
        elif a == "--cols" and i + 1 < len(argv):
            i += 1
            o["cols"] = int(argv[i])
        elif a == "--keys" and i + 1 < len(argv):
            i += 1
            o["keys"] = argv[i]
        elif a in ("-h", "--help"):
            raise SystemExit(__doc__)
        elif a.startswith("-"):
            raise SystemExit(f"不认识的参数 {a}")
        else:
            o["spec"] = a
        i += 1
    return o


def main():
    o = parse_args(sys.argv[1:])
    v = load(o["spec"])
    v["tmpdir"] = tempfile.mkdtemp(prefix="follow-")
    st = make_state(v)
    _lines, secs = frame(v, st, o["cols"] or shutil.get_terminal_size((100, 30)).columns - 2)
    cols = o["cols"] or shutil.get_terminal_size((100, 30)).columns - 2

    player = None
    if o["engine"] != "none":
        wav = os.path.join(v["tmpdir"], "whole.wav")
        audio_mod.render(v["path"], wav, engine=o["engine"])
        v["x"], v["sr"] = read_wav(wav)
        player = Player(v["x"], v["sr"])

    if o["keys"]:
        for key in [k.strip() for k in o["keys"].split(",") if k.strip()]:
            if step(v, st, key, secs, player, o["engine"]):
                break
            lines, secs = frame(v, st, cols)
            print("\n".join(lines))
            print()
        if player is not None:
            player.stop()
        return

    if o["draw"] or not sys.stdin.isatty():
        lines, _ = frame(v, st, cols)
        print("\n".join(lines))
        if not o["draw"]:
            print("\n  （没有 tty，只打一帧。用 --keys 走状态机，或者 --draw 指定小节。）")
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        dirty = True
        while True:
            if dirty:
                lines, secs = frame(v, st, cols)
                sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + "\n")
                sys.stdout.flush()
                dirty = False
            if player is not None and st["playing"]:
                st["t"] = player.pos()
                if st["loop"] and st["a"] and st["b"] and st["t"] >= secs[st["b"]] - 0.01:
                    st["t"] = secs[st["a"] - 1]
                    player.start(st["t"])
                if st["t"] >= secs[-1]:
                    st["playing"] = False
                    player.stop()
                dirty = True
            r, _w, _x = select.select([sys.stdin], [], [], 0.1)
            if not r:
                continue
            key = KEYMAP.get(_read_key(fd), "")
            if not key:
                continue
            if key in ("q", "\x03"):
                break
            if step(v, st, key, secs, player, o["engine"]):
                break
            dirty = True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if player is not None:
            player.stop()
        shutil.rmtree(v["tmpdir"], ignore_errors=True)


def _read_key(fd):
    """读一个按键。

    方向键是 `\\x1b[A` 这样的多字节序列，而且**不保证一次读完**：终端会把
    `\\x1b` 和 `[C` 分两次送到。一次 `read(8)` 拿到半个序列，就会退化成
    “按了 → 没反应”或者“按了 → 出来一个 [ ”。所以先读一个字节，看到 ESC
    再用很短的超时把剩下的凑齐；凑不齐就是单独的 ESC。
    """
    ch = os.read(fd, 1).decode("utf-8", "ignore")
    if ch != "\x1b":
        return ch
    for _ in range(6):
        r, _w, _x = select.select([sys.stdin], [], [], 0.005)
        if not r:
            break
        ch += os.read(fd, 1).decode("utf-8", "ignore")
        if ch in KEY_SEQS:
            break
    return ch


if __name__ == "__main__":
    main()
