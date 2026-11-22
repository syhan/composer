#!/usr/bin/env python3
"""spec -> WAV。内环的音频出口。

两种引擎：

  gm      fluidsynth + SF2。音质够判断，整曲 0.9 秒。
  synth   零依赖 numpy。糙，但永远能跑（gm 坏掉时它还在）。

⚠️ fluidsynth 会卡在音频设备发现上，而渲染到文件并不需要音频设备。

   它启动时先连 PipeWire/Pulse 的套接字、再枚举 ALSA 设备，**都在解析 `-a file` 之前**。
   只要音频栈不健康，它就无限等，而且 stdout 是块缓冲——看起来像"没有任何输出"。

   病因与症状：这台机器的 PipeWire 曾整个卡死（`pw-play` / `pactl` / `pw-cli` 全部不退出），
   当时不隔离跑 10 次挂 2 次。重启 `pipewire` + `wireplumber` 后，不隔离也 6/6 正常。

   但**隔离仍然保留**：渲染不应该依赖音频服务是否健康（C2）。你正在开会、正在放音乐、
   或者服务又卡了，都不该影响内环能不能出声。隔离手段：

     XDG_RUNTIME_DIR / PIPEWIRE_RUNTIME_DIR / PULSE_RUNTIME_PATH → 空目录
     PULSE_SERVER / DBUS_SESSION_BUS_ADDRESS → 不存在
     ALSA_CONFIG_PATH=/dev/null                      → 不枚举任何声卡

   隔离后：4 小节 80ms，整曲 0.9s，30/30 成功。

⚠️ fluidsynth 的 -F 是**追加**不是覆盖。所以永远写到一个新文件再改名。
   （这个坑吃过一次：两次渲染拼在一起，每个小节的测量值全错，而看起来像音乐问题。）

⚠️ 外部进程一律带 timeout。没有超时的那一次，一条永不返回的命令吃掉了整整一小时。
"""
import os
import shutil
import subprocess
import sys
import tempfile

SF2 = "/usr/share/soundfonts/FluidR3_GM.sf2"


def render_midi(spec_path, mid_path):
    """跑一遍和 render.py 相同的管线，只留 MIDI。"""
    import yaml
    import render
    spec = yaml.safe_load(open(spec_path))
    _ens, insts = render.load_ensemble(spec, spec_path)
    bars, parts, _tracks, _warns = render.expand(spec, insts)
    bars = render.autoplace(bars, parts, spec["meta"]["grid"])
    bars, _issues = render.apply_presence(spec, bars)
    open(mid_path, "wb").write(render.build_midi(spec, bars, parts))
    return mid_path


def _isolated_env(rt):
    """把 fluidsynth 和音频栈隔开：渲染不该依赖音频服务是否健康。"""
    return dict(
        os.environ,
        XDG_RUNTIME_DIR=rt,
        PIPEWIRE_RUNTIME_DIR=rt,
        PULSE_RUNTIME_PATH=rt,
        PULSE_SERVER="/nonexistent",
        DBUS_SESSION_BUS_ADDRESS="/nonexistent",
        ALSA_CONFIG_PATH="/dev/null",
    )


def render_gm(mid_path, out_path, sf2=SF2, timeout=30, attempts=2):
    fs = shutil.which("fluidsynth")
    if not fs:
        raise RuntimeError("没有 fluidsynth —— 用 --engine synth")
    if not os.path.exists(sf2):
        raise RuntimeError(f"没有音源 {sf2} —— 用 --engine synth")

    rt = tempfile.mkdtemp(prefix="fsrt-")
    os.chmod(rt, 0o700)
    tmp = out_path + ".part"
    try:
        for p in (tmp, out_path):
            if os.path.exists(p):
                os.remove(p)
        last = None
        for _ in range(attempts):
            try:
                r = subprocess.run(
                    [fs, "-a", "file", "-O", "s16", "-F", tmp, sf2, mid_path],
                    env=_isolated_env(rt), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                last = f"超时 {timeout}s"
                if os.path.exists(tmp):
                    os.remove(tmp)
                continue
            if r.returncode != 0:
                last = f"rc={r.returncode}: {r.stdout[-300:]!r}"
                continue
            if not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
                last = "没有写出有效音频"
                continue
            os.replace(tmp, out_path)
            return out_path
        raise RuntimeError(last or "未知失败")
    finally:
        shutil.rmtree(rt, ignore_errors=True)
        if os.path.exists(tmp):
            os.remove(tmp)


def render_synth(spec_path, out_path):
    import synth
    return synth.render(spec_path, out_path)


def render(spec_path, out_path, engine="gm"):
    """返回 (wav 路径, 实际用的引擎)。gm 失败时如实回落并说明。"""
    if engine == "gm":
        mid = out_path + ".mid"
        render_midi(spec_path, mid)
        try:
            return render_gm(mid, out_path), "gm"
        except Exception as e:                       # 不假装成功
            print(f"  ! gm 引擎不可用（{e}），回落到 synth", file=sys.stderr)
    return render_synth(spec_path, out_path), "synth"


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        raise SystemExit("用法: audio.py spec.yaml -o out.wav [--engine gm|synth]")
    spec_path = args[0]
    out = "out.wav"
    engine = "gm"
    for i, a in enumerate(args):
        if a in ("-o", "--out") and i + 1 < len(args):
            out = args[i + 1]
        if a == "--engine" and i + 1 < len(args):
            engine = args[i + 1]
    path, used = render(spec_path, out, engine)
    size = os.path.getsize(path)
    print(f"wrote {path}  ({size/1024:.0f} KB, engine={used})")


if __name__ == "__main__":
    main()
