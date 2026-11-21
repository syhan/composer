#!/usr/bin/env python3
"""spike 1: 把歌词对到旋律上（词层的第一次落地）。

不是为了渲染人声，而是为了回答一个问题：
**这句词在哪一拍上，哪个字被拖长了？** —— 因为拖长的地方通常就是情绪顶点，
也就是编曲该到的地方。

规则：每小节先一字一音从前往后铺；若音比字多，剩下的都算最后一字的拖腔（melisma）。
送别的实际唱法就是这一种（"碧连~~~天"、"今宵别梦~~寒"）。
"""
import sys
import yaml

import render

PHRASES = [
    ("5-8",   "长亭外古道边芳草碧连天"),
    ("9-12",  "晚风拂柳笛声残夕阳山外山"),
    ("13-16", "天之涯地之角知交半零落"),
    ("17-20", "一壶浊酒尽余欢今宵别梦寒"),
    ("21-24", None),   # 第 5 句是第 1 句的再现：词待定（长亭外？／哼鸣？）
]


def notes_per_bar(track):
    """旋律现在住在素材轨里，不再住在小提琴里。"""
    out = {}
    for seg in track["timeline"]:
        n = len(seg["degrees"].split())
        for b in render.parse_bars(seg["bars"], None):
            out[b] = n
    return out


def main():
    spec = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "farewell-v3.yaml"))
    theme = next(t for t in spec["material"] if t.get("theme"))
    npb = notes_per_bar(theme)

    print(f"{'编排/旋律':<10}{'音数':>4}  {'字':<16}{'拖腔':>4}   说明")
    peaks = []
    for rng, text in PHRASES:
        a, z = (int(x) for x in rng.split("-"))
        bars = list(range(a, z + 1))
        total_notes = sum(npb.get(b, 0) for b in bars)
        if text is None:
            print(f"  {rng:<9}{total_notes:>4}  —— 再现句，词待定（长亭外 ／ 哼鸣）\n")
            continue
        syll = list(text)
        melisma_total = total_notes - len(syll)

        i = 0
        for b in bars:
            n = npb.get(b, 0)
            take = min(n, max(0, len(syll) - i))
            got = syll[i:i + take]
            extra = n - take
            i += take
            note = ""
            if extra > 0 and got:
                note = f"← 「{got[-1]}」拖 {extra} 音"
                if extra >= 2:
                    peaks.append((b, got[-1], extra))
            print(f"  {b:<9}{n:>4}  {''.join(got):<16}{extra if extra>0 else '':>4}   {note}")
        print(f"  {'小计':<9}{total_notes:>4}  字数 {len(syll)}     差 {melisma_total:+d}"
              f"  {'✓ 对得上' if melisma_total >= 0 else '✗ 字比音多'}\n")

    print("  拖腔 ≥2 音的位置（≈ 情绪顶点，编曲该到的地方）:")
    for b, ch, n in peaks:
        print(f"    编排 bar {b:>2}（旋律 bar {b-4:>2}）「{ch}」拖 {n} 音")


if __name__ == "__main__":
    main()
