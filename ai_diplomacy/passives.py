"""Leader passive skills (1939): hard enforcement hooks + shared helpers.

Effects are enforced by code, never left to prompt obedience alone.
All filtering happens on the collected orders of a movement phase, before
``game.set_orders`` (same seam as ``prepare_special_orders``), because the
support-required checks need every power's orders to be known.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from .mechanics import (
    home_centers_of,
    is_fall_phase,
    move_unit,
    passives_enabled,
    phase_year,
    supply_centers_of,
    unit_loc,
    unit_type,
)

logger = logging.getLogger(__name__)

# Sea zones of the Mediterranean (verified WATER provinces on europe1939).
MEDITERRANEAN_SEAS = {"ADR", "AEG", "EAS", "ION", "LYO", "TYS", "WES"}

# Fallback Soviet home provinces; runtime code prefers game.map.homes.
SOVIET_HOMES_FALLBACK = {"MOS", "SEV", "STP", "UKR"}

# Short talent paragraphs injected into the per-power system prompts
# (see ai_diplomacy/prompts_1939/*_system_prompt.txt, section "# 你的天赋").
PASSIVE_TEXTS = {
    "BRITAIN": (
        "你的天赋是“情报嗅觉”：每阶段开始时，你会收到一份外交动态简报，"
        "写明上一阶段每对国家之间的私信数量（只给数量，不给内容）。"
    ),
    "USA": (
        "你的天赋是“民主兵工厂”：1941 年起，每个冬季你可以多建造一支单位；"
        "多建的这一支不会在下个冬季被强制解散。"
    ),
    "ITALY": (
        "你的天赋是“地中海制海权”：在地中海海域内，任何进攻你的舰队的指令"
        "若无支援都会无效（改为驻守）。"
    ),
    "SPAIN": (
        "你的天赋是“置身事外”：你不会成为随机事件的目标，你的私信也不会被"
        "“密电泄露”公开；但扶弱类的“预备役动员”仍可能帮到你。"
    ),
    "FRANCE": (
        "你的天赋是“凡尔登精神”：进攻巴黎的指令若无支援都会无效（改为驻守）。"
    ),
    "GERMANY": (
        "你的天赋是“机动战”：每年春季，你可指定一支陆军在结算后额外移动一格。"
        "目标省必须为空（可进入中立补给中心），不能进入有敌方单位或敌方补给中心"
        "的省，也不能进入本阶段刚被腾出或进入的省。在下单 JSON 里用可选字段 "
        "`\"bonus_move\": {\"unit\": \"A BER\", \"target\": \"SIL\"}` 指定；"
        "不符合条件时该移动会被忽略。"
    ),
    "POLAND": (
        "你的天赋是“流亡先锋”：若你战败待流亡，求助期为 3 个移动阶段"
        "（别国 2 个），且每阶段可向两国求助（别国限一国）。"
    ),
    "SOVIET": (
        "你的天赋是“冬将军”：1941 年起，每个秋季，位于你本土"
        "（MOS/SEV/STP/UKR）的外国陆军只能驻守。"
    ),
}


# ---------------------------------------------------------------------------
# Order parsing helpers
# ---------------------------------------------------------------------------

def _upper(order: str) -> str:
    return str(order).upper().strip()


def is_move_order(order: str) -> bool:
    text = _upper(order)
    return " - " in text and " S " not in text and " C " not in text


def is_support_order(order: str) -> bool:
    return " S " in _upper(order)


def is_convoy_order(order: str) -> bool:
    return " C " in _upper(order)


def is_hold_order(order: str) -> bool:
    return _upper(order).endswith(" H")


def move_destination(order: str) -> str:
    """Destination province of a move order (``A PAR - BUR VIA`` -> ``BUR``)."""
    text = _upper(order)
    if " - " not in text:
        return ""
    return text.rsplit(" - ", 1)[1].split()[0].split("/")[0][:3]


def support_target(order: str) -> tuple[str, Optional[str]]:
    """For a support order, return (supported unit loc, supported dest or None).

    Accepts both the engine format (``F LYO S F WES - TYS``) and the shorthand
    some models write (``F LYO S WES - TYS``).
    """
    text = _upper(order)
    if " S " not in text:
        return "", None
    rest = text.split(" S ", 1)[1].strip()
    parts = rest.split()
    if not parts:
        return "", None
    if parts[0] in ("A", "F") and len(parts) >= 2:
        supported_loc = parts[1].split("/")[0][:3]
    else:
        supported_loc = parts[0].split("/")[0][:3]
    if " - " in rest:
        dest = rest.rsplit(" - ", 1)[1].split()[0].split("/")[0][:3]
        return supported_loc, dest
    return supported_loc, None


def _supported_moves(orders_by_power: dict[str, list[str]]) -> set[tuple[str, str]]:
    """Set of (unit loc, dest) pairs that have at least one support order."""
    supported: set[tuple[str, str]] = set()
    for orders in orders_by_power.values():
        for order in orders:
            if is_support_order(order):
                loc, dest = support_target(order)
                if loc and dest:
                    supported.add((loc, dest))
    return supported


# ---------------------------------------------------------------------------
# Order filtering (passives + strike/storm events)
# ---------------------------------------------------------------------------

def filter_mechanics_orders(
    game: Any,
    orders_by_power: dict[str, list[str]],
    phase: str,
    event_effect: Optional[dict] = None,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Apply passive + event order filters to one movement phase's orders.

    Returns ``(kept_orders, forbidden_orders)``. Forbidden orders are dropped;
    the engine treats unordered units as holding, which matches the
    "改为驻守" wording of the design. No-op outside movement phases and
    when the corresponding switches are off.
    """
    from .mechanics import events_enabled, is_movement_phase

    kept = {power: list(orders) for power, orders in orders_by_power.items()}
    forbidden: dict[str, list[str]] = {}
    if not is_movement_phase(phase):
        return kept, forbidden

    def _drop(power: str, order: str, why: str) -> None:
        if order in kept.get(power, []):
            kept[power].remove(order)
            forbidden.setdefault(power, []).append(order)
            logger.info("[被动/事件] %s 指令被过滤：%s（%s）", power, order, why)

    if passives_enabled():
        _filter_passives(game, kept, _drop, phase)
    if events_enabled() and event_effect:
        _filter_event_orders(game, kept, _drop, event_effect)
    return kept, forbidden


def _filter_passives(game: Any, kept: dict[str, list[str]], drop, phase: str) -> None:
    supported = _supported_moves(kept)

    # --- Italy: 地中海制海权 / France: 凡尔登精神 (attacks need support) ---
    italian_fleets = {
        unit_loc(unit)
        for unit in game.powers.get("ITALY", _EmptyPower()).units
        if unit_type(unit) == "F" and unit_loc(unit) in MEDITERRANEAN_SEAS
    }
    france = game.powers.get("FRANCE")
    par_protected = False
    if france is not None:
        par_holder = next(
            (name for name, power in game.powers.items() if any(unit_loc(u) == "PAR" for u in power.units)),
            None,
        )
        par_protected = par_holder == "FRANCE" or "PAR" in [str(c).upper()[:3] for c in france.centers]

    for power_name, orders in list(kept.items()):
        for order in list(orders):
            if not is_move_order(order):
                continue
            dest = move_destination(order)
            origin = unit_loc(order)
            if (origin, dest) in supported:
                continue
            if dest in italian_fleets and power_name != "ITALY":
                drop(power_name, order, "地中海制海权：进攻意大利舰队须有支援")
            elif dest == "PAR" and par_protected and power_name != "FRANCE":
                drop(power_name, order, "凡尔登精神：进攻巴黎须有支援")

    # --- Soviet: 冬将军 (Fall 1941+: foreign armies in Soviet homes hold) ---
    year = phase_year(phase)
    if is_fall_phase(phase) and year is not None and year >= 1941:
        soviet_homes = set(home_centers_of(game, "SOVIET")) or SOVIET_HOMES_FALLBACK
        for power_name, orders in list(kept.items()):
            if power_name == "SOVIET":
                continue
            for order in list(orders):
                if unit_type(order) == "A" and unit_loc(order) in soviet_homes and not is_hold_order(order):
                    drop(power_name, order, "冬将军：苏联本土的外国陆军只能驻守")


class _EmptyPower:
    units: list = []
    centers: list = []


def _filter_event_orders(game: Any, kept: dict[str, list[str]], drop, effect: dict) -> None:
    card_id = effect.get("card_id")
    targets = effect.get("targets") or []
    if card_id == "storm":
        zone = (effect.get("zone") or "").upper()[:3]
        if not zone:
            return
        for power_name, orders in list(kept.items()):
            for order in list(orders):
                if is_move_order(order) and move_destination(order) == zone:
                    drop(power_name, order, f"海上风暴：{zone} 海域封闭")
                elif is_convoy_order(order) and unit_loc(order) == zone:
                    drop(power_name, order, f"海上风暴：{zone} 海域内的舰队不能护航")
        return

    if card_id not in {"port_strike", "rail_strike", "assassination"} or not targets:
        return
    target = targets[0]
    orders = kept.get(target, [])
    if not orders:
        return
    centers = supply_centers_of(game)
    for order in list(orders):
        if is_hold_order(order) or is_support_order(order):
            continue  # strikes allow hold and support
        if not (is_move_order(order) or is_convoy_order(order)):
            continue
        if card_id == "assassination":
            drop(target, order, "暗杀未遂：全国停摆，只能驻守或支援")
        elif card_id == "rail_strike" and unit_type(order) == "A":
            drop(target, order, "铁路罢工：陆军不能移动")
        elif card_id == "port_strike" and unit_type(order) == "F" and unit_loc(order) in centers:
            drop(target, order, "港口罢工：沿海补给中心的舰队不能移动")


# ---------------------------------------------------------------------------
# Germany bonus move (机动战) + shared bonus-move helper
# ---------------------------------------------------------------------------

def _phase_traffic(submitted_orders: Optional[Iterable[Iterable[str]]]) -> tuple[set[str], set[str]]:
    """Provinces vacated / entered this phase, from submitted move orders."""
    vacated: set[str] = set()
    entered: set[str] = set()
    for orders in submitted_orders or []:
        for order in orders:
            if is_move_order(order):
                vacated.add(unit_loc(order))
                entered.add(move_destination(order))
    return vacated, entered


def apply_bonus_move(
    game: Any,
    power: str,
    unit: str,
    target: str,
    submitted_orders: Optional[Iterable[Iterable[str]]] = None,
    allowed_targets: Optional[set[str]] = None,
) -> tuple[bool, str]:
    """Move one army one extra province after resolution, enforcing the
    shared bonus-move constraints: target empty, not an enemy unit/center
    province, not vacated/entered this phase, adjacent, army-legal.

    Returns ``(ok, reason)``; failures never mutate the board.
    """
    power_obj = game.powers.get(power)
    if power_obj is None:
        return False, "未知国家"
    unit = str(unit).upper().strip()
    target = str(target).upper().strip().split("/")[0][:3]
    if unit not in power_obj.units:
        return False, "该单位不在棋盘上"
    if unit_type(unit) != "A":
        return False, "奖励移动只限陆军"
    origin = unit_loc(unit)
    if target == origin:
        return False, "目标与出发省相同"
    if allowed_targets is not None and target not in allowed_targets:
        return False, "目标不在允许范围内"
    game_map = getattr(game, "map", None)
    if game_map is not None:
        try:
            if not game_map.is_valid_unit("A " + target):
                return False, "陆军不能进入该省"
        except Exception:
            pass
        try:
            if not game_map.abuts("A", origin, "-", target):
                return False, "目标不是相邻省份"
        except Exception:
            pass
    occupied = {
        unit_loc(u) for p in game.powers.values() for u in p.units
    }
    if target in occupied:
        return False, "目标省已有单位"
    for name, other in game.powers.items():
        if name != power and target in [str(c).upper()[:3] for c in other.centers]:
            return False, "目标是敌方补给中心"
    vacated, entered = _phase_traffic(submitted_orders)
    if target in vacated or target in entered:
        return False, "目标省本阶段刚被腾出或进入"
    moved = move_unit(game, power, unit, target)
    if moved is None:
        return False, "移动失败"
    logger.info("[被动] %s 机动战：%s 额外移动到 %s", power, unit, target)
    return True, ""


# ---------------------------------------------------------------------------
# Britain: 情报嗅觉 (message-count brief)
# ---------------------------------------------------------------------------

def diplomacy_brief(game_history: Any, current_phase: str) -> str:
    """Private-message COUNTS per pair of powers from the latest phase with
    messages before the current one. Counts only, never content."""
    phases = getattr(game_history, "phases", None) or []
    previous = None
    for phase in reversed(phases):
        name = getattr(phase, "name", "")
        if str(name) == str(current_phase):
            continue
        messages = getattr(phase, "messages", None) or []
        if any(getattr(m, "recipient", "") != "GLOBAL" for m in messages):
            previous = phase
            break
    header = "# 外交动态简报（情报嗅觉）"
    if previous is None:
        return f"{header}\n上阶段暂无私信记录。"
    counts: dict[tuple[str, str], int] = {}
    for message in previous.messages:
        sender = str(getattr(message, "sender", "")).upper()
        recipient = str(getattr(message, "recipient", "")).upper()
        if recipient == "GLOBAL" or not sender or not recipient:
            continue
        pair = tuple(sorted((sender, recipient)))
        counts[pair] = counts.get(pair, 0) + 1
    if not counts:
        return f"{header}\n上阶段暂无私信记录。"
    lines = "；".join(f"{a}-{b}：{n} 封" for (a, b), n in sorted(counts.items()))
    return f"{header}\n上一阶段各国之间私信数量（只计数量）：{lines}。"
