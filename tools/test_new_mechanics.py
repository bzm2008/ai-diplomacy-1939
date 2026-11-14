# 自测新机制（不调用大模型，固定种子）：领袖被动技能、随机事件+裁判、流亡政府、
# 和平期系统（条约/宣称/预备役/阅兵）、傀儡、满意度/分裂（1937 规则）。
# 用法：cd <repo> && PYTHONPATH=. <venv>/bin/python tools/test_new_mechanics.py
# 裁判（Luna）默认不联网：未设置 DIP_JUDGE_MODEL 时走种子随机/公式兜底。
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DIP_LAYA", "0")            # 旧 Laya 开关（已退役，保留无害）
os.environ.setdefault("DIP_MECHANICS_SEED", "42")
os.environ.pop("DIP_JUDGE_MODEL", None)           # 裁判默认走种子随机/公式兜底，不联网

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import logging
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from diplomacy import Game  # noqa: E402
from diplomacy.utils.export import from_saved_game_format  # noqa: E402

from ai_diplomacy import judge as judge_director  # noqa: E402
from ai_diplomacy.judge import JudgeDirector  # noqa: E402
from ai_diplomacy.cards import get_card_manager  # noqa: E402
from ai_diplomacy.events import EVENT_IDS, EventManager  # noqa: E402
from ai_diplomacy.exile import is_active, is_normal_active  # noqa: E402
from ai_diplomacy.game_history import GameHistory  # noqa: E402
from ai_diplomacy.game_logic import save_game_state  # noqa: E402
from ai_diplomacy.mechanics import (  # noqa: E402
    MAJOR_POWERS, MINOR_POWERS, add_unit, get_mechanics_manager,
    judge_enabled, mechanics_context_for_power, move_unit,
    production_bonus_active, remove_unit, speer_miracle_active,
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
    """Deterministic stand-in for JudgeDirector (injected via set_director)."""

    def __init__(self, pick=None, timing="this_phase", confidence=0.9):
        self.pick = pick  # preferred option key, else first key
        self.timing = timing
        self.confidence = confidence
        self.calls = []

    def choose(self, state_summary, question_id, instructions, options, rng):
        self.calls.append(question_id)
        keys = list(options.keys())
        choice = self.pick if self.pick in keys else keys[0]
        return choice, self.confidence, "judge"

    def choose_timing(self, state_summary, rng):
        return self.timing, self.confidence, "judge"


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

    # 美国民主兵工厂 + 租借法案：1941 年起冬季 +2 建造（两项叠加），且超建不被强制解散
    g = fresh_game()
    remove_unit(g, "GERMANY", g.powers["GERMANY"].units[0])  # 制造不平衡，否则引擎跳过冬季
    advance_to(g, "W1941A")
    usa = g.powers["USA"]
    while len(usa.units) > len(usa.centers) - 1:
        remove_unit(g, "USA", usa.units[0])
    raw = len(usa.centers) - len(usa.units)
    check("美国 1941 冬季 +2 建造（兵工厂+租借法案）", g._adjustment_balance(g.powers["USA"]) == raw + 2,
          f"raw={raw} adjusted={g._adjustment_balance(g.powers['USA'])}")
    add_unit(g, "USA", "A NYC") if "A NYC" not in usa.units else None
    add_unit(g, "USA", "F NWA") if len(usa.units) <= len(usa.centers) else None
    occupied = {u.split()[-1].split("/")[0] for p in g.powers.values() for u in p.units}
    empties = [loc for loc in sorted(g.map.loc_type) if g.map.loc_type[loc] != "WATER" and loc not in occupied]
    while len(usa.units) <= len(usa.centers):
        add_unit(g, "USA", f"A {empties.pop(0)}")
    check("美国 1941 超建 +2 抵消后可再建 1 支", g._adjustment_balance(g.powers["USA"]) == 1,
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
            # 截止期不透露：广播里不得出现“截止/到期/最后一年”之类的词
            # （1944 年的抵抗运动公告本身含有“1944 年”，故不能直接查年份数字）
            check(f"施佩尔广播 {ph} {pw} 不透露截止期",
                  "截止" not in block and "到期" not in block and "最后一年" not in block, block[:80])
    block45 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1945M")
    check("施佩尔广播 1945 年消失", "施佩尔奇迹" not in block45, block45[:80])
    check("德国提示词写明截止期",
          "1944" in PASSIVE_TEXTS["GERMANY"] and "施佩尔奇迹" in PASSIVE_TEXTS["GERMANY"])
    check("施佩尔年份判定",
          speer_miracle_active(1942) and speer_miracle_active(1944)
          and not speer_miracle_active(1941) and not speer_miracle_active(1945)
          and not speer_miracle_active(None))

    # 苏联乌拉尔军工 + 英国大西洋胜利：1943-1944 年冬季 +1 建造，1945 年起失效
    for power, winter, expected_bonus in [
        ("SOVIET", "W1942A", 0), ("SOVIET", "W1943A", 1),
        ("SOVIET", "W1944A", 1), ("SOVIET", "W1945A", 1),  # 柏林战役
        ("BRITAIN", "W1942A", 0), ("BRITAIN", "W1943A", 1),
        ("BRITAIN", "W1944A", 1), ("BRITAIN", "W1945A", 0),
    ]:
        g = fresh_game()
        remove_unit(g, "FRANCE", g.powers["FRANCE"].units[0])  # 制造不平衡，否则引擎跳过冬季
        advance_to(g, winter)
        pw = g.powers[power]
        while len(pw.units) > len(pw.centers) - 1:
            remove_unit(g, power, pw.units[0])
        raw = len(pw.centers) - len(pw.units)
        check(f"{power} {winter} 加成 {expected_bonus}",
              g._adjustment_balance(g.powers[power]) == raw + expected_bonus,
              f"raw={raw} adjusted={g._adjustment_balance(g.powers[power])}")
    for power, winter in [("SOVIET", "W1943A"), ("BRITAIN", "W1944A")]:
        g = fresh_game()
        remove_unit(g, "FRANCE", g.powers["FRANCE"].units[0])
        advance_to(g, winter)
        pw = g.powers[power]
        occupied = {u.split()[-1].split("/")[0] for p in g.powers.values() for u in p.units}
        empties = [loc for loc in sorted(g.map.loc_type) if g.map.loc_type[loc] != "WATER" and loc not in occupied]
        while len(pw.units) <= len(pw.centers):
            add_unit(g, power, f"A {empties.pop(0)}")
        check(f"{power} {winter} 超建豁免（balance 不为负）",
              g._adjustment_balance(g.powers[power]) == 0,
              f"units={len(pw.units)} centers={len(pw.centers)}")
    for power in ["SOVIET", "BRITAIN"]:
        g = fresh_game()
        remove_unit(g, "FRANCE", g.powers["FRANCE"].units[0])
        advance_to(g, "W1945A")
        pw = g.powers[power]
        occupied = {u.split()[-1].split("/")[0] for p in g.powers.values() for u in p.units}
        empties = [loc for loc in sorted(g.map.loc_type) if g.map.loc_type[loc] != "WATER" and loc not in occupied]
        while len(pw.units) <= len(pw.centers):
            add_unit(g, power, f"A {empties.pop(0)}")
        expected = 0 if power == "SOVIET" else -1  # 苏联 1945 有柏林战役兜底
        check(f"{power} 1945 年超建结算 {expected}",
              g._adjustment_balance(g.powers[power]) == expected,
              f"units={len(pw.units)} centers={len(pw.centers)}")
    # 新增年度特效药：1939 德国总体战动员、1940 英国敦刻尔克、1944 法国抵抗运动、1945 苏联柏林战役
    for power, winter, expected_bonus in [
        ("GERMANY", "W1939A", 1), ("GERMANY", "W1941A", 0),
        ("BRITAIN", "W1940A", 1), ("BRITAIN", "W1941A", 0),
        ("FRANCE", "W1944A", 1), ("FRANCE", "W1943A", 0),
        ("SOVIET", "W1945A", 1),
    ]:
        g = fresh_game()
        remove_unit(g, "FRANCE", g.powers["FRANCE"].units[0])  # 制造不平衡，否则引擎跳过冬季
        advance_to(g, winter)
        pw = g.powers[power]
        while len(pw.units) > len(pw.centers) - 1:
            remove_unit(g, power, pw.units[0])
        raw = len(pw.centers) - len(pw.units)
        check(f"{power} {winter} 加成 {expected_bonus}",
              g._adjustment_balance(g.powers[power]) == raw + expected_bonus,
              f"raw={raw} adjusted={g._adjustment_balance(g.powers[power])}")

    # 广播：1942 年只有施佩尔，1943 年三项齐全，1944 年四项齐全，1945 年仅柏林战役
    g = fresh_game()
    b42 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "F1942M")
    check("1942 年只有施佩尔广播",
          "施佩尔奇迹" in b42 and "乌拉尔军工" not in b42 and "大西洋胜利" not in b42, b42[:100])
    b43 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1943M")
    check("1943 年三项广播齐全",
          "施佩尔奇迹" in b43 and "乌拉尔军工" in b43 and "大西洋胜利" in b43, b43[:160])
    check("1943 年广播不透露截止期", "1944" not in b43, b43[:160])
    b39 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1939M")
    check("1939 年总体战动员广播", "总体战动员" in b39, b39[:80])
    b40 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1940M")
    check("1940 年敦刻尔克精神广播", "敦刻尔克精神" in b40, b40[:80])
    b41 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1941M")
    check("1941 年租借法案广播", "租借法案" in b41, b41[:80])
    b44 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1944M")
    check("1944 年四项广播齐全",
          "施佩尔奇迹" in b44 and "乌拉尔军工" in b44 and "大西洋胜利" in b44 and "抵抗运动" in b44, b44[:200])
    check("1944 年广播不提次年", "1945" not in b44, b44[:200])
    b45 = mechanics_context_for_power(g, GameHistory(), "FRANCE", {}, "S1945M")
    check("1945 年仅柏林战役广播",
          "施佩尔奇迹" not in b45 and "乌拉尔军工" not in b45 and "大西洋胜利" not in b45
          and "柏林战役" in b45, b45[:80])
    check("苏联提示词写明截止期",
          "1944" in PASSIVE_TEXTS["SOVIET"] and "乌拉尔军工" in PASSIVE_TEXTS["SOVIET"])
    check("英国提示词写明截止期",
          "1944" in PASSIVE_TEXTS["BRITAIN"] and "大西洋胜利" in PASSIVE_TEXTS["BRITAIN"])
    check("生产加成窗口判定",
          production_bonus_active("GERMANY", 1939) and not production_bonus_active("GERMANY", 1941)
          and production_bonus_active("GERMANY", 1942) and production_bonus_active("GERMANY", 1944)
          and not production_bonus_active("GERMANY", 1945)
          and not production_bonus_active("SOVIET", 1942) and production_bonus_active("SOVIET", 1943)
          and production_bonus_active("SOVIET", 1945)
          and production_bonus_active("BRITAIN", 1940) and production_bonus_active("BRITAIN", 1944)
          and not production_bonus_active("BRITAIN", 1945) and not production_bonus_active("BRITAIN", 1941)
          and production_bonus_active("USA", 1941) and not production_bonus_active("USA", 1943)
          and production_bonus_active("FRANCE", 1944) and not production_bonus_active("FRANCE", 1943)
          and not production_bonus_active("SOVIET", None))

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
    # 牌堆：8 张不重不漏，抽完不再重洗（每张整局只出一次）
    mgr = EventManager({"seed": 7})
    drawn = [mgr.draw_card_id() for _ in range(8)]
    check("牌堆 8 张不重不漏", sorted(drawn) == sorted(EVENT_IDS), str(drawn))
    ninth = mgr.draw_card_id()
    check("牌堆抽完不再重洗", ninth is None and len(mgr.deck) == 0)
    mgr_restored = EventManager({"seed": 7, "deck": [], "drawn": [{"card_id": c} for c in EVENT_IDS]})
    check("复原空牌堆不重洗", mgr_restored.deck == [])
    g_empty = fresh_game()
    ev_empty = get_mechanics_manager(g_empty).events
    ev_empty.deck = []
    check("牌堆空时 draw_event 返回 None", ev_empty.draw_event(g_empty, GameHistory(), "S1939M") is None)

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
    judge_director.set_director(fake)
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
        judge_director.set_director(None)

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
    judge_director.set_director(fake)
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
        judge_director.set_director(None)

    # 外交风波：两人成对、互不相同、西班牙不在内
    g = fresh_game()
    judge_director.set_director(FakeDirector())
    try:
        ev = get_mechanics_manager(g).events
        ev.deck = ["feud"] + [c for c in ev.deck if c != "feud"]
        rec = ev.draw_event(g, GameHistory(), "S1939M")
        check("外交风波两人成对", len(rec["targets"]) == 2 and len(set(rec["targets"])) == 2,
              str(rec["targets"]))
        check("外交风波排除西班牙", "SPAIN" not in rec["targets"])
        check("外交风波 pending 可查", ev.feud_pair() is not None or ev.pending is not None)
    finally:
        judge_director.set_director(None)

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


# ---------------------------------------------------------------- 3. 裁判
def _stub_judge_client(payload):
    """Fake OpenAI client returning one canned JSON payload."""
    content = json.dumps(payload)
    completions = SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
    )
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _judge_with_stub(payload):
    director = JudgeDirector(model_spec="openai:fake@https://example.invalid/v1#sk-fake")
    director._client = _stub_judge_client(payload)
    director._parsed = ("fake", "https://example.invalid/v1", "sk-fake")
    return director


def test_judge():
    section("3. 裁判（JudgeDirector，Luna）")
    rng = random.Random(1)
    options = {"GERMANY": "g", "FRANCE": "f"}
    old_spec = os.environ.pop("DIP_JUDGE_MODEL", None)

    # 无配置 -> 种子随机/公式兜底，绝不联网
    director = JudgeDirector()
    choice, conf, source = director.choose("state", "target", "pick", options, rng)
    check("裁判无配置退回随机", source == "random" and choice in options, f"{choice}/{source}")
    scores, source = director.score_all("facts", "rubric", ["GERMANY", "FRANCE"],
                                        {"GERMANY": 55, "FRANCE": 60})
    check("裁判无配置满意度走公式",
          source == "formula" and scores == {"GERMANY": (55, "公式兜底"), "FRANCE": (60, "公式兜底")},
          str(scores))

    # 高置信度采用；低置信度/越界退回随机；单选项强制
    director = _judge_with_stub({"choice": "GERMANY", "confidence": 0.95, "reason": "x"})
    choice, conf, source = director.choose("state", "target", "pick", options, random.Random(1))
    check("高置信度采用裁判", source == "judge" and choice == "GERMANY", f"{choice}/{conf}")
    director = _judge_with_stub({"choice": "GERMANY", "confidence": 0.1, "reason": "x"})
    choice, conf, source = director.choose("state", "target", "pick", options, random.Random(1))
    check("低置信度退回随机", source == "random", f"conf={conf}")
    director = _judge_with_stub({"choice": "SPAIN", "confidence": 0.95, "reason": "x"})
    choice, conf, source = director.choose("state", "target", "pick", options, random.Random(1))
    check("越界选择退回随机", source == "random" and choice in options, choice)
    director = _judge_with_stub({"choice": "ONLY", "confidence": 0.9, "reason": "x"})
    choice, conf, source = director.choose("state", "t", "pick", {"ONLY": "o"}, random.Random(1))
    check("单选项强制", (choice, source) == ("ONLY", "forced"))

    # score_all：钳制 0-100、缺项混合、结构异常走公式
    director = _judge_with_stub({"scores": {"GERMANY": {"score": 150, "reason": "r"},
                                            "FRANCE": {"score": -5, "reason": "r"}}})
    scores, source = director.score_all("facts", "rubric", ["GERMANY", "FRANCE"],
                                        {"GERMANY": 55, "FRANCE": 60})
    check("满意度钳制 0-100", source == "judge" and scores["GERMANY"][0] == 100
          and scores["FRANCE"][0] == 0, str(scores))
    director = _judge_with_stub({"scores": {"GERMANY": {"score": 70, "reason": "r"}}})
    scores, source = director.score_all("facts", "rubric", ["GERMANY", "FRANCE"],
                                        {"GERMANY": 55, "FRANCE": 60})
    check("满意度缺项走混合兜底", source == "mixed" and scores["GERMANY"][0] == 70
          and scores["FRANCE"] == (60, "公式兜底"), str(scores))
    director = _judge_with_stub({"not_scores": True})
    scores, source = director.score_all("facts", "rubric", ["GERMANY"], {"GERMANY": 55})
    check("满意度结构异常走公式", source == "formula" and scores["GERMANY"][0] == 55, str(scores))

    # DIP_JUDGE=0 时即使客户端可用也退回随机
    old_judge = os.environ.get("DIP_JUDGE")
    os.environ["DIP_JUDGE"] = "0"
    try:
        director = _judge_with_stub({"choice": "GERMANY", "confidence": 0.95, "reason": "x"})
        choice, conf, source = director.choose("state", "target", "pick", options, random.Random(1))
        check("DIP_JUDGE=0 退回随机", source == "random", f"{choice}/{source}")
    finally:
        if old_judge is None:
            os.environ.pop("DIP_JUDGE", None)
        else:
            os.environ["DIP_JUDGE"] = old_judge
    if old_spec is not None:
        os.environ["DIP_JUDGE_MODEL"] = old_spec

    # 硬约束：导演即使点名西班牙，抽牌也不会选中西班牙（选项集合已排除）
    g = fresh_game()
    judge_director.set_director(FakeDirector(pick="SPAIN"))
    try:
        ev = get_mechanics_manager(g).events
        ev.deck = ["assassination"] + [c for c in ev.deck if c != "assassination"]
        rec = ev.draw_event(g, GameHistory(), "S1939M")
        check("硬约束挡住西班牙", rec["targets"] != ["SPAIN"], str(rec["targets"]))
    finally:
        judge_director.set_director(None)

    # mock 注入：draw_event 走注入的导演
    g = fresh_game()
    fake = FakeDirector(pick="ITALY", timing="next_movement")
    judge_director.set_director(fake)
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
        judge_director.set_director(None)

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
    old = {k: os.environ.get(k) for k in ("DIP_EVENTS", "DIP_PASSIVES", "DIP_EXILE", "DIP_LAYA", "DIP_JUDGE")}

    def set_env(**kw):
        for k in ("DIP_EVENTS", "DIP_PASSIVES", "DIP_EXILE", "DIP_LAYA", "DIP_JUDGE"):
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

        set_env(DIP_JUDGE="0")
        director = JudgeDirector()
        choice, _, source = director.choose("s", "t", "i", {"A": "a", "B": "b"}, random.Random(0))
        check("DIP_JUDGE=0 退回随机", source == "random")

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

    old = {k: os.environ.get(k) for k in ("DIP_EVENTS", "DIP_PASSIVES", "DIP_EXILE", "DIP_LAYA", "DIP_JUDGE")}
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


# ---------------------------------------------------------------------------
# 1937 系统：地图、和平期、条约/宣称、预备役/阅兵、傀儡、满意度/分裂
# ---------------------------------------------------------------------------

def fresh_game_1937():
    g = Game(map_name="europe1937")
    get_card_manager(g)
    get_mechanics_manager(g)
    return g


class FakeJudge:
    """Deterministic stand-in for JudgeDirector choosing + scoring."""

    def __init__(self, scores=None, pick=None, timing="this_phase", confidence=0.9):
        self.scores = scores  # None -> formula fallback for every power
        self.pick = pick
        self.timing = timing
        self.confidence = confidence

    def choose(self, state_summary, question_id, instructions, options, rng):
        keys = list(options.keys())
        choice = self.pick if self.pick in keys else keys[0]
        return choice, self.confidence, "judge"

    def choose_timing(self, state_summary, rng):
        return self.timing, self.confidence, "judge"

    def score_all(self, facts_text, rubric_text, powers, fallback_scores):
        if self.scores is None:
            return {p: (int(fallback_scores.get(p, 60)), "公式兜底") for p in powers}, "formula"
        out = {}
        for p in powers:
            if p in self.scores:
                out[p] = (self.scores[p], "fake judge")
            else:
                out[p] = (int(fallback_scores.get(p, 60)), "公式兜底")
        return out, "judge"


def _snapshot(g):
    return {
        name: {"centers": {str(c).upper()[:3] for c in pw.centers}, "units": len(pw.units)}
        for name, pw in g.powers.items()
    }


def _centers_of(g, power):
    return {str(c).upper()[:3] for c in g.powers[power].centers}


def test_1937_map():
    section("11. 1937 地图（16 国 + 4 预备）")
    g = fresh_game_1937()
    check("1937 地图 20 个国家", len(g.powers) == 20, str(sorted(g.powers)))
    check("1937 胜利线 31", g.win == 31, str(g.win))
    owned = sum(len(pw.centers) for pw in g.powers.values())
    check("1937 已占中心 48", owned == 48, str(owned))
    unowned = {"VIE", "BOH", "LVN", "ALB", "FIN", "SWE", "BUD", "BUL", "SER", "TRI", "TYR", "SYR", "TUN"}
    all_owned = set().union(*[_centers_of(g, name) for name in g.powers])
    check("1937 无主 13 省", not (unowned & all_owned), str(unowned & all_owned))
    check("1937 开局 S1937M", g.get_current_phase() == "S1937M", g.get_current_phase())
    check("德国 3 支部队无维也纳部队",
          len(g.powers["GERMANY"].units) == 3 and "A VIE" not in g.powers["GERMANY"].units)
    check("德国本土含西里西亚", "SIL" in _centers_of(g, "GERMANY"))
    check("美国 3 支舰队", len(g.powers["USA"].units) == 3
          and all(str(u).startswith("F ") for u in g.powers["USA"].units))
    for minor, centers in [("PORTUGAL", 2), ("HOLLAND", 1), ("BELGIUM", 1), ("NORWAY", 1),
                           ("DENMARK", 2), ("GREECE", 1), ("ROMANIA", 1), ("TURKEY", 3)]:
        check(f"小国 {minor} 0 部队 {centers} 中心",
              len(g.powers[minor].units) == 0 and len(g.powers[minor].centers) == centers)
    for reserve in ["SPLIT_A", "SPLIT_B", "SPLIT_C", "SPLIT_D"]:
        check(f"预备国 {reserve} 开局淘汰", not is_active(g, reserve))
    run_holds(g, 1)
    check("全驻守推进到 F1937M", g.get_current_phase() == "F1937M", g.get_current_phase())
    from ai_diplomacy.cards import CARD_EFFECTS, UPGRADE_MODELS
    check("西班牙集群牌", "es_cluster" in CARD_EFFECTS and "es_upgrade" not in CARD_EFFECTS)
    check("升格表无西班牙", "es_upgrade" not in UPGRADE_MODELS)
    check("大小国划分 8+8", len(MAJOR_POWERS) == 8 and len(MINOR_POWERS) == 8)


def test_peace_filters():
    section("12. 和平期过滤 + 驻军过滤")
    g = fresh_game_1937()
    orders = {
        "GERMANY": ["A BER - PRU", "A MUN - BOH", "F KIE H"],
        "FRANCE": ["A MAR - PIE", "A PAR H", "F BRE H"],
        "ITALY": ["A VEN - TRI", "A ROM H", "F NAP H"],
    }
    kept, forb = filter_mechanics_orders(g, orders, "S1937M", None)
    check("和平期拦进攻波兰中心", "A BER - PRU" in forb.get("GERMANY", []), str(forb))
    check("和平期放行进无主省", "A MUN - BOH" in kept["GERMANY"], str(kept))
    check("和平期拦进攻意大利中心", "A MAR - PIE" in forb.get("FRANCE", []), str(forb))
    check("和平期放行进无主的里雅斯特", "A VEN - TRI" in kept["ITALY"], str(kept))
    move_unit(g, "GERMANY", "A MUN", "BOH")
    kept, forb = filter_mechanics_orders(g, {"ITALY": ["A VEN - BOH"]}, "S1937M", None)
    check("和平期拦进有外国部队的省", "A VEN - BOH" in forb.get("ITALY", []), str(forb))
    kept, forb = filter_mechanics_orders(g, {"FRANCE": ["A PAR S A MAR - PIE"]}, "S1937M", None)
    check("和平期放行支援", "A PAR S A MAR - PIE" in kept["FRANCE"], str(kept))

    g2 = fresh_game_1937()
    advance_to(g2, "S1939M")
    kept, forb = filter_mechanics_orders(g2, {"FRANCE": ["A MAR - PIE"]}, "S1939M", None)
    check("1939 年和平期过滤关闭", "A MAR - PIE" in kept["FRANCE"], str(forb))
    mgr2 = get_mechanics_manager(g2)
    expect_garrison = {"POR", "AZO", "HOL", "BEL", "NWY", "DEN", "ICE", "GRE", "RUM", "CON", "SMY", "ANK"}
    check("驻军初始为 12 个小国本土中心",
          mgr2.garrisoned_provinces(g2) == expect_garrison, str(mgr2.garrisoned_provinces(g2)))
    kept, forb = filter_mechanics_orders(g2, {"GERMANY": ["F KIE - DEN"]}, "S1939M", None)
    check("驻军拦无支援进攻", "F KIE - DEN" in forb.get("GERMANY", []), str(forb))
    kept, forb = filter_mechanics_orders(
        g2, {"GERMANY": ["F KIE - DEN", "A BER S F KIE - DEN"]}, "S1939M", None)
    check("驻军放行有支援进攻", "F KIE - DEN" in kept["GERMANY"], str(forb))
    move_unit(g2, "GERMANY", "F KIE", "DEN")
    mgr2.ungarrison_occupied(g2)
    check("被占驻军省解除驻军", "DEN" not in mgr2.garrisoned_provinces(g2))
    kept, forb = filter_mechanics_orders(g2, {"FRANCE": ["A PAR - DEN"]}, "S1939M", None)
    check("解除驻军后放行进攻", "A PAR - DEN" in kept["FRANCE"], str(forb))


def test_treaties_claims():
    section("13. 条约 / 宣称 / 积怨 / 联合进攻")
    g = fresh_game_1937()
    mgr = get_mechanics_manager(g)
    peace = mgr.peace
    peace.record_fields(g, "GERMANY", {"treaty_offer": {"target": "SOVIET", "type": "public"}}, mgr)
    peace.record_fields(g, "SOVIET", {"treaty_accept": {"target": "GERMANY", "type": "public"}}, mgr)
    peace.resolve_pending(g, "S1937M")
    check("公开条约缔结", peace.treaties.treaty_between("GERMANY", "SOVIET") is not None)
    check("公开条约广播", any("GERMANY" in a and "SOVIET" in a
                            for a in peace.treaties.announcements.get("S1937M", [])))
    mgr.satisfaction.scores["GERMANY"] = 60
    peace.treaties.check_betrayals(
        [{"attacker": "GERMANY", "dest": "WAR", "owner": "SOVIET", "occupant": None}],
        "S1939M", mgr.satisfaction)
    check("背叛扣 15 分", mgr.satisfaction.scores["GERMANY"] == 45,
          str(mgr.satisfaction.scores["GERMANY"]))
    treaty = peace.treaties.treaty_between("GERMANY", "SOVIET")
    check("背叛解散条约", treaty is None or not treaty.get("active"))
    check("背叛广播", any("背弃" in a for a in peace.treaties.announcements.get("S1939M", [])))

    # 小国/分裂政权只能接受条约，不能主动发起
    peace.record_fields(g, "PORTUGAL", {"treaty_offer": {"target": "BRITAIN", "type": "public"}}, mgr)
    check("小国主动缔约被忽略", "BRITAIN" not in peace.treaties.offers.get("PORTUGAL", {}))
    peace.record_fields(g, "SPLIT_A", {"treaty_offer": {"target": "BRITAIN", "type": "public"}}, mgr)
    check("分裂政权主动缔约被忽略", "BRITAIN" not in peace.treaties.offers.get("SPLIT_A", {}))
    peace.record_fields(g, "BRITAIN", {"treaty_offer": {"target": "PORTUGAL", "type": "public"}}, mgr)
    peace.record_fields(g, "PORTUGAL", {"treaty_accept": {"target": "BRITAIN", "type": "public"}}, mgr)
    peace.resolve_pending(g, "S1937M")
    check("小国可接受条约", peace.treaties.treaty_between("BRITAIN", "PORTUGAL") is not None)

    # 密约：背叛无声作废；联合进攻则曝光
    n_ann_before = len(peace.treaties.announcements.get("S1937M", []))
    peace.record_fields(g, "GERMANY", {"treaty_offer": {"target": "ITALY", "type": "secret"}}, mgr)
    peace.record_fields(g, "ITALY", {"treaty_accept": {"target": "GERMANY", "type": "secret"}}, mgr)
    peace.resolve_pending(g, "S1937M")
    check("密约缔结不广播",
          len(peace.treaties.announcements.get("S1937M", [])) == n_ann_before)
    mgr.satisfaction.scores["GERMANY"] = 60
    n_betray_before = len([a for a in peace.treaties.announcements.get("S1939M", []) if "背弃" in a])
    peace.treaties.check_betrayals(
        [{"attacker": "GERMANY", "dest": "VEN", "owner": "ITALY", "occupant": None}],
        "S1939M", mgr.satisfaction)
    check("密约背叛不扣分", mgr.satisfaction.scores["GERMANY"] == 60)
    n_betray_after = len([a for a in peace.treaties.announcements.get("S1939M", []) if "背弃" in a])
    check("密约背叛无新增广播", n_betray_after == n_betray_before,
          f"{n_betray_before}->{n_betray_after}")
    peace.record_fields(g, "FRANCE", {"treaty_offer": {"target": "BRITAIN", "type": "secret"}}, mgr)
    peace.record_fields(g, "BRITAIN", {"treaty_accept": {"target": "FRANCE", "type": "secret"}}, mgr)
    peace.resolve_pending(g, "S1937M")
    revealed = peace.treaties.check_joint_attacks(
        [{"attacker": "FRANCE", "dest": "HOL", "owner": "HOLLAND"},
         {"attacker": "BRITAIN", "dest": "HOL", "owner": "HOLLAND"}], "S1939M")
    check("联合进攻曝光密约", len(revealed) == 1
          and peace.treaties.treaty_between("FRANCE", "BRITAIN").get("revealed"))

    # 宣称：上限 3、只要无主省、覆盖式
    peace.record_fields(g, "GERMANY", {"claims": ["BOH", "VIE", "TYR"]}, mgr)
    peace.resolve_pending(g, "S1937M")
    check("宣称登记 3 省", peace.claims.claims_of("GERMANY") == ["BOH", "VIE", "TYR"],
          str(peace.claims.claims_of("GERMANY")))
    peace.record_fields(g, "GERMANY", {"claims": ["BOH", "VIE", "TYR", "SIL", "PAR"]}, mgr)
    peace.resolve_pending(g, "S1937M")
    check("宣称上限 3 且只要无主省", peace.claims.claims_of("GERMANY") == ["BOH", "VIE", "TYR"],
          str(peace.claims.claims_of("GERMANY")))

    # 积怨：侵入记账；雪耻 +5；过期 -5
    add_unit(g, "FRANCE", "A BOH")
    created = peace.grievances.check_claim_intrusions(g, "S1937M", peace.claims)
    check("宣称被侵记积怨", len(created) == 1 and created[0]["claimant"] == "GERMANY"
          and created[0]["intruder"] == "FRANCE", str(created))
    mgr.satisfaction.scores["GERMANY"] = 60
    peace.grievances.evaluate(
        [{"attacker": "GERMANY", "dest": "PAR", "owner": "FRANCE", "occupant": "FRANCE"}],
        "F1939M", mgr.satisfaction)
    check("雪耻 +5", mgr.satisfaction.scores["GERMANY"] == 65,
          str(mgr.satisfaction.scores["GERMANY"]))
    remove_unit(g, "FRANCE", "A BOH")  # 第一笔积怨已报，法军撤出波希米亚
    add_unit(g, "FRANCE", "A VIE")
    peace.grievances.check_claim_intrusions(g, "S1937M", peace.claims)
    mgr.satisfaction.scores["GERMANY"] = 60
    peace.grievances.evaluate([], "F1941M", mgr.satisfaction)
    check("积怨过期 -5", mgr.satisfaction.scores["GERMANY"] == 55,
          str(mgr.satisfaction.scores["GERMANY"]))

    # 联合进攻 +1：真实裁决，条约让苏联拿下华沙
    def _war_game(with_treaty):
        gg = fresh_game_1937()
        mm = get_mechanics_manager(gg)
        if with_treaty:
            mm.peace.record_fields(gg, "GERMANY", {"treaty_offer": {"target": "SOVIET", "type": "public"}}, mm)
            mm.peace.record_fields(gg, "SOVIET", {"treaty_accept": {"target": "GERMANY", "type": "public"}}, mm)
            mm.peace.resolve_pending(gg, "S1937M")
        advance_to(gg, "S1939M")
        move_unit(gg, "GERMANY", "A BER", "SIL")  # SIL 与 WAR 相邻，BER 不相邻
        orders = hold_orders(gg)
        orders["GERMANY"] = ["A SIL - WAR", "A MUN H", "F KIE H"]
        orders["SOVIET"] = ["A UKR - WAR", "A MOS S A UKR - WAR", "F SEV H", "F STP/SC H", "A ARM H"]
        orders["POLAND"] = ["A WAR H", "A PRU S A WAR H", "A GAL H"]
        for name, ords in orders.items():
            gg.set_orders(name, ords)
        gg.process()
        return gg

    gg = _war_game(with_treaty=True)
    check("联合进攻 +1 拿下华沙",
          any(str(u).split()[-1] == "WAR" for u in gg.powers["SOVIET"].units),
          str(gg.powers["SOVIET"].units))
    gg2 = _war_game(with_treaty=False)
    check("无条约时华沙弹回",
          any(str(u).split()[-1] == "WAR" for u in gg2.powers["POLAND"].units),
          str(gg2.powers["POLAND"].units))


def test_reserves_parades():
    section("14. 预备役储备 + 阅兵")
    g = fresh_game_1937()
    bank = get_mechanics_manager(g).peace.reserves
    check("和平冬存 2 个名额", bank.bank_unused("GERMANY", 2, 2, "W1937A") == 2)
    check("储备上限 2", bank.bank_unused("GERMANY", 1, 2, "W1937A") == 0
          and bank.balance_of("GERMANY") == 2)
    check("战时冬季不存", bank.bank_unused("FRANCE", 1, 1, "W1939A") == 0)
    bank.begin_winter("W1938A")
    check("和平冬不自动动用", bank.spend_bonus("GERMANY") == 0 and bank.balance_of("GERMANY") == 2)
    bank.begin_winter("W1939A")
    check("1939 冬自动动用 1 个", bank.spend_bonus("GERMANY") == 1 and bank.balance_of("GERMANY") == 1)
    bank.complete_winter()
    check("冬季结束清空动用", bank.spend_bonus("GERMANY") == 0)
    bank.begin_winter("W1940A")
    check("1940 冬再动用 1 个", bank.spend_bonus("GERMANY") == 1 and bank.balance_of("GERMANY") == 0)
    bank.complete_winter()

    # 阅兵：须 >= 4 支、每年一次、仅和平期
    g2 = fresh_game_1937()
    mgr2 = get_mechanics_manager(g2)
    mgr2.peace.record_fields(g2, "GERMANY", {"parade": True}, mgr2)
    mgr2.peace.resolve_pending(g2, "S1937M")
    check("3 支部队阅兵被拒", not mgr2.peace.parades.parade_held("GERMANY", "S1937M"))
    add_unit(g2, "GERMANY", "A SIL")
    mgr2.peace.record_fields(g2, "GERMANY", {"parade": True}, mgr2)
    mgr2.peace.resolve_pending(g2, "S1937M")
    check("4 支部队阅兵成功", mgr2.peace.parades.parade_held("GERMANY", "S1937M"))
    check("阅兵广播", any("阅兵" in a or "演习" in a
                        for a in mgr2.peace.parades.announcements.get("S1937M", [])))
    mgr2.peace.record_fields(g2, "GERMANY", {"parade": True}, mgr2)
    mgr2.peace.resolve_pending(g2, "F1937M")
    check("同年阅兵只记一次", mgr2.peace.parades.parades.get("GERMANY") == [1937])
    advance_to(g2, "S1939M")
    mgr2.peace.record_fields(g2, "GERMANY", {"parade": True}, mgr2)
    mgr2.peace.resolve_pending(g2, "S1939M")
    check("战时阅兵被拒", mgr2.peace.parades.parades.get("GERMANY") == [1937])


def test_puppets():
    section("15. 傀儡系统")
    g = fresh_game_1937()
    mgr = get_mechanics_manager(g)
    pup = mgr.puppets
    pup.record_fields(g, "PORTUGAL", {"puppet_request": {"overlord": "BRITAIN"}})
    pup.record_fields(g, "BRITAIN", {"puppet_decisions": {"PORTUGAL": True}})
    pup.resolve_pending(g, "S1937M")
    check("傀儡成立", pup.overlord_of("PORTUGAL") == "BRITAIN")
    check("傀儡广播", any("PORTUGAL" in a and "BRITAIN" in a
                        for a in pup.announcements.get("S1937M", [])))
    check("傀儡只能与宗主国私信",
          pup.can_message("PORTUGAL", "BRITAIN") and not pup.can_message("PORTUGAL", "FRANCE")
          and not pup.can_message("FRANCE", "PORTUGAL") and pup.can_message("FRANCE", "GERMANY"))
    from ai_diplomacy.negotiations import _can_send_global, _is_minor_power
    check("小国通信限制判定",
          _is_minor_power("PORTUGAL") and not _can_send_global("PORTUGAL")
          and not _is_minor_power("BRITAIN") and _can_send_global("BRITAIN")
          and _is_minor_power("SPLIT_A") and not _is_minor_power("ENGLAND"))

    # 指挥：上限一半向上取整、延迟一阶段、越界丢弃
    add_unit(g, "PORTUGAL", "A POR")
    add_unit(g, "PORTUGAL", "F AZO")
    add_unit(g, "PORTUGAL", "A BOH")
    pup.record_directives(g, "BRITAIN",
                          {"PORTUGAL": ["A POR - SPA", "F AZO H", "A BOH - VIE", "A XXX H"]},
                          "S1937M")
    check("指挥上限 2 支（3 支部队一半向上取整）",
          len(pup.directives.get("PORTUGAL", {}).get("orders", {})) == 2,
          str(pup.directives.get("PORTUGAL")))
    check("同阶段指挥不生效", pup.consume_directives(g, "S1937M") == {})
    ready = pup.consume_directives(g, "F1937M")
    check("下阶段指挥生效", set(ready.get("PORTUGAL", {})) == {"POR", "AZO"}, str(ready))
    check("指挥消费后清空", pup.directives == {})
    pup.record_directives(g, "FRANCE", {"PORTUGAL": ["A POR H"]}, "S1937M")
    check("非宗主国指挥被拒", pup.directives == {})

    # 指挥覆盖：过滤器用宗主国指令替换傀儡自己的指令
    g2 = fresh_game_1937()
    mgr2 = get_mechanics_manager(g2)
    pup2 = mgr2.puppets
    pup2.record_fields(g2, "PORTUGAL", {"puppet_request": {"overlord": "BRITAIN"}})
    pup2.record_fields(g2, "BRITAIN", {"puppet_decisions": {"PORTUGAL": True}})
    pup2.resolve_pending(g2, "S1937M")
    add_unit(g2, "PORTUGAL", "A POR")
    advance_to(g2, "F1939M")
    pup2.record_directives(g2, "BRITAIN", {"PORTUGAL": ["A POR - SPA"]}, "S1939M")
    kept, forb = filter_mechanics_orders(g2, {"PORTUGAL": ["A POR H"]}, "F1939M", None)
    check("宗主国指令覆盖傀儡指令", kept["PORTUGAL"] == ["A POR - SPA"], str(kept))

    # 脱离 + 冷静期
    pup.record_fields(g, "PORTUGAL", {"break_free": True})
    pup.resolve_pending(g, "S1937M")
    check("春季脱离成功", pup.overlord_of("PORTUGAL") is None)
    pup.record_fields(g, "PORTUGAL", {"puppet_request": {"overlord": "BRITAIN"}})
    pup.record_fields(g, "BRITAIN", {"puppet_decisions": {"PORTUGAL": True}})
    pup.resolve_pending(g, "F1937M")
    check("冷静期内不能再投", pup.overlord_of("PORTUGAL") is None)

    # 傀儡加成：每傀儡每冬 +1，上限 +2
    g3 = fresh_game_1937()
    mgr3 = get_mechanics_manager(g3)
    for minor in ["PORTUGAL", "NORWAY"]:
        mgr3.puppets.record_fields(g3, minor, {"puppet_request": {"overlord": "BRITAIN"}})
        mgr3.puppets.record_fields(g3, "BRITAIN", {"puppet_decisions": {minor: True}})
    mgr3.puppets.resolve_pending(g3, "S1937M")
    check("英国收 2 个傀儡", len(mgr3.puppets.puppets_of("BRITAIN")) == 2)
    mgr3.puppets.record_fields(g3, "DENMARK", {"puppet_request": {"overlord": "BRITAIN"}})
    mgr3.puppets.record_fields(g3, "BRITAIN", {"puppet_decisions": {"DENMARK": True}})
    mgr3.puppets.resolve_pending(g3, "S1937M")
    check("傀儡上限 2 个（第 3 个被拒）", len(mgr3.puppets.puppets_of("BRITAIN")) == 2
          and mgr3.puppets.overlord_of("DENMARK") is None)
    remove_unit(g3, "FRANCE", g3.powers["FRANCE"].units[0])
    advance_to(g3, "W1939A")
    bri = g3.powers["BRITAIN"]
    while len(bri.units) > len(bri.centers) - 1:
        remove_unit(g3, "BRITAIN", bri.units[0])
    raw = len(bri.centers) - len(bri.units)
    check("傀儡加成上限 +2", g3._adjustment_balance(bri) == raw + 2,
          f"raw={raw} adjusted={g3._adjustment_balance(bri)}")

    # 宗主国倒下 / 傀儡亡国
    wipe(g3, "BRITAIN")
    freed = mgr3.puppets.check_overlords_down(g3, "S1939M")
    check("宗主国倒下释放傀儡", len(freed) == 2 and mgr3.puppets.puppets_of("BRITAIN") == [])
    g4 = fresh_game_1937()
    mgr4 = get_mechanics_manager(g4)
    mgr4.puppets.record_fields(g4, "GREECE", {"puppet_request": {"overlord": "ITALY"}})
    mgr4.puppets.record_fields(g4, "ITALY", {"puppet_decisions": {"GREECE": True}})
    mgr4.puppets.resolve_pending(g4, "S1937M")
    wipe(g4, "GREECE")
    gone = mgr4.puppets.check_puppet_losses(g4, "S1937M")
    check("傀儡亡国关系终止", gone == ["GREECE"])


def test_satisfaction():
    section("16. 满意度 / 分裂")
    g = fresh_game_1937()
    mgr = get_mechanics_manager(g)
    sat = mgr.satisfaction
    check("满意度初始 60/55", sat.score_of("GERMANY") == 60 and sat.score_of("PORTUGAL") == 55)
    check("建造修正阈值",
          sat.build_modifier("GERMANY") == 0
          and (sat.scores.__setitem__("GERMANY", 85) or sat.build_modifier("GERMANY") == 1)
          and (sat.scores.__setitem__("GERMANY", 39) or sat.build_modifier("GERMANY") == -1)
          and (sat.scores.__setitem__("GERMANY", 60) or True))
    sat.scores["GERMANY"] = 39
    check("39 分以下全军驻守", sat.hold_only("GERMANY"))
    sat.scores["GERMANY"] = 40
    check("40 分不驻守", not sat.hold_only("GERMANY"))
    sat.scores["GERMANY"] = 60

    # 评分钳制：单阶段浮动 ±15
    judge = FakeJudge(scores={"GERMANY": 90})
    sat.score_phase(g, "S1937M", _snapshot(g), [], mgr, judge, random.Random(1))
    check("评分上浮钳制 +15", sat.scores["GERMANY"] == 75, str(sat.scores["GERMANY"]))
    judge = FakeJudge(scores={"GERMANY": 10})
    sat.score_phase(g, "F1937M", _snapshot(g), [], mgr, judge, random.Random(1))
    check("评分下浮钳制 -15", sat.scores["GERMANY"] == 60, str(sat.scores["GERMANY"]))
    check("症状文本生成", bool(sat.latest_symptoms_text()))

    # 公式兜底：夺一中心 +3
    snap = _snapshot(g)
    snap["GERMANY"]["centers"] = snap["GERMANY"]["centers"] - {"SIL"}
    sat.scores["GERMANY"] = 60
    sat.score_phase(g, "S1938M", snap, [], mgr, FakeJudge(scores=None), random.Random(1))
    check("公式兜底夺中心 +3", sat.scores["GERMANY"] == 63, str(sat.scores["GERMANY"]))

    # 非移动阶段不评分
    before = dict(sat.scores)
    sat.score_phase(g, "W1938A", _snapshot(g), [], mgr, FakeJudge(scores={"GERMANY": 90}), random.Random(1))
    check("冬季不评分", sat.scores == before)

    # 分裂：连续两阶段 <20，分出预备国
    g2 = fresh_game_1937()
    mgr2 = get_mechanics_manager(g2)
    sat2 = mgr2.satisfaction
    sat2.scores["GERMANY"] = 15
    sat2.low_streak["GERMANY"] = 1
    sat2.score_phase(g2, "S1937M", _snapshot(g2), [], mgr2,
                     FakeJudge(scores={"GERMANY": 15}, pick="MUN"), random.Random(1))
    check("分裂出预备国", "MUN" in _centers_of(g2, "SPLIT_A")
          and any(str(u) == "A MUN" for u in g2.powers["SPLIT_A"].units),
          str(g2.powers["SPLIT_A"].centers))
    check("分裂后原国 +15 泄压", sat2.scores["GERMANY"] == 30, str(sat2.scores["GERMANY"]))
    check("分裂后慕尼黑不再属德国", "MUN" not in _centers_of(g2, "GERMANY"))
    check("分裂广播", any("分裂" in a or "独立" in a
                        for a in sat2.announcements.get("S1937M", [])))
    check("分裂国满意度 55", sat2.scores["SPLIT_A"] == 55)
    # 分裂上限 2 次
    sat2.splits_done["GERMANY"] = 2
    sat2.scores["GERMANY"] = 15
    sat2.low_streak["GERMANY"] = 2
    n_before = len(sat2.splits)
    sat2.score_phase(g2, "F1937M", _snapshot(g2), [], mgr2,
                     FakeJudge(scores={"GERMANY": 15}, pick="KIE"), random.Random(1))
    check("分裂上限 2 次", len(sat2.splits) == n_before)

    # 小国动荡：傀儡低满意度可脱离
    g3 = fresh_game_1937()
    mgr3 = get_mechanics_manager(g3)
    mgr3.puppets.record_fields(g3, "PORTUGAL", {"puppet_request": {"overlord": "BRITAIN"}})
    mgr3.puppets.record_fields(g3, "BRITAIN", {"puppet_decisions": {"PORTUGAL": True}})
    mgr3.puppets.resolve_pending(g3, "S1937M")
    sat3 = mgr3.satisfaction
    sat3.scores["PORTUGAL"] = 15
    sat3.low_streak["PORTUGAL"] = 1
    sat3.score_phase(g3, "S1937M", _snapshot(g3), [], mgr3,
                     FakeJudge(scores={"PORTUGAL": 15}, pick="break_free"), random.Random(1))
    check("傀儡动荡脱离", mgr3.puppets.overlord_of("PORTUGAL") is None
          and sat3.scores["PORTUGAL"] == 40, str(sat3.scores["PORTUGAL"]))


def test_minor_cap():
    section("17. 小国建造上限 + 满意度建造修正")
    g = fresh_game_1937()
    advance_to(g, "W1939A")
    check("小国奇数年冬季 0 建造", g._adjustment_balance(g.powers["TURKEY"]) == 0,
          f"centers={len(g.powers['TURKEY'].centers)} units={len(g.powers['TURKEY'].units)}")
    advance_to(g, "W1940A")
    check("小国偶数年冬季最多 1 建造", g._adjustment_balance(g.powers["TURKEY"]) == 1,
          f"centers={len(g.powers['TURKEY'].centers)} units={len(g.powers['TURKEY'].units)}")

    g2 = fresh_game()
    remove_unit(g2, "FRANCE", g2.powers["FRANCE"].units[0])
    advance_to(g2, "W1941A")
    ger = g2.powers["GERMANY"]
    while len(ger.units) > len(ger.centers) - 1:
        remove_unit(g2, "GERMANY", ger.units[0])
    raw = len(ger.centers) - len(ger.units)
    mgr2 = get_mechanics_manager(g2)
    mgr2.satisfaction.scores["GERMANY"] = 85
    check("满意度振奋 +1 建造", g2._adjustment_balance(ger) == raw + 1,
          f"raw={raw} adjusted={g2._adjustment_balance(ger)}")
    mgr2.satisfaction.scores["GERMANY"] = 30
    check("满意度不满 -1 建造", g2._adjustment_balance(ger) == raw - 1,
          f"raw={raw} adjusted={g2._adjustment_balance(ger)}")
    # 满意度驻守过滤
    kept, forb = filter_mechanics_orders(g2, {"GERMANY": ["A BER - PRU", "A MUN H"]}, "S1941M", None)
    check("不满国家只能驻守/支援", "A BER - PRU" in forb.get("GERMANY", [])
          and "A MUN H" in kept["GERMANY"], str(forb))


def main():
    test_passives()
    test_events()
    test_judge()
    test_exile()
    test_save_resume()
    test_json_tolerance()
    test_is_active_audit()
    test_env_switches()
    test_random_smoke()
    test_unit_owner_cache()
    test_1937_map()
    test_peace_filters()
    test_treaties_claims()
    test_reserves_parades()
    test_puppets()
    test_satisfaction()
    test_minor_cap()
    print(f"\n全部通过：{len(PASSED)} 个断言")


if __name__ == "__main__":
    main()
