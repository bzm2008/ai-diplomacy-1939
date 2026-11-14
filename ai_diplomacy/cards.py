"""Card rules and serializable state for the Europe 1939 game.

The card manager deliberately owns only game state.  Model clients and the
Diplomacy engine call the small helpers at the edges of the turn loop, which
keeps card validation usable in model-free tests and during resume.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardDefinition:
    card_id: str
    power: str
    name: str
    kind: str
    requires_target: bool = False


def _card(card_id: str, power: str, name: str, kind: str, requires_target: bool = False) -> CardDefinition:
    return CardDefinition(card_id, power, name, kind, requires_target)


CARD_DEFINITIONS: dict[str, CardDefinition] = {
    "uk_upgrade": _card("uk_upgrade", "BRITAIN", "升格（集群）", "cluster"),
    "uk_guard": _card("uk_guard", "BRITAIN", "国土警卫队", "protect"),
    "uk_ultra": _card("uk_ultra", "BRITAIN", "ULTRA", "ultra", True),
    "us_upgrade": _card("us_upgrade", "USA", "升格", "upgrade"),
    "us_industry": _card("us_industry", "USA", "工业力量", "build"),
    "us_manhattan": _card("us_manhattan", "USA", "曼哈顿计划", "manhattan"),
    "it_upgrade": _card("it_upgrade", "ITALY", "升格", "upgrade"),
    "it_mare": _card("it_mare", "ITALY", "我们的海", "protect"),
    "it_pact": _card("it_pact", "ITALY", "钢铁条约", "iron_pact"),
    "es_upgrade": _card("es_upgrade", "SPAIN", "升格", "upgrade"),
    "es_watch": _card("es_watch", "SPAIN", "观望", "protect_isolation"),
    "es_wind": _card("es_wind", "SPAIN", "见风使舵", "sway", True),
    "fr_cluster": _card("fr_cluster", "FRANCE", "集群", "cluster"),
    "fr_maginot": _card("fr_maginot", "FRANCE", "马奇诺防线", "protect", True),
    "fr_saar": _card("fr_saar", "FRANCE", "萨尔攻势", "build"),
    "de_cluster": _card("de_cluster", "GERMANY", "集群", "cluster"),
    "de_alps": _card("de_alps", "GERMANY", "阿尔卑斯要塞", "protect_isolation"),
    "de_blitz": _card("de_blitz", "GERMANY", "闪电战", "build"),
    "pl_cluster": _card("pl_cluster", "POLAND", "集群", "cluster"),
    "pl_hel": _card("pl_hel", "POLAND", "海尔要塞", "protect"),
    "pl_stubborn": _card("pl_stubborn", "POLAND", "殊死防守", "protect", True),
    "su_cluster": _card("su_cluster", "SOVIET", "集群", "cluster"),
    "su_scorched": _card("su_scorched", "SOVIET", "焦土政策", "scorched"),
    "su_order227": _card("su_order227", "SOVIET", "斯大林管风琴", "order227", True),
}

UPGRADE_MODELS = {
    "us_upgrade": "gpt-6-astra",
    "it_upgrade": "doubao-seed-2.1-pro",
    "es_upgrade": "gemini-4-carbon",
}


def upgrade_model_spec(card_id: str) -> str:
    """Return a routed upgrade model, with the plain model as a safe fallback.

    The launcher may provide ``DIP_CARD_UPGRADE_MODELS`` as a semicolon
    separated ``card_id=model-spec`` map.  Keys stay in the environment so
    they are never committed to source control.
    """
    configured = os.environ.get("DIP_CARD_UPGRADE_MODELS", "")
    for item in configured.split(";"):
        key, separator, value = item.partition("=")
        if separator and key.strip() == card_id and value.strip():
            return value.strip()
    return UPGRADE_MODELS.get(card_id, "")

_POWER_ALIASES = {
    "GBR": "BRITAIN", "UK": "BRITAIN", "ENG": "BRITAIN",
    "FRA": "FRANCE", "GER": "GERMANY", "ITA": "ITALY",
    "POL": "POLAND", "SOV": "SOVIET", "RUS": "SOVIET",
    "ESP": "SPAIN", "USA": "USA", "US": "USA",
}


def canonical_power(power: str) -> str:
    return _POWER_ALIASES.get(str(power).upper().strip(), str(power).upper().strip())


def _phase_code(phase: str) -> str:
    normalized = str(phase).upper().replace(" ", "")
    match = re.search(r"([SF]\d{4}M)$", normalized)
    if match:
        return match.group(1)
    long_match = re.search(r"(SPRING|FALL)(\d{4})", str(phase).upper())
    if long_match:
        return f"{'S' if long_match.group(1) == 'SPRING' else 'F'}{long_match.group(2)}M"
    return normalized


def _next_movement(phase: str) -> str:
    phase = _phase_code(phase)
    match = re.match(r"([SF])(\d{4})M$", phase)
    if not match:
        return phase
    season, year = match.group(1), int(match.group(2))
    return f"F{year}M" if season == "S" else f"S{year + 1}M"


def _location_tokens(target: Any) -> list[str]:
    if isinstance(target, (list, tuple, set)):
        values = target
    else:
        values = re.split(r"[,/\s]+", str(target or ""))
    return [str(value).upper()[:3] for value in values if str(value).strip()]


class CardManager:
    """Tracks each power's hand, declarations and active effects."""

    POWERS = ("BRITAIN", "FRANCE", "GERMANY", "ITALY", "POLAND", "SOVIET", "SPAIN", "USA")

    def __init__(self, data: dict | None = None):
        self.remaining: dict[str, list[str]] = {
            power: [card_id for card_id, card in CARD_DEFINITIONS.items() if card.power == power]
            for power in self.POWERS
        }
        self.declarations: list[dict[str, Any]] = []
        self.completed: set[str] = set()
        self.active: dict[str, dict[str, Any]] = {}
        self.build_modifiers: dict[str, int] = {power: 0 for power in self.POWERS}
        self.relationship_events: list[dict[str, str]] = []
        self.disband_modifiers: dict[str, int] = {power: 0 for power in self.POWERS}
        if data:
            self._load(data)

    def _load(self, data: dict):
        for power in self.POWERS:
            cards = data.get("remaining", {}).get(power)
            if cards is not None:
                self.remaining[power] = [card for card in cards if card in CARD_DEFINITIONS]
        self.declarations = [dict(item) for item in data.get("declarations", [])]
        self.completed = {str(item) for item in data.get("completed", [])}
        self.active = {str(key): dict(value) for key, value in data.get("active", {}).items()}
        self.build_modifiers.update({str(k): int(v) for k, v in data.get("build_modifiers", {}).items()})
        self.disband_modifiers.update({str(k): int(v) for k, v in data.get("disband_modifiers", {}).items()})
        self.relationship_events = [dict(item) for item in data.get("relationship_events", [])]

    @classmethod
    def from_dict(cls, data: dict) -> "CardManager":
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "remaining": self.remaining,
            "declarations": self.declarations,
            "completed": sorted(self.completed),
            "active": self.active,
            "build_modifiers": self.build_modifiers,
            "disband_modifiers": self.disband_modifiers,
            "relationship_events": self.relationship_events,
        }

    def validate_play(self, power: str, play: Any, phase: str, *, game: Any = None) -> tuple[bool, str]:
        """Validate and register a play.  Invalid plays never affect orders."""
        power = canonical_power(power)
        if not isinstance(play, dict):
            return False, "格式错误"
        card_id = str(play.get("card", "")).strip().lower()
        definition = CARD_DEFINITIONS.get(card_id)
        reason = str(play.get("reason", "")).strip()
        target = play.get("target", "")
        phase_code = _phase_code(phase)
        if not definition:
            return False, "未知牌 id"
        if definition.power != power:
            return False, "不是该国手牌"
        if not phase_code.endswith("M") or not phase_code.startswith(("S", "F")):
            return False, "只能在移动阶段出牌"
        if card_id not in self.remaining.get(power, []):
            return False, "该牌已使用"
        if not reason:
            return False, "缺少出牌理由"
        if definition.requires_target and not str(target or "").strip():
            return False, "缺少目标"
        if game is not None:
            target_names = {name.upper() for name in game.powers}
            if card_id in {"uk_ultra", "es_wind"} and canonical_power(str(target)) not in target_names:
                return False, "目标国家不存在"
            if card_id == "fr_maginot":
                locations = _location_tokens(target)
                if len(locations) != 2 or not set(locations).issubset({"BRE", "MAR", "PAR"}):
                    return False, "马奇诺防线必须指定 BRE/MAR/PAR 中的两个省份"
            if card_id == "pl_stubborn" and str(target).upper()[:3] not in {
                unit[2:5].upper() for unit in game.powers["POLAND"].units
            }:
                return False, "殊死防守目标必须是波兰当前有己方单位的省份"
            if card_id == "su_order227":
                location = str(target).upper()[:3]
                enemy_here = any(
                    name != "SOVIET" and any(unit[2:5].upper() == location for unit in power.units)
                    for name, power in game.powers.items()
                )
                adjacent = any(
                    game.map.abuts(unit[0], unit[2:5], "-", location)
                    for unit in game.powers["SOVIET"].units
                )
                if not enemy_here or not adjacent:
                    return False, "目标必须是与苏联单位相邻的敌方单位所在省份"
            if card_id == "us_manhattan":
                year_match = re.search(r"\d{4}", phase_code)
                start_year = int(os.environ.get("DIP_MANHATTAN_FROM_YEAR", "1942"))
                if not year_match or int(year_match.group(0)) < start_year:
                    return False, f"曼哈顿计划须在 {start_year} 年或之后使用"
                if max((len(power.centers) for power in game.powers.values()), default=0) < 18:
                    return False, "至少一个国家需要达到 18 个中心"
        self.remaining[power].remove(card_id)
        declaration = {
            "card_id": card_id,
            "power": power,
            "name": definition.name,
            "kind": definition.kind,
            "reason": reason,
            "target": target,
            "declared_phase": phase_code,
            "effective_phase": phase_code if definition.kind in {"upgrade", "cluster"} else _next_movement(phase_code),
        }
        if game is not None:
            self._capture_game_target(declaration, game)
        self.declarations.append(declaration)
        if definition.kind in {"upgrade", "cluster"}:
            remaining = 4
            self.active[card_id] = {**declaration, "remaining": remaining, "last_phase": phase_code}
            if definition.kind in {"upgrade", "cluster"}:
                self.build_modifiers[power] -= 1
        logger.info("[CARD] %s 宣告 %s 理由=%s 目标=%s", power, definition.name, reason, target or "无")
        return True, ""

    def _capture_game_target(self, declaration: dict[str, Any], game: Any):
        if declaration["card_id"] == "su_scorched":
            soviet = game.powers.get("SOVIET")
            declaration["captured_centers"] = list(soviet.centers if soviet else [])
        elif declaration["card_id"] == "us_manhattan":
            counts = {power.name: len(power.centers) for power in game.powers.values()}
            maximum = max(counts.values(), default=0)
            declaration["target_powers"] = [name for name, count in counts.items() if count == maximum and name != "USA"]

    def begin_phase(self, phase: str):
        phase_code = _phase_code(phase)
        if not phase_code.endswith("M"):
            return
        for card_id, effect in list(self.active.items()):
            if effect.get("last_phase") != phase_code:
                effect["remaining"] = int(effect.get("remaining", 0)) - 1
            if effect.get("remaining", 0) <= 0:
                logger.info("[CARD] %s 结束 %s", effect.get("power", ""), effect.get("name", card_id))
                self.active.pop(card_id, None)
                self.completed.add(card_id)
            else:
                effect["last_phase"] = phase_code
        for declaration in self.declarations:
            card_id = declaration["card_id"]
            if declaration.get("effective_phase") != phase_code or card_id in self.active:
                continue
            kind = declaration["kind"]
            remaining = 2 if kind in {"protect", "protect_isolation"} else 1
            self.active[card_id] = {**declaration, "remaining": remaining, "last_phase": phase_code}
            if kind == "build":
                amount = {"us_industry": 2, "fr_saar": 1, "de_blitz": 1}.get(card_id, 0)
                self.build_modifiers[declaration["power"]] += amount
            elif card_id == "us_manhattan":
                for target_power in declaration.get("target_powers", []):
                    self.disband_modifiers[target_power] = self.disband_modifiers.get(target_power, 0) + 2
            logger.info("[CARD] %s 生效 %s 理由=%s", declaration["power"], declaration["name"], declaration["reason"])

    def active_effects(self, phase: str | None = None) -> list[str]:
        return sorted(self.active)

    def status(self, card_id: str) -> str:
        """Return the persisted lifecycle state used by prompts and reviews."""
        card_id = str(card_id).lower()
        if card_id in self.active:
            return "生效中"
        for item in self.declarations:
            if item.get("card_id") == card_id:
                return "已用完" if card_id in self.completed else "已宣告"
        return "未用"

    def adjusted_build_limit(self, power: str, base_limit: int) -> int:
        if int(base_limit) <= 0:
            return 0
        return max(0, int(base_limit) + int(self.build_modifiers.get(canonical_power(power), 0)))

    def adjusted_build_need(self, power: str, base_need: int) -> int:
        """Return the number of builds available in this adjustment phase."""
        return max(0, int(base_need) + int(self.build_modifiers.get(canonical_power(power), 0)))

    def effective_center_count(self, game: Any, power: str) -> int:
        power = canonical_power(power)
        centers = {str(center).upper() for center in game.powers[power].centers}
        count = len(centers)
        for effect in self.active.values():
            if effect.get("card_id") == "su_scorched" and power != "SOVIET":
                count -= len(set(effect.get("captured_centers", [])) & centers)
        return max(0, count)

    def adjustment_balance(self, game: Any, power: str) -> int:
        power = canonical_power(power)
        return (
            self.effective_center_count(game, power)
            - len(game.powers[power].units)
            + int(self.build_modifiers.get(power, 0))
            - int(self.disband_modifiers.get(power, 0))
        )

    def consume_build_modifier(self, power: str):
        power = canonical_power(power)
        self.build_modifiers[power] = 0
        self.disband_modifiers[power] = 0

    def protection_bonus(self, game: Any, unit: str) -> int:
        return protection_bonus(game, unit)


def get_card_manager(game: Any) -> CardManager:
    manager = getattr(game, "card_manager", None)
    if manager is None:
        manager = CardManager()
        game.card_manager = manager
    return manager


def card_context_for_power(manager: CardManager, power: str, phase: str | None = None) -> str:
    power = canonical_power(power)
    hand = ", ".join(manager.remaining.get(power, [])) or "（无）"
    public = [
        f"{item['power']} {item['card_id']} target={item.get('target') or '无'}"
        for item in manager.declarations
    ]
    public_text = "；".join(public) if public else "（无）"
    return f"# 卡牌\n你的剩余手牌：{hand}\n已宣告（所有国家可见）：{public_text}\n"


def filter_card_orders(game: Any, power: str, orders: Iterable[str]) -> tuple[list[str], list[str]]:
    """Apply the no-attack clause of Spain's Watch and Germany's Alps."""
    manager = get_card_manager(game)
    power = canonical_power(power)
    restricted = any(
        effect.get("power") == power and effect.get("card_id") in {"es_watch", "de_alps"}
        for effect in manager.active.values()
    )
    if not restricted:
        return list(orders), []
    foreign_units = {
        unit[2:5].upper()
        for name, owner in game.powers.items()
        if name != power
        for unit in owner.units
    }
    allowed, forbidden = [], []
    for order in orders:
        text = str(order).upper()
        target = text.rsplit(" - ", 1)[-1].split()[0][:3] if " - " in text else ""
        if text.startswith(("A ", "F ")) and " - " in text and target in foreign_units:
            forbidden.append(order)
        else:
            allowed.append(order)
    return allowed, forbidden


def protection_bonus(game: Any, unit: str) -> int:
    manager = get_card_manager(game)
    owner_name = next((name for name, power in game.powers.items() if unit in power.units), None)
    if not owner_name:
        return 0
    location = unit[2:5].upper()
    for effect in manager.active.values():
        if effect.get("power") != owner_name:
            continue
        card_id = effect.get("card_id")
        locations = {
            "uk_guard": {"EDI", "LON", "LVP"},
            "it_mare": {"NAP", "ROM", "VEN"},
            "de_alps": {"MUN", "VIE"},
            "pl_hel": {"WAR"},
        }.get(card_id)
        if card_id == "es_watch" or card_id == "pl_stubborn":
            locations = {str(effect.get("target", "")).upper()[:3]} if card_id == "pl_stubborn" else {location}
        if card_id == "fr_maginot":
            locations = set(_location_tokens(effect.get("target")))
        if locations and location in locations:
            return 10
    return 0


def prepare_special_orders(game: Any, orders_by_power: dict[str, list[str]]) -> dict[str, list[str]]:
    """Apply deterministic special-card rewrites before ``Game.set_orders``."""
    manager = get_card_manager(game)
    rewritten = {power: list(orders) for power, orders in orders_by_power.items()}
    soviet = game.powers.get("SOVIET")
    if soviet:
        for effect in manager.active.values():
            if effect.get("card_id") != "su_order227":
                continue
            target = str(effect.get("target", "")).upper()[:3]
            if not target or not any(
                game.map.abuts(unit[0], unit[2:5], "-", target) for unit in soviet.units
            ):
                continue
            for power_name, power in game.powers.items():
                if power_name == "SOVIET":
                    continue
                for index, order in enumerate(rewritten.get(power_name, [])):
                    if len(order.split()) >= 2 and order.split()[1][:3].upper() == target:
                        rewritten[power_name][index] = " ".join(order.split()[:2]) + " H"
                        logger.info("[CARD] SOVIET 生效 斯大林管风琴，%s 的 %s 被改为 H", power_name, order)
    return rewritten
