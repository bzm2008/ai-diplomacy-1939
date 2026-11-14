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
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Powers of the europe1939 map, in canonical order.
POWERS_1939 = ("BRITAIN", "FRANCE", "GERMANY", "ITALY", "POLAND", "SOVIET", "SPAIN", "USA")

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


# 施佩尔奇迹 (Speer miracle): Germany's wartime armaments surge. The build
# bonus mirrors the USA arsenal hook below; the broadcast notice in
# mechanics_context_for_power deliberately omits the end year (only
# Germany's own system prompt states the 1944 expiry).
SPEER_MIRACLE_YEARS = (1942, 1944)


def speer_miracle_active(year: Optional[int]) -> bool:
    return year is not None and SPEER_MIRACLE_YEARS[0] <= year <= SPEER_MIRACLE_YEARS[1]


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
        # Local imports: events/exile import the switches from this module.
        from .events import EventManager
        from .exile import ExileRegistry

        data = data or {}
        self.events = EventManager(data.get("events"))
        self.exile = ExileRegistry(data.get("exile"))

    def to_dict(self) -> dict:
        return {"events": self.events.to_dict(), "exile": self.exile.to_dict()}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "MechanicsManager":
        return cls(data or {})

    # -- engine hook ------------------------------------------------------

    def adjust_build_balance(self, game: Any, power_name: str, balance: int) -> int:
        """Adjust an adjustment-phase balance (centers - units, card-adjusted).

        Positive = builds allowed, negative = disbands required. Applies, in
        order: exile disband exemption, USA arsenal bonus, Germany Speer
        bonus, famine build ban, exile-funding build cost. Only active
        during adjustment phases.
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
        if passives_enabled() and power_name == "GERMANY" and speer_miracle_active(year):
            # 施佩尔奇迹: one extra build each winter 1942-1944; the same +1
            # also exempts the overbuilt unit from forced disband while active.
            balance += 1
        if events_enabled():
            famine_target = self.events.famine_target_for_year(year)
            if famine_target == power_name:
                balance = min(0, balance)
        if exile_enabled() and power_name in self.exile.winter_funding and balance > 0:
            # Funding a restoration army costs the host one build.
            balance -= 1
        return balance


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
    if passives_enabled() and speer_miracle_active(phase_year(phase)):
        # Broadcast to every power: the fact is public (builds are visible
        # on the board anyway); the 1944 expiry is stated only in
        # Germany's own system prompt.
        blocks.append(
            "【历史事件·施佩尔奇迹】1942 年，施佩尔出任德国军备部长，德国军工产量大幅提升："
            "德国每个冬季可多建造一支单位。"
        )
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
    return "\n\n".join(blocks)
