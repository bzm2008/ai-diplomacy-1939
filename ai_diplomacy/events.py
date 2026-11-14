"""Random events (1937): seeded 8-card deck, judge-directed targeting/timing,
visibility-scoped announcement, hard-effect enforcement via order filters.

One card is drawn each Spring movement phase. Each card fires at most once
per game: the deck is never reshuffled, so if it runs out, later years
simply have no event. The drawn card is shown to the
event director (the LLM judge, see judge.py) first; the judge picks the
target and the timing (this phase / next movement phase) from
hard-constrained option sets, and any unavailable/low-confidence/out-of-set
answer falls back to the seeded RNG.
Effects are enforced by code (order filters, build balance, board mutation),
never by prompt obedience alone.
"""

from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Optional

from . import judge as judge_director
from .exile import is_normal_active
from .mechanics import (
    add_unit,
    events_enabled,
    home_centers_of,
    is_movement_phase,
    phase_year,
    sea_zones_of,
    unit_loc,
)

logger = logging.getLogger(__name__)

EVENT_IDS = [
    "leak",
    "famine",
    "port_strike",
    "rail_strike",
    "assassination",
    "reserves",
    "feud",
    "storm",
]

EVENT_NAMES = {
    "leak": "密电泄露",
    "famine": "粮食危机",
    "port_strike": "港口罢工",
    "rail_strike": "铁路罢工",
    "assassination": "暗杀未遂",
    "reserves": "预备役动员",
    "feud": "外交风波",
    "storm": "海上风暴",
}

# Events announced to every power (others: target + allies only).
GLOBAL_EVENTS = {"leak", "storm"}

# Events enforced by order filtering while active.
FILTER_EVENTS = {"port_strike", "rail_strike", "assassination", "storm"}

# Built-in text templates (events.json next to the 1939 prompts overrides).
_BUILTIN_TEXTS = {
    "leak": "一封私信被公之于众：{sender} 写给 {recipient}：“{content}”",
    "famine": "{target} 粮仓见底，当年冬季不能建造任何单位。",
    "port_strike": "{target} 的港口工人罢工：本阶段位于沿海补给中心的舰队不能移动，只能驻守或支援。",
    "rail_strike": "{target} 的铁路工人罢工：本阶段所有陆军不能移动，只能驻守或支援。",
    "assassination": "{target} 的领袖遭遇暗杀未遂，全国戒严：本阶段所有单位不能移动，只能驻守或支援。",
    "reserves": "{target} 征召预备役，立即补充一支单位。",
    "feud": "{target_a} 与 {target_b} 发生外交风波：本阶段两国不能互相发私信，但可与其他国家正常谈判。",
    "storm": "{zone} 海域风暴肆虐：本阶段该海域封闭，任何单位不得进入或穿越。",
}


def _load_texts() -> dict[str, str]:
    texts = dict(_BUILTIN_TEXTS)
    try:
        path = Path(__file__).resolve().parent / "prompts_1939" / "events.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key, value in data.items():
                if key in texts and isinstance(value, str) and value.strip():
                    texts[key] = value
    except (OSError, ValueError):
        pass
    return texts


EVENT_TEXTS = _load_texts()


def format_event_text(record: dict) -> str:
    template = EVENT_TEXTS.get(record.get("card_id", ""), "")
    targets = record.get("targets") or []
    values = {
        "target": targets[0] if targets else "",
        "target_a": targets[0] if len(targets) > 0 else "",
        "target_b": targets[1] if len(targets) > 1 else "",
        "zone": record.get("zone") or "",
        "year": str(record.get("year") or ""),
        "sender": (record.get("leak") or {}).get("sender", ""),
        "recipient": (record.get("leak") or {}).get("recipient", ""),
        "content": (record.get("leak") or {}).get("content", ""),
    }
    try:
        text = template.format(**values)
    except (KeyError, IndexError):
        text = template
    revealed = record.get("treaty_revealed")
    if isinstance(revealed, dict):
        text += (
            f"\n【密约曝光】{revealed.get('a', '')} 与 {revealed.get('b', '')} "
            "之间的一份秘密协定被曝光。"
        )
    return text


def _default_seed() -> int:
    """Deck seed for a fresh game: DIP_MECHANICS_SEED when set (reproducible
    tests), otherwise a fresh random seed per game so two games do not draw
    the same event sequence. Resumed games keep the saved seed/RNG state."""
    raw = os.environ.get("DIP_MECHANICS_SEED", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("[事件] DIP_MECHANICS_SEED=%r 无效，使用随机种子", raw)
    return random.SystemRandom().randrange(1, 2**31)


class EventManager:
    """Seeded event deck + active/pending effects. JSON-serializable."""

    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.seed: int = int(data["seed"]) if "seed" in data else _default_seed()
        self.deck: list[str] = list(data.get("deck") or [])
        self.drawn: list[dict] = [dict(item) for item in data.get("drawn", [])]
        self.active: Optional[dict] = dict(data["active"]) if data.get("active") else None
        self.pending: Optional[dict] = dict(data["pending"]) if data.get("pending") else None
        self.famine_targets: dict[str, str] = dict(data.get("famine_targets") or {})
        self._rng = random.Random(self.seed)
        rng_state = data.get("rng_state")
        if rng_state:
            try:
                self._rng.setstate((int(rng_state[0]), tuple(int(x) for x in rng_state[1]), rng_state[2]))
            except (TypeError, ValueError, IndexError):
                self._rng = random.Random(self.seed)
        # Keep only known cards in a restored deck.
        self.deck = [card for card in self.deck if card in EVENT_IDS]
        # Fresh game only: build the initial deck. A restored game whose
        # deck is legitimately empty (all cards drawn) must NOT reshuffle.
        if not self.deck and not self.drawn:
            self._reshuffle()

    def _reshuffle(self) -> None:
        self.deck = list(EVENT_IDS)
        self._rng.shuffle(self.deck)
        logger.info("[事件] 牌堆洗牌：%s", self.deck)

    def to_dict(self) -> dict:
        version, internal, gauss = self._rng.getstate()
        return {
            "seed": self.seed,
            "deck": list(self.deck),
            "drawn": [dict(item) for item in self.drawn],
            "active": dict(self.active) if self.active else None,
            "pending": dict(self.pending) if self.pending else None,
            "famine_targets": dict(self.famine_targets),
            "rng_state": [version, list(internal), gauss],
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EventManager":
        return cls(data or {})

    # -- deck -------------------------------------------------------------

    def draw_card_id(self) -> Optional[str]:
        # No reshuffle: each card fires at most once per game. An empty
        # deck means the year's draw is skipped (see draw_event).
        if not self.deck:
            return None
        return self.deck.pop(0)

    # -- phase lifecycle --------------------------------------------------

    def begin_phase(self, game: Any, phase: str) -> None:
        """Expire last phase's effect; activate a held (next-phase) event."""
        if not is_movement_phase(phase):
            return
        if self.pending is not None:
            self.active = self.pending
            self.pending = None
            logger.info(
                "[事件] %s 按导演决定在本阶段触发：%s",
                EVENT_NAMES.get(self.active.get("card_id", ""), "?"),
                format_event_text(self.active),
            )
        else:
            self.active = None

    def active_filter_effect(self) -> Optional[dict]:
        if self.active and self.active.get("card_id") in FILTER_EVENTS and not self.active.get("fizzled"):
            return self.active
        return None

    def feud_pair(self) -> Optional[tuple[str, str]]:
        if self.active and self.active.get("card_id") == "feud" and not self.active.get("fizzled"):
            targets = self.active.get("targets") or []
            if len(targets) == 2:
                return str(targets[0]), str(targets[1])
        return None

    def famine_target_for_year(self, year: int) -> Optional[str]:
        return self.famine_targets.get(str(year))

    # -- drawing ----------------------------------------------------------

    def draw_event(
        self,
        game: Any,
        game_history: Any,
        phase: str,
        rng: Optional[random.Random] = None,
    ) -> Optional[dict]:
        """Draw one card and resolve its target/timing. Returns the record
        (fizzled records have ``fizzled=True`` and no effect)."""
        if not events_enabled():
            return None
        rng = rng or self._rng
        year = phase_year(phase)
        card_id = self.draw_card_id()
        if card_id is None:
            logger.info("[事件] 牌堆已抽空，本年度无事件。")
            return None
        record: dict = {
            "card_id": card_id,
            "year": year,
            "drawn_phase": phase,
            "targets": [],
            "zone": None,
            "timing": "this_phase",
            "source": "random",
            "confidence": 0.0,
            "fizzled": False,
            "leak": None,
        }
        director = judge_director.get_director()
        prev_targets = [
            (item.get("targets") or [item.get("zone") or "?"])[0]
            for item in self.drawn[-3:]
            if item.get("targets") or item.get("zone")
        ]
        summary = judge_director.build_state_summary(game, game_history, card_id, prev_targets)

        if card_id == "leak":
            self._resolve_leak(game, game_history, phase, record, director, summary, rng)
        elif card_id == "famine":
            self._resolve_famine(game, record, rng)
        elif card_id == "reserves":
            self._resolve_reserves(game, record, rng)
        elif card_id in {"port_strike", "rail_strike", "assassination"}:
            self._resolve_targeted(game, record, director, summary, rng)
        elif card_id == "feud":
            self._resolve_feud(game, record, director, summary, rng)
        elif card_id == "storm":
            self._resolve_storm(game, record, director, summary, rng)

        record["text"] = "" if record["fizzled"] else format_event_text(record)
        self.drawn.append(record)

        if record["fizzled"]:
            logger.info("[事件] %s 轮空（无符合条件的目标），记日志", EVENT_NAMES[card_id])
            return record
        if card_id == "famine":
            self.famine_targets[str(year)] = record["targets"][0]
        elif card_id == "reserves":
            self._apply_reserves(game, record)
            if record["fizzled"]:
                return record
            self.active = record
        elif record["timing"] == "next_movement":
            self.pending = record
            logger.info(
                "[事件] 抽到 %s，导演决定下个移动阶段触发（来源=%s，置信度=%.3f）",
                EVENT_NAMES[card_id], record["source"], record["confidence"],
            )
        else:
            self.active = record
        logger.info(
            "[事件] 抽到 %s：%s（目标=%s，时间=%s，来源=%s，置信度=%.3f）",
            EVENT_NAMES[card_id], record["text"], record["targets"] or record["zone"],
            record["timing"], record["source"], record["confidence"],
        )
        return record

    # -- per-card resolution ----------------------------------------------

    def _eligible_targets(self, game: Any, exclude_spain: bool = True) -> list[str]:
        # Events target major powers only; minors/reserves are never hit.
        from .mechanics import MAJOR_POWERS

        targets = [
            name
            for name in game.powers
            if name in MAJOR_POWERS
            and is_normal_active(game, name)
            and not (exclude_spain and name == "SPAIN")
        ]
        return sorted(targets)

    def _resolve_targeted(self, game, record, director, summary, rng) -> None:
        candidates = self._eligible_targets(game)
        if not candidates:
            record["fizzled"] = True
            return
        criteria = {
            name: f"{name}, {len(game.powers[name].centers)} centers"
            for name in candidates
        }
        choice, confidence, source = director.choose(
            summary, "target",
            f"Which country should the {record['card_id']} event hit? Pick the most dramatic and fair target.",
            criteria, rng,
        )
        record["targets"] = [choice]
        record["confidence"] = confidence
        record["source"] = source
        timing, _, _ = director.choose_timing(summary, rng)
        record["timing"] = timing

    def _resolve_feud(self, game, record, director, summary, rng) -> None:
        candidates = self._eligible_targets(game)
        pairs = [
            (a, b) for i, a in enumerate(candidates) for b in candidates[i + 1:]
        ]
        if not pairs:
            record["fizzled"] = True
            return
        criteria = {
            f"{a}-{b}": f"{a} and {b}" for a, b in pairs
        }
        choice, confidence, source = director.choose(
            summary, "pair",
            "Which pair of countries should suffer a diplomatic incident? Pick the pair whose silence would be most dramatic.",
            criteria, rng,
        )
        record["targets"] = choice.split("-")
        record["confidence"] = confidence
        record["source"] = source
        timing, _, _ = director.choose_timing(summary, rng)
        record["timing"] = timing

    def _resolve_storm(self, game, record, director, summary, rng) -> None:
        zones = sorted(sea_zones_of(game))
        if not zones:
            record["fizzled"] = True
            return
        criteria = {zone: f"{zone} sea zone" for zone in zones}
        choice, confidence, source = director.choose(
            summary, "zone",
            "Which sea zone should the storm close? Pick the zone whose closure would disrupt the most plans.",
            criteria, rng,
        )
        record["zone"] = choice
        record["confidence"] = confidence
        record["source"] = source
        timing, _, _ = director.choose_timing(summary, rng)
        record["timing"] = timing

    def _resolve_famine(self, game, record, rng) -> None:
        candidates = self._eligible_targets(game)
        if not candidates:
            record["fizzled"] = True
            return
        most = max(len(game.powers[name].centers) for name in candidates)
        leaders = [name for name in candidates if len(game.powers[name].centers) == most]
        record["targets"] = [rng.choice(leaders)]
        record["timing"] = "winter"
        record["source"] = "fixed"

    def _resolve_reserves(self, game, record, rng) -> None:
        from .mechanics import MAJOR_POWERS

        candidates = []
        for name in sorted(game.powers):
            if name not in MAJOR_POWERS or not is_normal_active(game, name):
                continue
            power = game.powers[name]
            if len(power.units) >= len(power.centers):
                continue
            if self._free_home_center(game, name) is None:
                continue
            candidates.append(name)
        if not candidates:
            record["fizzled"] = True
            return
        fewest = min(len(game.powers[name].centers) for name in candidates)
        weakest = [name for name in candidates if len(game.powers[name].centers) == fewest]
        record["targets"] = [rng.choice(weakest)]
        record["timing"] = "immediate"
        record["source"] = "fixed"

    def _free_home_center(self, game: Any, power_name: str) -> Optional[str]:
        power = game.powers.get(power_name)
        if power is None:
            return None
        occupied = {unit_loc(u) for p in game.powers.values() for u in p.units}
        owned = {str(c).upper()[:3] for c in power.centers}
        for home in home_centers_of(game, power_name):
            if home in owned and home not in occupied:
                return home
        return None

    def _apply_reserves(self, game: Any, record: dict) -> None:
        target = record["targets"][0]
        site = self._free_home_center(game, target)
        if site is None:
            record["fizzled"] = True
            logger.info("[事件] 预备役动员轮空：%s 已无空闲本土中心", target)
            return
        if add_unit(game, target, f"A {site}"):
            logger.info("[事件] 预备役动员：%s 在 %s 补充一支陆军", target, site)
        else:
            record["fizzled"] = True

    def _resolve_leak(self, game, game_history, phase, record, director, summary, rng) -> None:
        messages = _previous_private_messages(game_history, phase)
        # Only majors' letters can leak; Spain's never do; exiles' stay in.
        from .mechanics import MAJOR_POWERS

        pool = [
            m
            for m in messages
            if str(m.get("sender", "")).upper() in MAJOR_POWERS
            and str(m.get("sender", "")).upper() != "SPAIN"
        ]
        if not pool:
            record["fizzled"] = True
            return
        senders = sorted({str(m["sender"]).upper() for m in pool})
        choice, confidence, source = director.choose(
            summary, "source",
            "Whose private message should be leaked? Pick the sender whose exposed letter would be most dramatic.",
            {name: f"{name}'s letters" for name in senders}, rng,
        )
        record["targets"] = [choice]
        record["confidence"] = confidence
        record["source"] = source
        record["timing"] = "immediate"
        from_sender = [m for m in pool if str(m["sender"]).upper() == choice]
        picked = rng.choice(from_sender)
        record["leak"] = {
            "sender": picked["sender"],
            "recipient": picked["recipient"],
            "content": picked["content"],
        }
        # A leak may also expose one secret treaty (1937 design).
        try:
            manager = getattr(game, "mechanics_manager", None)
            if manager is not None:
                revealed = manager.peace.treaties.reveal_one_secret(phase, rng)
                if revealed is not None:
                    record["treaty_revealed"] = {
                        "a": revealed.get("a"),
                        "b": revealed.get("b"),
                    }
        except Exception as exc:
            logger.warning("[事件] 密约曝光失败：%s", exc)

    # -- visibility --------------------------------------------------------

    def context_block_for_power(
        self, game: Any, viewer: str, relationships: dict, phase: str
    ) -> str:
        """The `# 本期事件` block this power may see ("" when nothing)."""
        blocks: list[str] = []
        if self.active and not self.active.get("fizzled"):
            if self._visible_to(self.active, viewer, relationships):
                blocks.append(f"# 本期事件\n{self.active['text']}")
        year = phase_year(phase)
        famine_target = self.famine_targets.get(str(year)) if year else None
        if famine_target and self._famine_visible(famine_target, viewer, relationships):
            text = next(
                (item.get("text", "") for item in reversed(self.drawn)
                 if item.get("card_id") == "famine" and item.get("year") == year and not item.get("fizzled")),
                "",
            )
            if text:
                blocks.append(f"# 本期事件\n{text}")
        return "\n\n".join(blocks)

    def _visible_to(self, record: dict, viewer: str, relationships: dict) -> bool:
        if record.get("card_id") in GLOBAL_EVENTS:
            return True
        targets = record.get("targets") or []
        if viewer in targets:
            return True
        for target in targets:
            if str(relationships.get(target, "")).strip().lower() == "ally":
                return True
        return False

    def _famine_visible(self, target: str, viewer: str, relationships: dict) -> bool:
        if viewer == target:
            return True
        return str(relationships.get(target, "")).strip().lower() == "ally"


def _previous_private_messages(game_history: Any, current_phase: str) -> list[dict]:
    """Private messages of the latest phase with messages before the current one."""
    phases = getattr(game_history, "phases", None) or []
    for phase in reversed(phases):
        if str(getattr(phase, "name", "")) == str(current_phase):
            continue
        messages = getattr(phase, "messages", None) or []
        private = [
            {"sender": str(m.sender), "recipient": str(m.recipient), "content": str(m.content)}
            for m in messages
            if str(getattr(m, "recipient", "")) != "GLOBAL"
        ]
        if private:
            return private
    return []
