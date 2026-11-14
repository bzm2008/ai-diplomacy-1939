# 自测三套新机制（不调用大模型，固定种子）：领袖被动技能、随机事件+Laya 导演、流亡政府
# 用法：cd <repo> && PYTHONPATH=. /tmp/venv1939/bin/python tools/test_new_mechanics.py
# 真实 Laya 调用（需 venv-laya 且已下载权重）：DIP_TEST_REAL_LAYA=1 ...（其余测试默认不碰模型）
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DIP_LAYA", "0")            # 默认不加载 Laya 模型，走种子随机兜底
os.environ.setdefault("DIP_MECHANICS_SEED", "42")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import logging
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from diplomacy import Game  # noqa: E402
from diplomacy.utils.export import from_saved_game_format  # noqa: E402

from ai_diplomacy import laya_director  # noqa: E402
from ai_diplomacy.cards import get_card_manager  # noqa: E402
from ai_diplomacy.events import EVENT_IDS, EventManager  # noqa: E402
from ai_diplomacy.exile import is_active, is_normal_active  # noqa: E402
from ai_diplomacy.game_history import GameHistory  # noqa: E402
from ai_diplomacy.game_logic import save_game_state  # noqa: E402
from ai_diplomacy.mechanics import (  # noqa: E402
    add_unit, get_mechanics_manager, mechanics_context_for_power, move_unit,
    remove_unit, speer_miracle_active,
)
from ai_diplomacy.negotiations import conduct_negotiations  # noqa: E402
from ai_diplomacy.passives import (  # noqa: E402
    PASSIVE_TEXTS, apply_bonus_move, diplomacy_brief, filter_mechanics_orders,
)

PASSED = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED.append(name)
    print(f"  PASS {name}" + (f" ({detail})" if detail else ""))


def fresh_game():
    g = Game(map_name="europe1939")
    get_card_manager(g)
    get_mechanics_manager(g)
    return g


def wipe(g, power):
    """Remove every unit and center from a power (defeat setup)."""
    p = g.powers[power]
    for unit in list(p.units):
        remove_unit(g, power, unit)
    for center in list(p.centers):
        if center in p.influence:
            p.influence.remove(center)
    p.centers = []


def hold_orders(g):
    return {name: [f"{u} H" for u in p.units] for name, p in g.powers.items()}


def run_holds(g, n=1):
    for _ in range(n):
        for name, orders in hold_orders(g).items():
            g.set_orders(name, orders)
        g.process()


def advance_to(g, target_phase, limit=30):
    for _ in range(limit):
        if g.get_current_phase() == target_phase:
            return
        run_holds(g, 1)
    raise AssertionError(f"did not reach {target_phase}, stuck at {g.get_current_phase()}")


class FakeDirector:
    """Deterministic stand-in for LayaDirector (injected via set_director)."""

    def __init__(self, pick=None, timing="this_phase", confidence=0.9):
        self.pick = pick  # preferred option key, else first key
        self.timing = timing
        self.confidence = confidence
        self.calls = []

    def choose(self, state_summary, question_id, instructions, options, rng):
        self.calls.append(question_id)
        keys = list(options.keys())
        choice = self.pick if self.pick in keys else keys[0]
        return choice, self.confidence, "laya"

    def choose_timing(self, state_summary, rng):
        return self.timing, self.confidence, "laya"


def section(title):
    print(f"\n== {title} ==")


# ---------------------------------------------------------------- 1. 被动
def test_passives():
    section("1. 领袖被动技能")
    g = fresh_game()
    m = g.map
    assert m.abuts("F", "WES", "-", "TYS") and m.abuts("F", "LYO", "-", "TYS")
    assert m.abuts("A", "BUR", "-", "PAR") and m.abuts("A", "BER", "-", "SIL")
    assert m.abuts("A", "VIE", "-", "TRI") and "TRI" in m.scs
    owned = {c for p in g.powers.values() for c in p.centers}
    assert "TRI" not in owned  # TRI 开局中立

    # 意大利：地中海内无支援进攻意大利舰队 -> 无效；有支援 -> 保留；地中海外不管
    wipe(g, "ITALY"); wipe(g, "BRITAIN"); wipe(g, "FRANCE")
    add_unit(g, "ITALY", "F TYS"); add_unit(g, "BRITAIN", "F WES"); add_unit(g, "FRANCE", "F LYO")
    orders = {"ITALY": ["F TYS H"], "BRITAIN": ["F WES - TYS"], "FRANCE": ["F LYO H"]}
    kept, forb = filter_mechanics_orders(g, orders, "S1939M")
    check("意大利无支援进攻被过滤", forb.get("BRITAIN") == ["F WES - TYS"], str(forb))
    orders["FRANCE"] = ["F LYO S F WES - TYS"]
    kept, forb = filter_mechanics_orders(g, orders, "S1939M")
    check("意大利有支援进攻保留", "F WES - TYS" in kept["BRITAIN"], str(kept))
    orders["FRANCE"] = ["F LYO S WES - TYS"]  # 简写支援格式同样识别
    kept, forb = filter_mechanics_orders(g, orders, "S1939M")
    check("简写支援格式同样识别", "F WES - TYS" in kept["BRITAIN"], str(kept))
    orders["BRITAIN"] = ["F WES - NAF"]  # NAF 不是意大利舰队所在省
    kept, forb = filter_mechanics_orders(g, orders, "S1939M")
    check("非进攻意大利舰队的指令不管", not forb, str(forb))

    # 法国：无支援进攻巴黎 -> 无效；有支援 -> 保留；巴黎无法国单位且非法国领土时不管
    g = fresh_game()
    add_unit(g, "FRANCE", "A PAR"); add_unit(g, "GERMANY", "A BUR")
    orders = {"FRANCE": ["A PAR H"], "GERMANY": ["A BUR - PAR"]}
    kept, forb = filter_mechanics_orders(g, orders, "S1939M")
    check("法国无支援进攻巴黎被过滤", forb.get("GERMANY") == ["A BUR - PAR"], str(forb))
    orders["GERMANY"] = ["A BUR - PAR"]
    orders["ITALY"] = ["A VEN S A BUR - PAR"]
    kept, forb = filter_mechanics_orders(g, orders, "S1939M")
    check("法国有支援进攻巴黎保留", "A BUR - PAR" in kept["GERMANY"], str(kept))
    g2 = fresh_game()
    remove_unit(g2, "FRANCE", "A PAR") if "A PAR" in g2.powers["FRANCE"].units else None
    g2.powers["FRANCE"].centers = [c for c in g2.powers["FRANCE"].centers if c != "PAR"]
    add_unit(g2, "GERMANY", "A BUR")
    kept, forb = filter_mechanics_orders(g2, {"GERMANY": ["A BUR - PAR"]}, "S1939M")
    check("巴黎失守后被动不再触发", not forb, str(forb))

    # 苏联冬将军：1941 年秋季苏联本土外国陆军只能驻守；春季/1940 年不管
    g = fresh_game()
    add_unit(g, "GERMANY", "A MOS")
    orders = {"GERMANY": ["A MOS - SEV", "A BER H"]}
    kept, forb = filter_mechanics_orders(g, orders, "F1941M")
    check("冬将军冻结外国陆军", forb.get("GERMANY") == ["A MOS - SEV"], str(forb))
    kept, forb = filter_mechanics_orders(g, orders, "S1941M")
    check("冬将军春季不管", not forb, str(forb))
    kept, forb = filter_mechanics_orders(g, orders, "F1940M")
    check("冬将军 1941 年前不管", not forb, str(forb))
    kept, forb = filter_mechanics_orders(g, {"GERMANY": ["A MOS H", "A MOS S BER H"]}, "F1941M")
    check("冬将军下驻守/支援保留", not forb, str(forb))

    # 德国奖励移动：约束全集
    g = fresh_game()
    ok, _ = apply_bonus_move(g, "GERMANY", "A BER", "SIL", [])
    check("奖励移动合法目标", ok and "A SIL" in g.powers["GERMANY"].units)
    g = fresh_game()
    ok, why = apply_bonus_move(g, "GERMANY", "A BER", "KIE", [])  # 有德国舰队
    check("奖励移动拒绝非空目标", not ok, why)
    ok, why = apply_bonus_move(g, "GERMANY", "A BER", "WAR", [])  # 波兰补给中心
    check("奖励移动拒绝敌方中心", not ok, why)
    ok, why = apply_bonus_move(g, "GERMANY", "A BER", "PAR", [])  # 不相邻
    check("奖励移动拒绝不相邻", not ok, why)
    ok, why = apply_bonus_move(g, "GERMANY", "F KIE", "HEL", [])
    check("奖励移动拒绝舰队", not ok, why)
    ok, why = apply_bonus_move(g, "GERMANY", "A BER", "SIL", [["A SIL - WAR"]])
    check("奖励移动拒绝本阶段腾出的省", not ok, why)
    ok, why = apply_bonus_move(g, "GERMANY", "A BER", "SIL", [["A MUN - SIL"]])
    check("奖励移动拒绝本阶段进入的省", not ok, why)
    ok, why = apply_bonus_move(g, "GERMANY", "A BER", "TRI", [])  # TRI 不与 BER 相邻
    check("奖励移动拒绝不相邻中立中心", not ok, why)
    g = fresh_game()
    ok, _ = apply_bonus_move(g, "GERMANY", "A VIE", "TRI", [])
    check("奖励移动可进中立补给中心", ok and "A TRI" in g.powers["GERMANY"].units)

    # 美国民主兵工厂：1941 年起冬季 +1 建造，且超建不被强制解散
    g = fresh_game()
    remove_unit(g, "GERMANY", g.powers["GERMANY"].units[0])  # 制造不平衡，否则引擎跳过冬季
    advance_to(g, "W1941A")
    usa = g.powers["USA"]
    while len(usa.units) > len(usa.centers) - 1:
        remove_unit(g, "USA", usa.units[0])
    raw = len(usa.centers) - len(usa.units)
    check("美国 1941 冬季 +1 建造", g._adjustment_balance(g.powers["USA"]) == raw + 1,
          f"raw={raw} adjusted={g._adjustment_balance(g.powers['USA'])}")
    add_unit(g, "USA", "A NYC") if "A NYC" not in usa.units else None
    add_unit(g, "USA", "F NWA") if len(usa.units) <= len(usa.centers) else None
    occupied = {u.split()[-1].split("/")[0] for p in g.powers.values() for u in p.units}
    empties = [loc for loc in sorted(g.map.loc_type) if g.map.loc_type[loc] != "WATER" and loc not in occupied]
    while len(usa.units) <= len(usa.centers):
        add_unit(g, "USA", f"A {empties.pop(0)}")
    check("美国超建豁免（balance 不为负）", g._adjustment_balance(g.powers["USA"]) == 0,
          f"units={len(usa.units)} centers={len(usa.centers)}")
    g = fresh_game()
    remove_unit(g, "GERMANY", g.powers["GERMANY"].units[0])  # 制造不平衡，否则引擎跳过冬季
    advance_to(g, "W1940A")
    usa = g.powers["USA"]
    while len(usa.units) > len(usa.centers) - 1:
        remove_unit(g, "USA", usa.units[0])
    raw = len(usa.centers) - len(usa.units)
    check("美国 1941 年前无加成", g._adjustment_balance(g.powers["USA"]) == raw, f"raw={raw}")

    # 德国施佩尔奇迹：1942-1944 年冬季 +1 建造，1945 年起失效；有效期内超建豁免
    for winter, expected_bonus in [("W1941A", 0), ("W1942A", 1), ("W1943A", 1),
                                   ("W1944A", 1), ("W1945A", 0)]:
        g = fresh_game()
        remove_unit(g, "FRANCE", g.powers["FRANCE"].units[0])  # 制造不平衡，否则引擎跳过冬季
        advance_to(g, winter)
        ger = g.powers["GERMANY"]
        while len(ger.units) > len(ger.centers) - 1:
            remove_unit(g, "GERMANY", ger.units[0])
        raw = len(ger.centers) - len(ger.units)
        check(f"施佩尔奇迹 {winter} 加成 {expected_bonus}",
              g._adjustment_balance(g.powers["GERMANY"]) == raw + expected_bonus,
              f"raw={raw} adjusted={g._adjustment_balance(g.powers['GERMANY'])}")
    g = fresh_game()
    remove_unit(g, "FRANCE", g.powers["FRANCE"].units[0])
    advance_to(g, "W1943A")
    ger = g.powers["GERMANY"]
    occupied = {u.split()[-1].split("/")[0] for p in g.powers.values() for u in p.units}
    empties = [loc for loc in sorted(g.map.loc_type) if g.map.loc_type[loc] != "WATER" and loc not in occupied]
    while len(ger.units) <= len(ger.centers):
        add_unit(g, "GERMANY", f"A {empties.pop(0)}")
    check("施佩尔奇迹超建豁免（balance 不为负）", g._adjustment_balance(g.powers["GERMANY"]) == 0,
          f"units={len(ger.units)} centers={len(ger.centers)}")
    g = fresh_game()
    remove_unit(g, "FRANCE", g.powers["FRANCE"].units[0])
    advance_to(g, "W1945A")
    ger = g.powers["GERMANY"]
    occupied = {u.split()[-1].split("/")[0] for p in g.powers.values() for u in p.units}
    empties = [loc for loc in sorted(g.map.loc_type) if g.map.loc_type[loc] != "WATER" and loc not in occupied]
    while len(ger.units) <= len(ger.centers):
        add_unit(g, "GERMANY", f"A {empties.pop(0)}")
    check("施佩尔奇迹 1945 年失效后超建须解散", g._adjustment_balance(g.powers["GERMANY"]) == -1,
          f"units={len(ger.units)} centers={len(ger.centers)}")
    # 广播：1942-1944 年所有国家可见事实，但看不到截止期；1945 年消失
    g = fresh_game()
    for ph in ["F1942M", "S1943M", "W1944A"]:
        for pw in ["BRITAIN", "GERMANY", "USA"]:
            block = mechanics_context_for_power(g, GameHistory(), pw, {}, ph)
            check(f"施佩尔广播 {ph} {pw} 可见", "施佩尔奇迹" in block, block[:80])
            check(f"施佩尔广播 {ph} {pw} 不透露截止期", "1944" not in block, block[:80])
    block45 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1945M")
    check("施佩尔广播 1945 年消失", "施佩尔奇迹" not in block45, block45[:80])
    check("德国提示词写明截止期",
          "1944" in PASSIVE_TEXTS["GERMANY"] and "施佩尔奇迹" in PASSIVE_TEXTS["GERMANY"])
    check("施佩尔年份判定",
          speer_miracle_active(1942) and speer_miracle_active(1944)
          and not speer_miracle_active(1941) and not speer_miracle_active(1945)
          and not speer_miracle_active(None))

    # 西班牙：绝不成为针对性事件目标
    g = fresh_game()
    mgr = get_mechanics_manager(g)
    check("西班牙不在合格目标内", "SPAIN" not in mgr.events._eligible_targets(g))
    check("西班牙不在饥荒候选内", "SPAIN" not in mgr.events._eligible_targets(g))
    # 英国简报：只给私信数量，不给内容
    hist = GameHistory()
    hist.add_phase("S1939M")
    hist.add_message("S1939M", "GERMANY", "FRANCE", "秘密进攻波兰计划绝密内容")
    hist.add_message("S1939M", "GERMANY", "FRANCE", "第二封信")
    hist.add_message("S1939M", "BRITAIN", "GLOBAL", "公开喊话")
    hist.add_phase("F1939M")
    brief = diplomacy_brief(hist, "F1939M")
    check("英国简报含数量不含内容",
          "2" in brief and "绝密内容" not in brief and "第二封信" not in brief, brief[:120])


# ---------------------------------------------------------------- 2. 事件
def test_events():
    section("2. 随机事件")
    # 牌堆：8 张不重不漏，抽完重洗
    mgr = EventManager({"seed": 7})
    drawn = [mgr.draw_card_id() for _ in range(8)]
    check("牌堆 8 张不重不漏", sorted(drawn) == sorted(EVENT_IDS), str(drawn))
    ninth = mgr.draw_card_id()
    check("牌堆抽完重洗", ninth in EVENT_IDS and len(mgr.deck) == 7)

    # 密电泄露：西班牙的信绝不被选中；无信则轮空
    for seed in range(20):
        os.environ["DIP_MECHANICS_SEED"] = str(seed)
        g = fresh_game()
        hist = GameHistory()
        hist.add_phase("S1939M")
        hist.add_message("S1939M", "SPAIN", "FRANCE", "西班牙密信")
        hist.add_message("S1939M", "GERMANY", "ITALY", "德国密信")
        hist.add_phase("F1939M")
        ev = get_mechanics_manager(g).events
        ev.deck = ["leak"] + [c for c in ev.deck if c != "leak"]
        rec = ev.draw_event(g, hist, "F1939M")
        assert rec["card_id"] == "leak"
        check(f"泄露排除西班牙 seed={seed}",
              not rec["fizzled"] and rec["leak"]["sender"] == "GERMANY", str(rec.get("leak")))
    g = fresh_game()
    hist = GameHistory()
    hist.add_phase("S1939M")
    hist.add_phase("F1939M")
    ev = get_mechanics_manager(g).events
    ev.deck = ["leak"] + [c for c in ev.deck if c != "leak"]
    rec = ev.draw_event(g, hist, "F1939M")
    check("泄露无信可泄则轮空", rec["fizzled"])

    # 铁路罢工：目标国陆军不能移动，驻守/支援保留，舰队不受影响
    g = fresh_game()
    fake = FakeDirector(pick="GERMANY")
    laya_director.set_director(fake)
    try:
        ev = get_mechanics_manager(g).events
        ev.deck = ["rail_strike"] + [c for c in ev.deck if c != "rail_strike"]
        rec = ev.draw_event(g, GameHistory(), "S1939M")
        check("铁路罢工定向德国", rec["targets"] == ["GERMANY"], str(rec["targets"]))
        effect = ev.active_filter_effect()
        orders = {"GERMANY": ["A BER - SIL", "A MUN H", "A BER S MUN H", "F KIE - HEL"],
                  "FRANCE": ["A PAR - BUR"]}
        kept, forb = filter_mechanics_orders(g, orders, "S1939M", effect)
        check("铁路罢工只拦陆军移动",
              forb.get("GERMANY") == ["A BER - SIL"] and "F KIE - HEL" in kept["GERMANY"],
              str(forb))
    finally:
        laya_director.set_director(None)

    # 粮食危机：领跑者当年冬季不能建造
    os.environ["DIP_MECHANICS_SEED"] = "42"
    g = fresh_game()
    ev = get_mechanics_manager(g).events
    ev.deck = ["famine"] + [c for c in ev.deck if c != "famine"]
    rec = ev.draw_event(g, GameHistory(), "S1939M")
    leader = rec["targets"][0]
    check("饥荒目标是中心数最多者",
          len(g.powers[leader].centers) == max(len(p.centers) for p in g.powers.values()), leader)
    remove_unit(g, "ITALY", g.powers["ITALY"].units[0])  # 制造不平衡，否则引擎跳过冬季
    advance_to(g, "W1939A")
    p = g.powers[leader]
    while len(p.units) > len(p.centers) - 1:
        remove_unit(g, leader, p.units[0])
    check("饥荒年冬季领跑者 0 建造", g._adjustment_balance(g.powers[leader]) == 0,
          f"{leader} units={len(p.units)} centers={len(p.centers)}")
    check("饥荒目标登记", ev.famine_target_for_year(1939) == leader)

    # 同种子可复现：两局抽牌序列完全一致
    seqs = []
    for _ in range(2):
        os.environ["DIP_MECHANICS_SEED"] = "1234"
        g = fresh_game()
        ev = get_mechanics_manager(g).events
        seq = []
        for year in range(1939, 1947):
            rec = ev.draw_event(g, GameHistory(), f"S{year}M")
            seq.append((rec["card_id"], tuple(rec["targets"]), rec["zone"], rec["timing"]))
        seqs.append(seq)
    check("同种子事件序列一致", seqs[0] == seqs[1], str(seqs[0][:3]))

    # 可见性：全局事件人人可见；目标事件只给目标+盟友；饥荒全年可见
    g = fresh_game()
    fake = FakeDirector(pick="GERMANY")
    laya_director.set_director(fake)
    try:
        ev = get_mechanics_manager(g).events
        ev.deck = ["port_strike"] + [c for c in ev.deck if c != "port_strike"]
        ev.draw_event(g, GameHistory(), "S1939M")
        check("目标事件目标可见", "港口工人罢工" in ev.context_block_for_power(g, "GERMANY", {}, "S1939M"))
        check("目标事件盟友可见", "港口工人罢工" in ev.context_block_for_power(g, "ITALY", {"GERMANY": "Ally"}, "S1939M"))
        check("目标事件无关者不可见", ev.context_block_for_power(g, "FRANCE", {}, "S1939M") == "")
        ev.deck = ["storm"] + [c for c in ev.deck if c != "storm"]
        ev.begin_phase(g, "F1939M")
        ev.draw_event(g, GameHistory(), "F1939M")
        check("海上风暴全局可见", "风暴肆虐" in ev.context_block_for_power(g, "SPAIN", {}, "F1939M"))
    finally:
        laya_director.set_director(None)

    # 外交风波：两人成对、互不相同、西班牙不在内
    g = fresh_game()
    laya_director.set_director(FakeDirector())
    try:
        ev = get_mechanics_manager(g).events
        ev.deck = ["feud"] + [c for c in ev.deck if c != "feud"]
        rec = ev.draw_event(g, GameHistory(), "S1939M")
        check("外交风波两人成对", len(rec["targets"]) == 2 and len(set(rec["targets"])) == 2,
              str(rec["targets"]))
        check("外交风波排除西班牙", "SPAIN" not in rec["targets"])
        check("外交风波 pending 可查", ev.feud_pair() is not None or ev.pending is not None)
    finally:
        laya_director.set_director(None)

    # 预备役动员：最弱且有空闲本土中心者立即补一支
    g = fresh_game()
    wipe(g, "SPAIN")
    add_unit(g, "SPAIN", "A SPA")
    g.powers["SPAIN"].centers = ["SPA", "NAF"]
    ev = get_mechanics_manager(g).events
    ev.deck = ["reserves"] + [c for c in ev.deck if c != "reserves"]
    rec = ev.draw_event(g, GameHistory(), "S1939M")
    check("预备役动员补一支", not rec["fizzled"] and len(g.powers["SPAIN"].units) == 2,
          str(rec["targets"]))


# ---------------------------------------------------------------- 3. Laya
def test_laya():
    section("3. Laya 导演")
    rng = random.Random(1)
    options = {"GERMANY": "g", "FRANCE": "f"}
    old_laya = os.environ.get("DIP_LAYA")
    os.environ["DIP_LAYA"] = "1"  # 以下用例测模型分支，须打开开关

    # 模型不可用 -> 种子随机兜底
    director = laya_director.LayaDirector()
    director._load_tried = True  # 模拟加载失败
    director._agent = None
    choice, conf, source = director.choose("state", "target", "pick", options, rng)
    check("Laya 不可用退回随机", source == "random" and choice in options, f"{choice}/{source}")

    # 低置信度 -> 种子随机兜底；高置信度 -> 采用 Laya 选择
    class StubAgent:
        def __init__(self, choice, confidence):
            self.choice, self.confidence = choice, confidence
        def system_one(self, state, questions):
            qid = next(iter(questions))
            return {"answers": {qid: {"type": "choice", "choice": self.choice,
                                      "answer_confidence": self.confidence}}}
    director = laya_director.LayaDirector()
    director._agent = StubAgent("GERMANY", 0.1)
    director._load_tried = True
    choice, conf, source = director.choose("state", "target", "pick", options, random.Random(1))
    check("低置信度退回随机", source == "random", f"conf={conf}")
    director._agent = StubAgent("GERMANY", 0.95)
    choice, conf, source = director.choose("state", "target", "pick", options, random.Random(1))
    check("高置信度采用 Laya", source == "laya" and choice == "GERMANY", f"{choice}/{conf}")
    director._agent = StubAgent("SPAIN", 0.95)  # 不在允许集合内
    choice, conf, source = director.choose("state", "target", "pick", options, random.Random(1))
    check("越界选择退回随机", source == "random" and choice in options, choice)
    choice, conf, source = director.choose("state", "t", "pick", {"ONLY": "o"}, random.Random(1))
    check("单选项强制", (choice, source) == ("ONLY", "forced"))
    if old_laya is None:
        os.environ.pop("DIP_LAYA", None)
    else:
        os.environ["DIP_LAYA"] = old_laya

    # 硬约束：导演即使点名西班牙，抽牌也不会选中西班牙（选项集合已排除）
    g = fresh_game()
    laya_director.set_director(FakeDirector(pick="SPAIN"))
    try:
        ev = get_mechanics_manager(g).events
        ev.deck = ["assassination"] + [c for c in ev.deck if c != "assassination"]
        rec = ev.draw_event(g, GameHistory(), "S1939M")
        check("硬约束挡住西班牙", rec["targets"] != ["SPAIN"], str(rec["targets"]))
    finally:
        laya_director.set_director(None)

    # mock 注入：draw_event 走注入的导演
    g = fresh_game()
    fake = FakeDirector(pick="ITALY", timing="next_movement")
    laya_director.set_director(fake)
    try:
        ev = get_mechanics_manager(g).events
        ev.deck = ["port_strike"] + [c for c in ev.deck if c != "port_strike"]
        rec = ev.draw_event(g, GameHistory(), "S1939M")
        check("mock 导演决定目标+时间",
              rec["targets"] == ["ITALY"] and rec["timing"] == "next_movement"
              and ev.pending is not None and ev.active is None, str(rec))
        ev.begin_phase(g, "F1939M")
        check("pending 事件下阶段激活", ev.active is not None and ev.active["card_id"] == "port_strike")
    finally:
        laya_director.set_director(None)

    # 真实 Laya 调用（仅 DIP_TEST_REAL_LAYA=1 且 venv-laya 可用时）
    if os.environ.get("DIP_TEST_REAL_LAYA") != "1":
        print("  SKIP 真实 Laya 调用（未设置 DIP_TEST_REAL_LAYA=1）")
        return
    laya_python = Path.home() / "workspace/ai-ww2-prep/diplomacy-1939/work/venv-laya/bin/python"
    if not laya_python.exists():
        print("  SKIP 真实 Laya 调用（venv-laya 不存在）")
        return
    code = (
        "import random, sys; sys.path.insert(0, %r)\n"
        "from ai_diplomacy.laya_director import LayaDirector\n"
        "d = LayaDirector()\n"
        "choice, conf, src = d.choose('Spring 1939. Germany 5 centers, France 4 centers.',\n"
        "  'target', 'Which country should the rail strike hit?',\n"
        "  {'GERMANY': 'g', 'FRANCE': 'f'}, random.Random(0))\n"
        "print(f'REAL_LAYA choice={choice} conf={conf:.3f} source={src}')\n"
        "assert choice in ('GERMANY', 'FRANCE') and src in ('laya', 'random')\n"
    ) % str(REPO)
    env = dict(os.environ)
    env.pop("DIP_LAYA", None)  # 真实调用需要 Laya 开关打开
    proc = subprocess.run([str(laya_python), "-c", code], capture_output=True, text=True,
                          timeout=600, cwd=str(REPO), env=env)
    print("  " + "\n  ".join((proc.stdout + proc.stderr).strip().splitlines()[-5:]))
    check("真实 Laya 调用完成", proc.returncode == 0, f"rc={proc.returncode}")


# ---------------------------------------------------------------- 4. 流亡
def test_exile():
    section("4. 流亡政府")

    def seeking_game(power="POLAND", host="BRITAIN"):
        g = fresh_game()
        wipe(g, power)
        reg = get_mechanics_manager(g).exile
        reg.detect_triggers(g, "S1939M")
        return g, reg

    # 触发：波兰 3 阶段，别国 2 阶段；未触发前 is_active 与引擎一致
    g, reg = seeking_game("POLAND")
    check("波兰求助期 3 阶段", reg.seeking["POLAND"]["phases_left"] == 3)
    check("待流亡仍算活跃", is_active(g, "POLAND") and not is_normal_active(g, "POLAND"))
    g, reg = seeking_game("FRANCE")
    check("别国求助期 2 阶段", reg.seeking["FRANCE"]["phases_left"] == 2)

    # 接受 -> 流亡成立；拒绝 -> 继续求助；沉默 -> 无记录
    g, reg = seeking_game("POLAND")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"POLAND": "accept"}})
    reg.resolve_pending(g, "S1939M")
    check("接受后流亡成立", reg.exiled.get("POLAND", {}).get("host") == "BRITAIN")
    check("流亡者可参与", is_active(g, "POLAND"))
    g, reg = seeking_game("FRANCE")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"FRANCE": False}})
    reg.resolve_pending(g, "S1939M")
    check("拒绝后仍在求助", "FRANCE" in reg.seeking and "FRANCE" not in reg.exiled)
    g, reg = seeking_game("FRANCE")
    reg.resolve_pending(g, "S1939M")
    check("沉默视为拒绝", "FRANCE" in reg.seeking and "FRANCE" not in reg.exiled)
    g, reg = seeking_game("FRANCE")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"FRANCE": True}})
    reg.resolve_pending(g, "S1939M")
    check("布尔 true 也接受", reg.exiled.get("FRANCE", {}).get("host") == "BRITAIN")

    # 求助期耗尽 -> 正常灭亡
    g, reg = seeking_game("FRANCE")
    reg.end_movement_phase(g, "S1939M")  # 创建当阶段不扣
    check("创建当阶段不扣求助期", reg.seeking["FRANCE"]["phases_left"] == 2)
    reg.end_movement_phase(g, "F1939M")
    reg.end_movement_phase(g, "S1940M")
    check("求助期耗尽后灭亡", "FRANCE" not in reg.seeking and not is_active(g, "FRANCE"))

    # 驱逐 -> 回到待流亡，窗口 1 阶段
    g, reg = seeking_game("POLAND")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"POLAND": "accept"}})
    reg.resolve_pending(g, "S1939M")
    reg.record_fields(g, "BRITAIN", {"expel_exile": True})
    reg.resolve_pending(g, "F1939M")
    check("驱逐后回到待流亡", "POLAND" in reg.seeking and reg.seeking["POLAND"]["phases_left"] == 1)

    # 收留国倒台 -> 流亡者回到待流亡，窗口 1 阶段
    g, reg = seeking_game("POLAND")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"POLAND": "accept"}})
    reg.resolve_pending(g, "S1939M")
    wipe(g, "BRITAIN")
    reg.check_hosts_down(g, "F1939M")
    check("收留国倒台后回到待流亡",
          "POLAND" in reg.seeking and reg.seeking["POLAND"]["phases_left"] == 1)

    # 一国最多收留一个（按字母序确定性处理）
    g = fresh_game()
    wipe(g, "POLAND"); wipe(g, "FRANCE")
    reg = get_mechanics_manager(g).exile
    reg.detect_triggers(g, "S1939M")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"POLAND": "accept", "FRANCE": "accept"}})
    reg.resolve_pending(g, "S1939M")
    check("一国最多收留一个", len([e for e in reg.exiled.values() if e["host"] == "BRITAIN"]) == 1
          and len(reg.exiled) == 1, str(reg.exiled))
    check("第二位仍在求助", len(reg.seeking) == 1)

    # 资助 + 豁免 + 复国（全流程走真实引擎结算）
    g = fresh_game()
    wipe(g, "POLAND")
    reg = get_mechanics_manager(g).exile
    reg.detect_triggers(g, "S1939M")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"POLAND": "accept"}})
    reg.resolve_pending(g, "S1939M")
    reg.record_fields(g, "BRITAIN", {"fund_exile_unit": True})
    reg.resolve_pending(g, "S1939M")
    # 腾一个英国本土中心出来放复国军
    brit = g.powers["BRITAIN"]
    for unit in list(brit.units):
        if unit.split()[-1] in ("EDI", "LON", "LVP"):
            remove_unit(g, "BRITAIN", unit)
            break
    advance_to(g, "W1939A")
    reg.begin_winter(g, "W1939A")
    check("冬季资助登记", "BRITAIN" in reg.winter_funding, str(reg.winter_funding))
    run_holds(g, 1)  # 结算 W1939A
    reg.complete_winter(g, "W1939A")
    check("复国军已放置", len(g.powers["POLAND"].units) == 1, str(g.powers["POLAND"].units))
    check("资助国当年已记账", 1939 in reg.funded_years.get("BRITAIN", []))
    # 豁免：复国军 0 中心 1 部队，冬季结算不被强制解散
    unit = g.powers["POLAND"].units[0]
    occupied = {u.split()[-1].split("/")[0] for p in g.powers.values() for u in p.units}
    empty_land = next(loc for loc in sorted(g.map.loc_type)
                      if g.map.loc_type[loc] != "WATER" and loc not in occupied and loc not in g.map.scs)
    move_unit(g, "POLAND", unit, empty_land)
    advance_to(g, "W1940A")
    check("流亡者豁免 balance=0", g._adjustment_balance(g.powers["POLAND"]) == 0)
    run_holds(g, 1)
    check("复国军熬过冬季不被解散", len(g.powers["POLAND"].units) == 1)
    # 复国：部队站上本土中心并过冬季 -> 恢复
    unit = g.powers["POLAND"].units[0]
    move_unit(g, "POLAND", unit, "WAR")
    advance_to(g, "W1941A")
    run_holds(g, 1)
    reg.check_restorations(g, "W1941A")
    check("夺回本土中心复国",
          "POLAND" not in reg.exiled and "WAR" in g.powers["POLAND"].centers,
          str(g.powers["POLAND"].centers))
    check("复国后移出已处理", "POLAND" not in reg.processed)

    # 每年最多资助一支
    g, reg = seeking_game("POLAND")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"POLAND": "accept"}})
    reg.resolve_pending(g, "S1939M")
    reg.record_fields(g, "BRITAIN", {"fund_exile_unit": True})
    reg.resolve_pending(g, "S1939M")
    reg.record_fields(g, "BRITAIN", {"fund_exile_unit": True})
    reg.resolve_pending(g, "F1939M")
    check("重复资助意向只记一次", reg.pending_funding == {"BRITAIN": "POLAND"},
          str(reg.pending_funding))


# ------------------------------------------------------- 5. 存档/续跑一致性
def test_save_resume():
    section("5. 存档/续跑一致性")
    os.environ["DIP_MECHANICS_SEED"] = "42"
    g = fresh_game()
    run_holds(g, 1)  # S1939M
    ok, _ = apply_bonus_move(g, "GERMANY", "A VIE", "TRI", [])
    assert ok
    hist = GameHistory()
    hist.add_phase("S1939M")
    # 制造一个待流亡 + 一个已抽事件，让存档里有东西可比
    wipe(g, "SPAIN")
    reg = get_mechanics_manager(g).exile
    reg.detect_triggers(g, "S1939M")
    ev = get_mechanics_manager(g).events
    ev.draw_event(g, hist, "S1939M")
    before_units = {p: sorted(pw.units) for p, pw in g.powers.items()}
    before_state = get_mechanics_manager(g).to_dict()

    agents = {}
    for name in g.powers:
        agents[name] = SimpleNamespace(
            power_name=name,
            client=SimpleNamespace(model_name="test-model", max_tokens=100),
            goals=[], relationships={}, full_private_diary="", private_diary="",
        )
    run_config = SimpleNamespace(num_negotiation_rounds=0, prompts_dir="ai_diplomacy/prompts_1939")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lmvsgame.json")
        asyncio.run(save_game_state(g, agents, hist, path, run_config, "S1939M"))
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    check("存档含 mechanics_state", "mechanics_state" in saved)
    check("存档含 card_state", "card_state" in saved)
    completed_block = next(b for b in saved["phases"] if b["name"] == "S1939M")
    check("待流亡国家保留在 state_agents",
          "SPAIN" in completed_block.get("state_agents", {}), "待流亡仍算活跃，应保留")
    g2 = from_saved_game_format(saved)
    from ai_diplomacy.mechanics import MechanicsManager
    mgr2 = MechanicsManager.from_dict(saved.get("mechanics_state"))
    after_units = {p: sorted(pw.units) for p, pw in g2.powers.items()}
    check("续跑后单位位置一致（含奖励移动）", before_units == after_units,
          f"TRI: {[u for u in after_units['GERMANY'] if 'TRI' in u]}")
    check("续跑后牌堆状态一致", mgr2.to_dict()["events"]["deck"] == before_state["events"]["deck"])
    check("续跑后流亡登记一致", mgr2.exile.seeking == reg.seeking)
    # RNG 状态续跑：两边各抽一张，结果一致
    card_a = get_mechanics_manager(g).events.draw_card_id()
    card_b = mgr2.events.draw_card_id()
    check("RNG 状态续跑一致", card_a == card_b, f"{card_a} vs {card_b}")

    # 奖励移动进中立中心 -> 过冬后中心入账（真实引擎结算）
    g = fresh_game()
    remove_unit(g, "ITALY", g.powers["ITALY"].units[0])  # 制造不平衡保冬季存在
    run_holds(g, 1)
    ok, _ = apply_bonus_move(g, "GERMANY", "A VIE", "TRI", [])
    assert ok
    advance_to(g, "W1939A")
    run_holds(g, 1)
    check("奖励移动占领的中立中心冬季入账", "TRI" in g.powers["GERMANY"].centers,
          str(g.powers["GERMANY"].centers))


# ---------------------------------------------------------------- 6. JSON 容错
def test_json_tolerance():
    section("6. JSON 容错")
    g = fresh_game()
    wipe(g, "FRANCE")
    reg = get_mechanics_manager(g).exile
    reg.detect_triggers(g, "S1939M")
    malformed = [
        {"exile_decisions": "BRITAIN"},
        {"exile_decisions": {"FRANCE": 12345}},
        {"exile_decisions": {"NOWHERE": "accept"}},
        {"exile_decisions": {"FRANCE": {"nested": True}}},
        {"fund_exile_unit": {"amount": 2}},
        {"fund_exile_unit": 12345},
        {"expel_exile": ["FRANCE"]},
        {"expel_exile": 12345},
        "not a dict",
        None,
        42,
    ]
    for payload in malformed:
        reg.record_fields(g, "BRITAIN", payload)  # 不抛异常、不重试
    reg.resolve_pending(g, "S1939M")
    check("畸形字段全部忽略无崩溃", "FRANCE" in reg.seeking and "FRANCE" not in reg.exiled
          and not reg.pending_funding and not reg.pending_expulsions)
    # 混合：畸形字段不影响同对象里的合法字段
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"FRANCE": "accept"},
                                     "fund_exile_unit": {"bad": 1}})
    reg.resolve_pending(g, "S1939M")
    check("畸形字段不污染合法字段", reg.exiled.get("FRANCE", {}).get("host") == "BRITAIN")
    check("无关国家无提示词", get_mechanics_manager(fresh_game()).exile.diary_instructions(fresh_game(), "USA") == "")


# ------------------------------------------------- 7. is_active 审计 + 谈判实走
def test_is_active_audit():
    section("7. is_active 调用点审计 + 流亡者谈判实走")
    hits = []
    for path in [REPO / "lm_game.py", *sorted((REPO / "ai_diplomacy").glob("*.py"))]:
        lines = path.read_text(encoding="utf-8").splitlines()
        current_fn = ""
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("def "):
                current_fn = stripped[4:].split("(")[0]
            if "is_eliminated()" in line and "def is_eliminated" not in line:
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("``") or "``" in line:
                    continue  # 注释/文档字符串里的提及不算调用
                rel = str(path.relative_to(REPO))
                # 仅允许：exile 内部的原始判断、utils 打标签用的原始判断
                if rel == "ai_diplomacy/exile.py" and current_fn == "_is_engine_eliminated":
                    continue
                if rel == "ai_diplomacy/utils.py" and current_fn == "_power_status_tag":
                    continue
                hits.append(f"{rel}:{i}: {line.strip()}")
    check("参与判断调用点已全部切换", not hits, "; ".join(hits[:5]))

    # 流亡者无单位仍可谈判；外交风波过滤私信
    class StubClient:
        model_name = "stub"
        def __init__(self, messages):
            self._messages = messages
        async def get_conversation_reply(self, *args, **kwargs):
            return list(self._messages)

    class StubAgent:
        def __init__(self, power, messages):
            self.power_name = power
            self.client = StubClient(messages)
            self.goals, self.relationships = [], {}
            self.journal = []
        def format_private_diary_for_prompt(self):
            return ""
        def add_journal_entry(self, *args, **kwargs):
            self.journal.append(args)

    g = fresh_game()
    wipe(g, "POLAND")
    reg = get_mechanics_manager(g).exile
    reg.detect_triggers(g, "S1939M")
    reg.record_fields(g, "BRITAIN", {"exile_decisions": {"POLAND": "accept"}})
    reg.resolve_pending(g, "S1939M")
    hist = GameHistory()
    hist.add_phase("S1939M")
    agents = {
        "POLAND": StubAgent("POLAND", [{"message_type": "private", "recipient": "BRITAIN",
                                        "content": "请收留我们"}]),
        "BRITAIN": StubAgent("BRITAIN", [{"message_type": "private", "recipient": "POLAND",
                                          "content": "欢迎"}]),
    }
    asyncio.run(conduct_negotiations(g, agents, hist, {}, "/tmp/stub.log", max_rounds=1))
    sent = [(m.sender, m.recipient) for m in hist.phases[-1].messages]
    check("流亡者可发私信求助", ("POLAND", "BRITAIN") in sent, str(sent))

    # 外交风波：风波中的两人私信被丢弃，给第三国的不受影响
    g = fresh_game()
    ev = get_mechanics_manager(g).events
    ev.active = {"card_id": "feud", "targets": ["GERMANY", "FRANCE"], "text": "test",
                 "timing": "this_phase", "fizzled": False}
    hist = GameHistory()
    hist.add_phase("S1939M")
    agents = {
        "GERMANY": StubAgent("GERMANY", [{"message_type": "private", "recipient": "FRANCE",
                                          "content": "悄悄话"},
                                         {"message_type": "private", "recipient": "ITALY",
                                          "content": "给意大利"}]),
        "FRANCE": StubAgent("FRANCE", []),
        "ITALY": StubAgent("ITALY", []),
    }
    asyncio.run(conduct_negotiations(g, agents, hist, {}, "/tmp/stub.log", max_rounds=1))
    sent = [(m.sender, m.recipient) for m in hist.phases[-1].messages]
    check("外交风波过滤两人私信", ("GERMANY", "FRANCE") not in sent, str(sent))
    check("外交风波不影响第三国", ("GERMANY", "ITALY") in sent, str(sent))


# ---------------------------------------------------------------- 8. 环境开关
def test_env_switches():
    section("8. 环境开关独立生效")
    old = {k: os.environ.get(k) for k in ("DIP_EVENTS", "DIP_PASSIVES", "DIP_EXILE", "DIP_LAYA")}

    def set_env(**kw):
        for k in ("DIP_EVENTS", "DIP_PASSIVES", "DIP_EXILE", "DIP_LAYA"):
            os.environ[k] = kw.get(k, "1")

    try:
        set_env(DIP_EVENTS="0")
        g = fresh_game()
        ev = get_mechanics_manager(g).events
        check("DIP_EVENTS=0 不抽牌", ev.draw_event(g, GameHistory(), "S1939M") is None)
        check("DIP_EVENTS=0 无事件块",
              get_mechanics_manager(g).events.context_block_for_power(g, "USA", {}, "S1939M") == "")

        set_env(DIP_PASSIVES="0")
        g = fresh_game()
        add_unit(g, "FRANCE", "A PAR"); add_unit(g, "GERMANY", "A BUR")
        kept, forb = filter_mechanics_orders(g, {"GERMANY": ["A BUR - PAR"]}, "S1939M")
        check("DIP_PASSIVES=0 被动过滤关闭", not forb)

        set_env(DIP_EXILE="0")
        g = fresh_game()
        wipe(g, "FRANCE")
        reg = get_mechanics_manager(g).exile
        reg.detect_triggers(g, "S1939M")
        check("DIP_EXILE=0 不触发流亡", not reg.seeking and not is_active(g, "FRANCE"))

        set_env(DIP_LAYA="0")
        director = laya_director.LayaDirector()
        choice, _, source = director.choose("s", "t", "i", {"A": "a", "B": "b"}, random.Random(0))
        check("DIP_LAYA=0 退回随机", source == "random")

        set_env()  # 全开
        g = fresh_game()
        check("开关全开恢复正常", get_mechanics_manager(g).events.draw_event(g, GameHistory(), "S1939M") is not None)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------- 9. 随机对局冒烟
def test_random_smoke():
    section("9. 随机对局冒烟（10 阶段）")
    from ai_diplomacy.mechanics import is_adjustment_phase, is_spring_phase

    def run_game(switches, seed):
        for k, v in switches.items():
            os.environ[k] = v
        os.environ["DIP_MECHANICS_SEED"] = str(seed)
        rng = random.Random(seed)
        g = fresh_game()
        hist = GameHistory()
        mgr = get_mechanics_manager(g)
        phases = 0
        while phases < 10 and not g.is_game_done:
            phase = g.get_current_phase()
            hist.add_phase(phase)
            short = g.current_short_phase
            if short.endswith("M"):
                mgr.events.begin_phase(g, phase)
                if is_spring_phase(phase):
                    mgr.events.draw_event(g, hist, phase)
            if is_adjustment_phase(phase):
                mgr.exile.begin_winter(g, phase)
            poss = g.get_all_possible_orders()
            orders = {}
            for name in g.powers:
                orders[name] = [rng.choice(poss[loc]) for loc in g.get_orderable_locations(name)
                                if poss.get(loc)]
            if short.endswith("M"):
                orders, _ = filter_mechanics_orders(g, orders, phase, mgr.events.active_filter_effect())
            for name, o in orders.items():
                g.set_orders(name, o)
            completed = phase
            g.process()
            mgr.exile.detect_triggers(g, completed)
            mgr.exile.check_hosts_down(g, completed)
            mgr.exile.check_restorations(g, completed)
            if short.endswith("M"):
                mgr.exile.end_movement_phase(g, completed)
            if is_adjustment_phase(phase):
                mgr.exile.complete_winter(g, completed)
            phases += 1
        return phases, g

    old = {k: os.environ.get(k) for k in ("DIP_EVENTS", "DIP_PASSIVES", "DIP_EXILE", "DIP_LAYA")}
    try:
        n, g = run_game({"DIP_EVENTS": "1", "DIP_PASSIVES": "1", "DIP_EXILE": "1", "DIP_LAYA": "0"}, 7)
        check("全开冒烟 10 阶段无崩溃", n == 10, f"停在 {g.get_current_phase()}")
        n, g = run_game({"DIP_EVENTS": "0", "DIP_PASSIVES": "0", "DIP_EXILE": "0", "DIP_LAYA": "0"}, 7)
        check("全关冒烟 10 阶段无崩溃", n == 10, f"停在 {g.get_current_phase()}")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_unit_owner_cache():
    """Regression: units moved/added by mechanics must stay orderable.

    The engine builds Game._unit_owner_cache once and never refreshes it on
    its own; mechanics.py unit mutators must invalidate it, otherwise the
    moved/added unit is invisible to Game._unit_owner and ALL of its orders
    are rejected as UNORDERABLE UNIT (trial run: Germany's bonus-moved A BUD
    lost its Fall 1939 support order this way).
    """
    section("10. 单位归属缓存失效回归")
    g = fresh_game()
    # Warm the cache the way a real phase does (any order op builds it).
    g.clear_orders("GERMANY")
    g.set_orders("GERMANY", ["A VIE H"])
    check("预热后缓存已建立", g._unit_owner_cache is not None)
    # Bonus move via the real passive path: A VIE -> BUD (empty neutral SC).
    ok, why = apply_bonus_move(g, "GERMANY", "A VIE", "BUD", [])
    check("奖励移动 VIE->BUD 成功", ok, why)
    check("A BUD 在德国单位列表中", "A BUD" in g.powers["GERMANY"].units)
    # The moved unit must accept orders (hold / move / support).
    g.clear_orders("GERMANY")
    g.set_orders("GERMANY", ["A BUD H"])
    check("移动后单位可下 HOLD 令", g.get_orders("GERMANY") == ["A BUD H"])
    g.clear_orders("GERMANY")
    g.set_orders("GERMANY", ["A BUD - VIE"])
    check("移动后单位可下移动令", g.get_orders("GERMANY") == ["A BUD - VIE"])
    g.clear_orders("GERMANY")
    g.set_orders("GERMANY", ["A BUD S A GAL - VIE"])
    check("移动后单位可下支援令", g.get_orders("GERMANY") == ["A BUD S A GAL - VIE"])
    # Added units (reserves event / exile funding) must be orderable too.
    g2 = fresh_game()
    g2.clear_orders("ITALY")
    g2.set_orders("ITALY", ["A VEN H"])
    check("预备役增援单位可添加", add_unit(g2, "ITALY", "A APU"))
    g2.clear_orders("ITALY")
    g2.set_orders("ITALY", ["A APU H"])
    check("新增单位可下 HOLD 令", g2.get_orders("ITALY") == ["A APU H"])
    # Removed units must NOT stay orderable via a stale cache entry.
    g3 = fresh_game()
    g3.clear_orders("FRANCE")
    g3.set_orders("FRANCE", ["A PAR H"])
    check("移除单位", remove_unit(g3, "FRANCE", "A PAR"))
    g3.clear_orders("FRANCE")
    g3.set_orders("FRANCE", ["A PAR H"])
    check("已移除单位不再可下令", g3.get_orders("FRANCE") == [])


def main():
    test_passives()
    test_events()
    test_laya()
    test_exile()
    test_save_resume()
    test_json_tolerance()
    test_is_active_audit()
    test_env_switches()
    test_random_smoke()
    test_unit_owner_cache()
    print(f"\n全部通过：{len(PASSED)} 个断言")


if __name__ == "__main__":
    main()
