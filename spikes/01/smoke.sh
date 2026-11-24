#!/usr/bin/env bash
# 所有入口跑一遍。任何一个非零退出就红。
#
# 为什么要有这个：阶段 A 改了 expand() 的返回值，render/lyrics/relations 都跟着改了，
# 唯独漏了 synth.py —— 它坏了三天没人知道，因为每次只跑 render.py。
#
# 用法: ./smoke.sh        正常跑
#      ./smoke.sh -v     失败时打印完整日志
set -u
cd "$(dirname "$0")"

# 解释器：本机 python3 有时被别的工具链遮住（例如 pi-lens 的 pip-tools venv，
# 里面没有 PyYAML/numpy），于是所有入口都会以 ModuleNotFoundError 失败。
# 所以这里挑一个真能 import 的，而不是盲信 PATH。
PY=""
for cand in python3 /usr/bin/python3 /usr/sbin/python3; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c "import yaml, numpy" >/dev/null 2>&1; then
    PY=$cand; break
  fi
done
if [ -z "$PY" ]; then
  echo "找不到带 PyYAML + numpy 的 python3。装一下：pip install pyyaml numpy" >&2
  exit 2
fi

VERBOSE=${1:-}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0

run() {
  local desc="$1"; shift
  local t0 t1
  t0=$(date +%s%N)
  if timeout 90 "$@" >"$tmp/log" 2>&1; then
    t1=$(date +%s%N)
    printf '  \033[32m✓\033[0m %-18s %5d ms\n' "$desc" $(( (t1 - t0) / 1000000 ))
  else
    t1=$(date +%s%N)
    printf '  \033[31m✗\033[0m %-18s %5d ms\n' "$desc" $(( (t1 - t0) / 1000000 ))
    if [ "$VERBOSE" = "-v" ]; then cat "$tmp/log"; else tail -4 "$tmp/log" | sed 's/^/      /'; fi
    fail=1
  fi
}

run "render --selftest" "$PY" render.py --selftest

# 每首谱跑一遍全部入口。加了新谱就往这里加一行——
# 一个入口只被一首谱覆盖过，等于没被覆盖（synth.py 就是这么坏掉的）。
suite() {
  local spec=$1 tempo=$2 nb=$3 rng=$4 solo=$5
  local t="[$spec]"
  run "render $t"    "$PY" render.py "$spec"
  run "synth $t"     "$PY" synth.py "$spec"
  run "lyrics $t"    "$PY" lyrics.py "$spec"
  run "relations $t" "$PY" relations.py "$spec"
  run "audio gm $t"  "$PY" audio.py "$spec" -o "$tmp/$spec-gm.wav" --engine gm
  run "audio sy $t"  "$PY" audio.py "$spec" -o "$tmp/$spec-sy.wav" --engine synth
  run "measure $t"   "$PY" measure.py "$tmp/$spec-gm.wav" "$tempo" "$nb"
  run "audition $t"  "$PY" audition.py "$spec" --bars "$rng" --loop 2 -o "$tmp/$spec-a1.wav"
  run "solo $t"      "$PY" audition.py "$spec" --bars "$rng" --solo "$solo" -o "$tmp/$spec-a2.wav"
  run "A/B $t"       "$PY" audition.py "$spec" "$spec" --bars "$rng" --diff -o "$tmp/$spec-a3.wav"
  run "probe $t"     "$PY" probe.py "$spec" --bars "$rng"
  run "probe-n $t"   "$PY" probe.py "$spec" --bars "$rng" --no-audio
}

suite farewell-v3.yaml      112 24 17-20 violin
# 蒸汽机版带 color: 段，probe 只渲染 21-24 —— 正好覆盖"只渲染后半段"这条路径
suite farewell-steampunk.yaml 92 32 21-24 theremin

# 音频必须真的有声音，而不是一个空壳文件
if [ -s "$tmp/farewell-v3.yaml-gm.wav" ]; then
  d=$("$PY" -c "
import wave,sys
w=wave.open('$tmp/farewell-v3.yaml-gm.wav'); print(round(w.getnframes()/w.getframerate(),1))")
  echo "  · v3 gm.wav 时长 ${d}s"
else
  echo "  ✗ gm.wav 不存在"; fail=1
fi

if [ $fail -eq 0 ]; then echo "全部通过"; else echo "有失败"; exit 1; fi
