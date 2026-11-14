# 用随机合法指令把 1939 地图跑若干年，确认引擎能加载、每个阶段都能推进
import random, sys
from collections import Counter
from diplomacy import Game
def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    years = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    rng = random.Random(seed)
    g = Game(map_name="europe1939")
    print("地图警告（原版即有）：", g.map.error)
    print("加载成功：", sorted(g.powers), "胜利线", g.win, "补给中心", len(g.map.scs))
    start = {p: (len(v.centers), len(v.units)) for p, v in g.powers.items()}
    print("开局：", start)
    while not g.is_game_done and int(g.get_current_phase()[1:5]) < 1939 + years:
        poss = g.get_all_possible_orders()
        for p, power in g.powers.items():
            locs = g.get_orderable_locations(p)
            g.set_orders(p, [rng.choice(poss[l]) for l in locs if poss[l]])
        g.process()
    cent = {p: len(v.centers) for p, v in g.powers.items()}
    print("结束于", g.get_current_phase(), "补给中心：", cent, "胜者：", [p for p, n in cent.items() if n >= g.win])

if __name__ == "__main__":
    main()
