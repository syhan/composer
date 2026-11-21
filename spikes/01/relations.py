#!/usr/bin/env python3
"""spike 1: 关系层 —— 编排的对象是"对"，不是声部（§3.11）。

不做任何禁止。只回答两个问题：
  1. 此刻实际听到的是谁？（显著度 = 测量，不是声明）
  2. 哪两个声部在交织，哪两个在互相糊？（成对指标）

同一组数字，可能是"齐奏"也可能是"糊"——只有耳朵能分开。所以这里只描述。
"""
import sys
import yaml

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from render import expand, autoplace, apply_presence, load_ensemble, note_name

GRID = 16


def load(path):
    spec = yaml.safe_load(open(path))
    ens, insts = load_ensemble(spec, path)
    bars, parts = expand(spec, insts)
    bars = autoplace(bars, parts, spec["meta"]["grid"])
    bars, _ = apply_presence(spec, bars)
    return spec, bars, parts


def per_bar(bars):
    out = {}
    for b, evs in bars.items():
        d = {}
        for (p, st, du, ps, v) in evs:
            e = d.setdefault(p["id"], {"name": p["name"], "steps": set(), "pitches": [],
                                       "at": {}, "notes": 0})
            e["steps"].add(st)
            e["at"].setdefault(st, []).extend(ps)
            e["pitches"].extend(ps)
            e["notes"] += 1
        out[b] = d
    return out


def salience(pb):
    """每小节谁最容易被听见：运动独占、密度、音区独立。全是描述。"""
    rows = []
    for b in sorted(pb):
        d = pb[b]
        if not d:
            continue
        allsteps = set().union(*(e["steps"] for e in d.values()))
        allp = [x for e in d.values() for x in e["pitches"]]
        tot = sum(e["notes"] for e in d.values()) or 1
        scores = {}
        for pid, e in d.items():
            others = [x for q, f in d.items() if q != pid for x in f["pitches"]]
            own_lo, own_hi = min(e["pitches"]), max(e["pitches"])
            if others:
                olo, ohi = min(others), max(others)
                inter = max(0, min(own_hi, ohi) - max(own_lo, olo))
                reg = 1 - inter / max(1, min(own_hi - own_lo, ohi - olo))
            else:
                reg = 1.0
            excl = sum(1 for s in e["steps"]
                       if not any(s in f["steps"] for q, f in d.items() if q != pid)) / max(1, len(e["steps"]))
            dens = e["notes"] / tot
            scores[e["name"]] = 0.4 * excl + 0.3 * dens * min(3, len(d)) / 3 + 0.3 * reg
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:2]
        rows.append((b, top))
    return rows


def pair_metrics(pb):
    agg = {}
    for b, d in pb.items():
        ids = list(d)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, c = d[ids[i]], d[ids[j]]
                k = tuple(sorted((a["name"], c["name"])))
                m = agg.setdefault(k, {"bars": 0, "co": 0, "on": 0, "uni": 0, "iv": [], "dens": []})
                m["bars"] += 1
                co = sum(1 for s in a["steps"] if s in c["steps"])
                m["on"] += len(a["steps"])
                m["co"] += co
                m["dens"].append((len(a["steps"]) + len(c["steps"])) / 2)
                # 音程只在**同时发声**的位置上算（跨小节全塞在一起是错的）
                for s in a["steps"] & c["steps"]:
                    for x in a["at"][s]:
                        for y in c["at"][s]:
                            iv = abs(x - y)
                            m["iv"].append(iv)
                            if iv % 12 == 0:
                                m["uni"] += 1
    return agg


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "farewell-v3.yaml"
    spec, bars, parts = load(path)
    pb = per_bar(bars)

    print("  显著度剖面（测量，不是声明）——每小节实际最容易听到的两个")
    for b, top in salience(pb):
        chord = next(h["name"] for h in spec["harmony"] if h["bar"] == b)
        s = "  ".join(f"{n}({v:.2f})" for n, v in top)
        print(f"    bar {b:>2}  {chord:<7} {s}")

    print("\n  关系表（成对指标；按交错度排序）")
    print(f"    {'一对声部':<38}{'同在':>4}{'节奏重合':>9}{'交错度':>8}{'中位音程':>6}{'同八度':>7}")
    rows = []
    for (x, y), m in pair_metrics(pb).items():
        if m["bars"] < 2:
            continue
        coin = m["co"] / max(1, m["on"])
        cover = min(1.0, m["on"] / (m["bars"] * GRID / 2))
        interlock = cover * (1 - coin)
        ivs = sorted(m["iv"])
        med = ivs[len(ivs) // 2] if ivs else 0
        uni = m["uni"] / max(1, len(m["iv"]))
        rows.append((interlock, f"{x} × {y}", m["bars"], coin, med, uni, sum(m["dens"]) / len(m["dens"])))
    for il, name, bars_n, coin, med, uni, dens in sorted(rows, reverse=True):
        print(f"    {name[:37]:<38}{bars_n:>4}{coin*100:>8.0f}%{il:>8.2f}{med:>7}{uni*100:>7.0f}%  {dens:>4.1f}音/小节")

    print("\n  值得看的四类（阈值只是为了让眼睛先看到东西，不是规则）：")
    for il, name, bars_n, coin, med, uni, dens in sorted(rows, reverse=True)[:3]:
        print(f"    交织  {name}：交错度 {il:.2f}，重合仅 {coin*100:.0f}%，中位音程 {med} 半音")
    for il, name, bars_n, coin, med, uni, dens in sorted(rows, reverse=True):
        if coin > 0.75 and med < 12:
            print(f"    碰撞  {name}：重合 {coin*100:.0f}% + 中位音程 {med} 半音 → 这一段可能糊")
    for il, name, bars_n, coin, med, uni, dens in sorted(rows, reverse=True):
        if uni > 0.8:
            print(f"    叠置  {name}：同八度/同度 {uni*100:.0f}% → 有意齐奏（还是糊？耳朵定）")


if __name__ == "__main__":
    main()
