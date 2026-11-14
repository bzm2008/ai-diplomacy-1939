"""Shared helpers for the 1939 mechanics: env switches, phase parsing, unit
mutation, and the MechanicsManager container (events + exile state).

The manager is attached to the game object as ``game.mechanics_manager``
(following the ``game.card_manager`` pattern) and persisted by
``save_game_state`` / ``load_game_state`` as the top-level ``mechanics_state``
key of lmvsgame.json.
"""

from __future__ import annotations

import logging
import os
import random
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Powers of the europe1939 map, in canonical order (the eight majors).
MAJOR_POWERS = ("BRITAIN", "FRANCE", "GERMANY", "ITALY", "POLAND", "SOVIET", "SPAIN", "USA")
POWERS_1939 = MAJOR_POWERS

# Minor powers of the europe1937 map (GPT 6 Luna; 0 starting units).
MINOR_POWERS = ("PORTUGAL", "HOLLAND", "BELGIUM", "NORWAY", "DENMARK", "GREECE", "ROMANIA", "TURKEY")

# Dormant reserve powers, activated when a major splits (max 4 live splits).
RESERVE_POWERS = ("SPLIT_A", "SPLIT_B", "SPLIT_C", "SPLIT_D")

ALL_1937_POWERS = MAJOR_POWERS + MINOR_POWERS + RESERVE_POWERS

_OFF_VALUES = {"0", "false", "no", "off", "n", "f"}


def _env_on(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _OFF_VALUES


def events_enabled() -> bool:
    return _env_on("DIP_EVENTS", True)


def passives_enabled() -> bool:
    return _env_on("DIP_PASSIVES", True)


def exile_enabled() -> bool:
    return _env_on("DIP_EXILE", True)


def laya_enabled() -> bool:
    return _env_on("DIP_LAYA", True)


def judge_enabled() -> bool:
    return _env_on("DIP_JUDGE", True)


def satisfaction_enabled() -> bool:
    return _env_on("DIP_SATISFACTION", True)


def peace_systems_enabled() -> bool:
    return _env_on("DIP_PEACE_SYSTEMS", True)


def puppets_enabled() -> bool:
    return _env_on("DIP_PUPPETS", True)


_PHASE_RE = re.compile(r"([SFW])\s*(\d{4})\s*([MRA])")
_LONG_PHASE_RE = re.compile(r"(SPRING|FALL|WINTER)\s*(\d{4})", re.IGNORECASE)


def parse_phase(phase: str) -> Optional[tuple[str, int, str]]:
    """Parse a phase string into (season, year, type).

    Accepts short forms (``S1939M``, ``W1939A``) and long forms
    (``SPRING 1939 MOVEMENT``). Returns None when unparseable.
    """
    text = str(phase or "").upper()
    match = _PHASE_RE.search(text)
    if match:
        return match.group(1), int(match.group(2)), match.group(3)
    long_match = _LONG_PHASE_RE.search(text)
    if long_match:
        season = {"SPRING": "S", "FALL": "F", "WINTER": "W"}[long_match.group(1).upper()]
        kind = "A" if season == "W" else "M"
        return season, int(long_match.group(2)), kind
    return None


def phase_year(phase: str) -> Optional[int]:
    parsed = parse_phase(phase)
    return parsed[1] if parsed else None


# Annual "miracle drug" production bonuses (1937 restart): one extra build
# each winter inside the window, mirroring the USA arsenal hook below (the
# same +1 also exempts the overbuilt unit from forced disband while active).
# Multi-year notices deliberately omit the end year (only each power's own
# system prompt states the expiry); single-year notices say "this winter".
PRODUCTION_BONUSES = [
    {"power": "GERMANY", "years": (1939, 1939),
     "notice": "【历史事件·总体战动员】1939 年，德国进入总体战动员：德国今年冬季可多建造一支单位。"},
    {"power": "BRITAIN", "years": (1940, 1940),
     "notice": "【历史事件·敦刻尔克精神】1940 年，敦刻尔克大撤退后英国举国同仇敌忾：英国今年冬季可多建造一支单位。"},
    {"power": "USA", "years": (1941, 1941),
     "notice": "【历史事件·租借法案】1941 年，美国《租借法案》通过，军工全面开动：美国今年冬季可多建造一支单位。"},
    {"power": "GERMANY", "years": (1942, 1944),
     "notice": "【历史事件·施佩尔奇迹】1942 年，施佩尔出任德国军备部长，德国军工产量大幅提升：德国每个冬季可多建造一支单位。"},
    {"power": "SOVIET", "years": (1943, 1944),
     "notice": "【历史事件·乌拉尔军工】1943 年，迁往乌拉尔的苏联工业全力开工，苏联军工产量大幅提升：苏联每个冬季可多建造一支单位。"},
    {"power": "BRITAIN", "years": (1943, 1944),
     "notice": "【历史事件·大西洋胜利】1943 年，大西洋航线打通，英国物资供应大幅改善：英国每个冬季可多建造一支单位。"},
    {"power": "FRANCE", "years": (1944, 1944),
     "notice": "【历史事件·抵抗运动】1944 年，法国抵抗运动席卷全国：法国今年冬季可多建造一支单位。"},
    {"power": "SOVIET", "years": (1945, 1945),
     "notice": "【历史事件·柏林战役】1945 年，苏联红军向柏林发起最后总攻：苏联今年冬季可多建造一支单位。"},
]


def production_bonus_active(power_name: str, year: Optional[int]) -> bool:
    if year is None:
        return False
    return any(
        entry["power"] == power_name and entry["years"][0] <= year <= entry["years"][1]
        for entry in PRODUCTION_BONUSES
    )


def production_bonus_notices(year: Optional[int]) -> list[str]:
    if year is None:
        return []
    return [
        entry["notice"]
        for entry in PRODUCTION_BONUSES
        if entry["years"][0] <= year <= entry["years"][1]
    ]


def speer_miracle_active(year: Optional[int]) -> bool:
    return production_bonus_active("GERMANY", year)


def phase_season(phase: str) -> Optional[str]:
    parsed = parse_phase(phase)
    return parsed[0] if parsed else None


def is_movement_phase(phase: str) -> bool:
    parsed = parse_phase(phase)
    return bool(parsed and parsed[2] == "M")


def is_spring_phase(phase: str) -> bool:
    parsed = parse_phase(phase)
    return bool(parsed and parsed[0] == "S" and parsed[2] == "M")


def is_fall_phase(phase: str) -> bool:
    parsed = parse_phase(phase)
    return bool(parsed and parsed[0] == "F" and parsed[2] == "M")


def is_adjustment_phase(phase: str) -> bool:
    parsed = parse_phase(phase)
    return bool(parsed and parsed[2] == "A")


def current_phase_of(game: Any) -> str:
    try:
        return str(game.get_current_phase())
    except Exception:
        return str(getattr(game, "phase", "") or "")


def unit_loc(unit: str) -> str:
    """3-letter location of a unit string like ``A PAR`` or ``F STP/SC``."""
    parts = str(unit).split()
    if len(parts) < 2:
        return ""
    return parts[1].split("/")[0].upper()[:3]


def unit_type(unit: str) -> str:
    return str(unit).strip()[:1].upper()


# ---------------------------------------------------------------------------
# Board mutation helpers (mirror the engine's own move/build code paths:
# units list + zobrist hash + influence).
# ---------------------------------------------------------------------------

def _update_influence(game: Any, power: Any, loc: str) -> None:
    loc = loc[:3].upper()
    for other in game.powers.values():
        if loc in other.influence:
            other.influence.remove(loc)
    if loc not in power.influence:
        power.influence.append(loc)


def _invalidate_unit_owner_cache(game: Any) -> None:
    """Drop the engine's lazily built unit->owner cache.

    ``Game._unit_owner_cache`` is built once and never refreshed on its own;
    every direct ``power.units`` mutation below must invalidate it, otherwise
    the moved/added unit is invisible to ``Game._unit_owner`` and all of its
    orders are rejected as UNORDERABLE UNIT (this silently dropped Germany's
    bonus-moved army's Fall 1939 support order in the trial run).
    """
    try:
        game._unit_owner_cache = None
    except Exception:
        pass


def add_unit(game: Any, power_name: str, unit: str) -> bool:
    """Append a unit to a power's army, keeping hash/influence consistent."""
    power = game.powers.get(power_name)
    if power is None:
        return False
    unit = str(unit).upper()
    if unit in power.units:
        return False
    game.update_hash(power.name, unit_type=unit[0], loc=unit[2:])
    power.units.append(unit)
    _update_influence(game, power, unit_loc(unit))
    _invalidate_unit_owner_cache(game)
    return True


def remove_unit(game: Any, power_name: str, unit: str) -> bool:
    power = game.powers.get(power_name)
    if power is None or unit not in power.units:
        return False
    game.update_hash(power.name, unit_type=unit[0], loc=unit[2:])
    power.units.remove(unit)
    _invalidate_unit_owner_cache(game)
    return True


def move_unit(game: Any, power_name: str, unit: str, dest: str) -> Optional[str]:
    """Move a unit to a new province (post-resolution bonus moves).

    Returns the new unit string, or None if the unit is not on the board.
    """
    power = game.powers.get(power_name)
    if power is None or unit not in power.units:
        return None
    dest = dest[:3].upper()
    new_unit = unit[:2] + dest
    game.update_hash(power.name, unit_type=unit[0], loc=unit[2:])
    power.units.remove(unit)
    game.update_hash(power.name, unit_type=new_unit[0], loc=new_unit[2:])
    power.units.append(new_unit)
    _update_influence(game, power, dest)
    _invalidate_unit_owner_cache(game)
    return new_unit


def home_centers_of(game: Any, power_name: str) -> list[str]:
    power = game.powers.get(power_name)
    if power is not None and getattr(power, "homes", None):
        return [str(h).upper()[:3] for h in power.homes]
    homes = getattr(getattr(game, "map", None), "homes", {}) or {}
    return [str(h).upper()[:3] for h in homes.get(power_name, [])]


def supply_centers_of(game: Any) -> set[str]:
    scs = getattr(getattr(game, "map", None), "scs", None)
    if scs:
        return {str(c).upper()[:3] for c in scs}
    centers: set[str] = set()
    for power in game.powers.values():
        centers.update(str(c).upper()[:3] for c in power.centers)
    return centers


def sea_zones_of(game: Any) -> set[str]:
    loc_type = getattr(getattr(game, "map", None), "loc_type", None)
    if isinstance(loc_type, dict):
        return {str(loc).upper()[:3] for loc, kind in loc_type.items() if kind == "WATER"}
    return set()


def detect_attacks(
    orders_by_power: dict,
    centers_before: dict[str, str],
    units_before: dict[str, str],
) -> list[dict]:
    """Move orders into foreign-owned or foreign-occupied provinces.

    Used for betrayal/grievance detection and satisfaction facts. Supports,
    convoys, holds, retreats, and builds are not attacks.
    """
    attacks: list[dict] = []
    for attacker, orders in (orders_by_power or {}).items():
        for order in orders or []:
            text = str(order).upper()
            if " - " not in text or " S " in text or " C " in text:
                continue
            dest = text.rsplit(" - ", 1)[1].split()[0].split("/")[0][:3]
            owner = centers_before.get(dest)
            occupant = units_before.get(dest)
            if (owner and owner != attacker) or (occupant and occupant != attacker):
                attacks.append(
                    {"attacker": attacker, "dest": dest, "owner": owner, "occupant": occupant}
                )
    return attacks


def _engine_move_destination(order: str) -> Optional[str]:
    """Destination of an engine-internal move order ("- PAR" / "- PAR VIA")."""
    words = order.split()
    if not words or words[0] != "-":
        return None
    offset = 1 if words[-1] == "VIA" else 0
    return words[-1 - offset][:3].upper()


def _mechanics_seed() -> int:
    raw = os.environ.get("DIP_MECHANICS_SEED", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return random.SystemRandom().randrange(2 ** 31)


# ---------------------------------------------------------------------------
# MechanicsManager: one container for events + exile state, plus the engine
# adjustment-balance hook (disband exemption + build modifiers).
# ---------------------------------------------------------------------------

class MechanicsManager:
    """Owns EventManager + ExileRegistry and the adjustment-balance hook.

    The diplomacy engine calls :meth:`adjust_build_balance` (via its
    ``_adjustment_balance`` helper) in every adjustment-phase computation,
    so build/disband rules stay consistent between possible orders,
    order validation, civil-disorder handling and resolution.
    """

    def __init__(self, data: Optional[dict] = None):
        # Local imports: submodules import the switches from this module.
        from .events import EventManager
        from .exile import ExileRegistry
        from .peace_systems import PeaceSystems
        from .puppets import PuppetRegistry
        from .satisfaction import SatisfactionState

        data = data or {}
        self.events = EventManager(data.get("events"))
        self.exile = ExileRegistry(data.get("exile"))
        self.satisfaction = SatisfactionState(data.get("satisfaction"))
        self.peace = PeaceSystems(data.get("peace"))
        self.puppets = PuppetRegistry(data.get("puppets"))
        self.garrisons: Optional[set[str]] = (
            {str(g).upper()[:3] for g in data["garrisons"]}
            if data.get("garrisons") is not None
            else None
        )
        self.last_completed_phase: str = str(data.get("last_completed_phase") or "")
        self.rng = random.Random(_mechanics_seed() + 1)

    def to_dict(self) -> dict:
        return {
            "events": self.events.to_dict(),
            "exile": self.exile.to_dict(),
            "satisfaction": self.satisfaction.to_dict(),
            "peace": self.peace.to_dict(),
            "puppets": self.puppets.to_dict(),
            "garrisons": sorted(self.garrisons) if self.garrisons is not None else None,
            "last_completed_phase": self.last_completed_phase,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "MechanicsManager":
        return cls(data or {})

    # -- engine hook ------------------------------------------------------

    def adjust_build_balance(self, game: Any, power_name: str, balance: int) -> int:
        """Adjust an adjustment-phase balance (centers - units, card-adjusted).

        Positive = builds allowed, negative = disbands required. Applies, in
        order: exile disband exemption, USA arsenal bonus, time-bounded
        production bonuses, satisfaction modifier, puppet bonus, reserve
        spending, famine build ban, minor build cap, exile-funding cost.
        Only active during adjustment phases. Pure query: called repeatedly
        by the engine, so it must never mutate state (reserve spending is
        fixed at winter start by ReserveBank.begin_winter).
        """
        if getattr(game, "phase_type", None) != "A":
            return balance
        year = phase_year(current_phase_of(game))
        if year is None:
            return balance

        if exile_enabled() and self.exile.is_exiled(power_name):
            # Restoration armies are never force-disbanded.
            balance = max(0, balance)
        if passives_enabled() and power_name == "USA" and year >= 1941:
            # 民主兵工厂: one extra build each winter; the same +1 also
            # exempts the overbuilt unit from forced disband next winter.
            balance += 1
        if passives_enabled() and production_bonus_active(power_name, year):
            # Annual production bonus: one extra build each winter inside
            # the window; the same +1 also exempts the overbuilt unit from
            # forced disband while active.
            balance += 1
        if satisfaction_enabled():
            balance += self.satisfaction.build_modifier(power_name)
        if puppets_enabled():
            from .puppets import MAX_PUPPETS_PER_OVERLORD

            balance += min(MAX_PUPPETS_PER_OVERLORD, len(self.puppets.puppets_of(power_name)))
        if peace_systems_enabled():
            balance += self.peace.reserves.spend_bonus(power_name)
        if events_enabled():
            famine_target = self.events.famine_target_for_year(year)
            if famine_target == power_name:
                balance = min(0, balance)
        if power_name in MINOR_POWERS or power_name in RESERVE_POWERS:
            # Minor powers build at most 1 unit per even-year winter.
            if balance > 0:
                balance = 1 if year % 2 == 0 else 0
        if exile_enabled() and power_name in self.exile.winter_funding and balance > 0:
            # Funding a restoration army costs the host one build.
            balance -= 1
        return balance

    # -- treaty joint-attack strength (engine _strengths hook) -----------

    def joint_attack_bonus(self, game: Any, unit: str) -> int:
        """+1 strength when a treaty partner attacks the same province.

        Called by the engine's _strengths() for every unit; returns 0 unless
        the unit is moving, the year is >= 1939, and a treaty partner's unit
        is moving to the same destination. Pure query: no state changes.
        """
        if not peace_systems_enabled():
            return 0
        year = phase_year(current_phase_of(game))
        if year is None or year < 1939:
            return 0
        command = getattr(game, "command", None) or {}
        order = command.get(unit)
        if not order:
            return 0
        dest = _engine_move_destination(str(order))
        if dest is None:
            return 0
        owner = next((name for name, p in game.powers.items() if unit in p.units), None)
        if owner is None:
            return 0
        partners = set(self.peace.treaties.partners_of(owner))
        if not partners:
            return 0
        for other_unit, other_order in command.items():
            if other_unit == unit:
                continue
            if _engine_move_destination(str(other_order)) != dest:
                continue
            other_owner = next(
                (name for name, p in game.powers.items() if other_unit in p.units), None
            )
            if other_owner in partners:
                return 1
        return 0

    # -- minor home garrisons ----------------------------------------------

    def garrisoned_provinces(self, game: Any) -> set[str]:
        """Minor home supply centers never yet occupied (1-point garrisons)."""
        if self.garrisons is None:
            homes: set[str] = set()
            for minor in MINOR_POWERS:
                if minor in game.powers:
                    homes.update(home_centers_of(game, minor))
            self.garrisons = homes
        return set(self.garrisons)

    def ungarrison_occupied(self, game: Any) -> set[str]:
        """Drop the garrison once any unit occupies the province."""
        garrisoned = self.garrisoned_provinces(game)
        occupied = {unit_loc(u) for p in game.powers.values() for u in p.units}
        lost = garrisoned & occupied
        if lost:
            self.garrisons = garrisoned - lost
            for prov in sorted(lost):
                logger.info("[驻军] %s 已有部队进驻，驻军防御取消", prov)
        return lost


def get_mechanics_manager(game: Any) -> MechanicsManager:
    manager = getattr(game, "mechanics_manager", None)
    if manager is None:
        manager = MechanicsManager()
        game.mechanics_manager = manager
        # Seed the exile registry's view of pre-existing eliminations so a
        # resumed game does not resurrect long-dead powers as asylum seekers.
        manager.exile.seed_processed(game)
    return manager


# ---------------------------------------------------------------------------
# Per-power context blocks (called from build_context_prompt).
# ---------------------------------------------------------------------------

def mechanics_context_for_power(
    game: Any,
    game_history: Any,
    power_name: str,
    agent_relationships: Optional[dict],
    phase: str,
) -> str:
    """Compose the mechanics blocks visible to one power this phase.

    Short by design (Grok timeouts); never contains ``Reasoning:`` wording.
    """
    manager = getattr(game, "mechanics_manager", None)
    if manager is None:
        return ""
    blocks: list[str] = []
    if passives_enabled():
        # Broadcast to every power: the facts are public (builds are visible
        # on the board anyway); the 1944 expiry is stated only in each
        # power's own system prompt.
        year = phase_year(phase)
        for notice in production_bonus_notices(year):
            blocks.append(notice)
    if events_enabled():
        block = manager.events.context_block_for_power(game, power_name, agent_relationships or {}, phase)
        if block:
            blocks.append(block)
    if passives_enabled() and power_name == "BRITAIN":
        from .passives import diplomacy_brief

        brief = diplomacy_brief(game_history, phase)
        if brief:
            blocks.append(brief)
    if exile_enabled():
        block = manager.exile.context_block_for_power(game, power_name, phase)
        if block:
            blocks.append(block)
    if peace_systems_enabled():
        block = manager.peace.context_block_for_power(game, power_name, phase)
        if block:
            blocks.append(block)
    if puppets_enabled():
        block = manager.puppets.context_block_for_power(game, power_name, phase)
        if block:
            blocks.append(block)
    if satisfaction_enabled():
        # Symptom language only: numbers and tiers never enter prompts.
        if manager.satisfaction.hold_only(power_name) and is_movement_phase(phase):
            blocks.append("【国内动态】国内动荡不安，本阶段你的部队只能驻守或支援，无法移动。")
        if is_adjustment_phase(phase):
            mod = manager.satisfaction.build_modifier(power_name)
            if mod > 0:
                blocks.append("【国内动态】国内军工全力开工，本冬季可多建造一支单位。")
            elif mod < 0:
                blocks.append("【国内动态】国内动荡，本冬季建造名额减少一支。")
    last = getattr(manager, "last_completed_phase", "")
    if last:
        for registry in (
            manager.satisfaction,
            manager.peace.treaties,
            manager.peace.grievances,
            manager.peace.parades,
            manager.puppets,
        ):
            texts = getattr(registry, "announcements", {}).get(last, [])
            blocks.extend(texts)
    return "\n\n".join(blocks)
