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

run "render --selftest" python3 render.py --selftest
run "render"            python3 render.py farewell-v3.yaml
run "synth"             python3 synth.py farewell-v3.yaml
run "lyrics"            python3 lyrics.py farewell-v3.yaml
run "relations"         python3 relations.py farewell-v3.yaml
run "audio gm"          python3 audio.py farewell-v3.yaml -o "$tmp/gm.wav" --engine gm
run "audio synth"       python3 audio.py farewell-v3.yaml -o "$tmp/sy.wav" --engine synth
run "measure"           python3 measure.py "$tmp/gm.wav" 112 24

# 音频必须真的有声音，而不是一个空壳文件
if [ -s "$tmp/gm.wav" ]; then
  d=$(python3 -c "
import wave,sys
w=wave.open('$tmp/gm.wav'); print(round(w.getnframes()/w.getframerate(),1))")
  echo "  · gm.wav 时长 ${d}s"
else
  echo "  ✗ gm.wav 不存在"; fail=1
fi

if [ $fail -eq 0 ]; then echo "全部通过"; else echo "有失败"; exit 1; fi
