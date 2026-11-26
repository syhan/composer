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
# 键位表本身要被测：上一版 resolve_key 把 s/m/l/q 全吞了，而界面照常刷新，
# 看起来就像"这些快捷键没什么用"。
run "follow --selftest" "$PY" follow.py --selftest
# 导入报告：只读、不写文件。它答的是"这个文件里有什么、哪些会丢"。
run "ingest --report" "$PY" ingest.py ref-tricky.musicxml
run "ingest --dump" "$PY" ingest.py ref-tricky.musicxml --dump violin

# 导入 -> 生成 spec -> 跑全套入口。写进临时 root，不污染仓库。
# 这条路径专门覆盖 `notes:`（绝对 tick 的素材），是唯一不走格子的入口。
imp="$tmp/imp"
mkdir -p "$imp"
run "import --as-spec" env INGEST_ROOT="$imp" "$PY" ingest.py \
    ref-tricky.musicxml --as-spec "$imp/tricky.yaml"

# 每首谱跑一遍全部入口。加了新谱就往这里加一行——
# 一个入口只被一首谱覆盖过，等于没被覆盖（synth.py 就是这么坏掉的）。
suite() {
  local spec=$1 rng=$2 solo=$3
  local lab; lab=$(basename "$spec")
  local t="[$lab]"
  run "render $t"    "$PY" render.py "$spec"
  run "synth $t"     "$PY" synth.py "$spec"
  run "lyrics $t"    "$PY" lyrics.py "$spec"
  run "relations $t" "$PY" relations.py "$spec"
  run "audio gm $t"  "$PY" audio.py "$spec" -o "$tmp/$lab-gm.wav" --engine gm
  run "audio sy $t"  "$PY" audio.py "$spec" -o "$tmp/$lab-sy.wav" --engine synth
  run "measure $t"   "$PY" measure.py "$tmp/$lab-gm.wav" "$spec"
  run "audition $t"  "$PY" audition.py "$spec" --bars "$rng" --loop 2 -o "$tmp/$lab-a1.wav"
  run "solo $t"      "$PY" audition.py "$spec" --bars "$rng" --solo "$solo" -o "$tmp/$lab-a2.wav"
  run "A/B $t"       "$PY" audition.py "$spec" "$spec" --bars "$rng" --diff -o "$tmp/$lab-a3.wav"
  run "probe $t"     "$PY" probe.py "$spec" --bars "$rng"
  run "probe-n $t"   "$PY" probe.py "$spec" --bars "$rng" --no-audio
  run "follow $t"    "$PY" follow.py "$spec" --no-audio --draw
  # --keys 走的就是交互模式用的那个 step()，所以这两行测的是真路径，
  # 不是另写一条测试专用的小路。
  run "follow-k $t"  "$PY" follow.py "$spec" --no-audio \
      --keys "fast-right,[,fast-right,],l,right,left,fast-left"
}

suite farewell-v3.yaml      17-20 violin
# 蒸汽机版带 color: 段，probe 只渲染 21-24 —— 正好覆盖"只渲染后半段"这条路径
suite farewell-steampunk.yaml 21-24 theremin
# 拍号自检谱：4/4 → 7/8 → 5/4。所有入口都得在**不等长小节**上跑一遍，
# 否则"小节边界"这类假设会安静地错下去（measure.py 曾经把 `4 * 60/tempo` 写死——
# 对 4/4 一直是对的，所以从来没人发现，直到第一首 7/8 出现）。
suite meter-selftest.yaml   5-8  theremin
# 导入出来的谱：唯一用 `notes:`（绝对 tick）而不是格子写成的谱。
suite "$imp/tricky.yaml"    1-2  violin
# 导入出来的谱：唯一用 `notes:`（绝对 tick）而不是格子写成的谱。
suite "$imp/tricky.yaml"    1-2  violin
# 导入出来的谱：唯一用 `notes:`（绝对 tick）而不是格子写成的谱。
suite "$imp/tricky.yaml"    1-2  violin

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
