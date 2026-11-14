"""Event director: Laya (open-source decision model) integration.

Laya only answers ``choice`` questions from caller-provided option sets, so it
can never invent an effect. Every decision is gated: model/package missing,
low confidence, or an answer outside the allowed set all fall back to the
seeded RNG, and the game never crashes because of the director.

Real-model notes (verified 2026-11-14 against laya 0.4.31):
``laya.load("typed-decisions")`` resolves the fine-tuned checkpoint, and
``agent.system_one(state, questions)`` returns
``{"answers": {qid: {"choice": ..., "probabilities": {...},
"answer_confidence": ...}}, ...}``. ``answer_confidence`` is max(p) and is
the quantity the gate uses.
"""

from __future__ import annotations

import logging
import os
import random
from typing import Any, Optional

from .mechanics import laya_enabled

logger = logging.getLogger(__name__)

# Fine-tuned checkpoint, per the design doc. Override with DIP_LAYA_MODEL
# (a laya alias like "typed-decisions"/"ml", a Hub id, or a local path).
DEFAULT_MODEL = "typed-decisions"

# Below this answer_confidence the decision is discarded for seeded random.
# Laya ships over-confident; validate the threshold offline before raising it.
DEFAULT_MIN_CONFIDENCE = 0.5


def laya_model_spec() -> str:
    return os.environ.get("DIP_LAYA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def min_confidence() -> float:
    try:
        return float(os.environ.get("DIP_LAYA_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIDENCE


class LayaDirector:
    """Lazy-loading wrapper around a Laya agent with RNG fallback."""

    def __init__(self, model_spec: Optional[str] = None, threshold: Optional[float] = None):
        self._model_spec = model_spec or laya_model_spec()
        self._threshold = DEFAULT_MIN_CONFIDENCE if threshold is None else threshold
        self._agent: Any = None
        self._load_tried = False
        self._load_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> Optional[Any]:
        if self._agent is not None:
            return self._agent
        if self._load_tried:
            return None
        self._load_tried = True
        try:
            import laya
        except Exception as exc:  # package missing -> seeded random
            self._load_error = f"laya package unavailable: {exc}"
            logger.warning("[Laya] %s，退回种子随机", self._load_error)
            return None
        try:
            self._agent = laya.load(self._model_spec)
        except Exception as exc:  # weights/download failure -> seeded random
            self._load_error = f"laya model load failed ({self._model_spec}): {exc}"
            logger.warning("[Laya] %s，退回种子随机", self._load_error)
            return None
        logger.info("[Laya] 模型已加载：%s", self._model_spec)
        return self._agent

    def choose(
        self,
        state_summary: str,
        question_id: str,
        instructions: str,
        options: dict[str, str],
        rng: random.Random,
    ) -> tuple[str, float, str]:
        """Pick one option key. Returns (choice, confidence, source).

        ``options`` maps option key -> short criterion text shown to Laya.
        ``source`` is ``"laya"`` or ``"random"`` (fallback).
        """
        keys = list(options.keys())
        if not keys:
            raise ValueError("Laya choice needs at least one option")
        if len(keys) == 1:
            return keys[0], 1.0, "forced"
        if not laya_enabled():
            return rng.choice(keys), 0.0, "random"
        agent = self._load()
        if agent is None:
            return rng.choice(keys), 0.0, "random"
        try:
            raw = agent.system_one(
                state_summary,
                {question_id: {"type": "choice", "instructions": instructions, "criteria": dict(options)}},
            )
        except Exception as exc:
            logger.warning("[Laya] 调用失败（%s），退回种子随机", exc)
            return rng.choice(keys), 0.0, "random"
        answers = raw.get("answers", raw) if isinstance(raw, dict) else {}
        answer = answers.get(question_id) if isinstance(answers, dict) else None
        if not isinstance(answer, dict):
            logger.warning("[Laya] 返回结构异常，退回种子随机")
            return rng.choice(keys), 0.0, "random"
        choice = answer.get("choice")
        confidence = _answer_confidence(answer)
        if choice not in options:
            logger.warning("[Laya] 选择 %r 不在允许集合内，退回种子随机", choice)
            return rng.choice(keys), confidence, "random"
        if confidence < self._threshold:
            logger.info("[Laya] 置信度 %.3f 低于阈值 %.3f，退回种子随机", confidence, self._threshold)
            return rng.choice(keys), confidence, "random"
        return str(choice), confidence, "laya"

    def choose_timing(self, state_summary: str, rng: random.Random) -> tuple[str, float, str]:
        """Decide whether an event fires this phase or next movement phase."""
        return self.choose(
            state_summary,
            "timing",
            "Should this event fire in the current movement phase or be held to the next movement phase?",
            {
                "this_phase": "Fire now, in the current movement phase.",
                "next_movement": "Hold it for the next movement phase.",
            },
            rng,
        )


def _answer_confidence(answer: dict) -> float:
    for key in ("answer_confidence", "confidence"):
        value = answer.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and probabilities:
        try:
            return float(max(probabilities.values()))
        except (TypeError, ValueError):
            pass
    return 0.0


# Module-level singleton (tests replace it via set_director).
_DIRECTOR: Optional[LayaDirector] = None


def get_director() -> LayaDirector:
    global _DIRECTOR
    if _DIRECTOR is None:
        _DIRECTOR = LayaDirector()
    return _DIRECTOR


def set_director(director: Optional[LayaDirector]) -> None:
    global _DIRECTOR
    _DIRECTOR = director


def build_state_summary(
    game: Any,
    game_history: Any,
    card_id: str,
    prev_event_targets: list[str],
) -> str:
    """Compact English key-value state for Laya (tens of tokens, not a report)."""
    phase = ""
    try:
        phase = str(game.get_current_phase())
    except Exception:
        pass
    year = ""
    for token in phase:
        if token.isdigit():
            year += token
    year = year[:4] or "?"
    centers = " ".join(
        f"{name}={len(power.centers)}" for name, power in sorted(game.powers.items())
    )
    attacks = _last_phase_attacks(game, game_history)
    attacks_text = ",".join(attacks[:6]) if attacks else "none"
    prev_text = ",".join(prev_event_targets[-3:]) if prev_event_targets else "none"
    return (
        f"year={year} phase={phase} centers: {centers} "
        f"last_phase_attacks: {attacks_text} prev_event_targets: {prev_text} card={card_id}"
    )


def _last_phase_attacks(game: Any, game_history: Any) -> list[str]:
    """Moves from the latest recorded movement phase into foreign centers."""
    try:
        phases = getattr(game_history, "phases", None) or []
        centers_of = {
            str(c).upper()[:3]: name
            for name, power in game.powers.items()
            for c in power.centers
        }
        for phase in reversed(phases):
            orders_by_power = getattr(phase, "orders_by_power", None) or {}
            if not orders_by_power:
                continue
            attacks: list[str] = []
            for power_name, orders in orders_by_power.items():
                for order in orders:
                    text = str(order).upper()
                    if " - " not in text or " S " in text or " C " in text:
                        continue
                    dest = text.rsplit(" - ", 1)[1].split()[0].split("/")[0][:3]
                    owner = centers_of.get(dest)
                    if owner and owner != power_name:
                        attacks.append(f"{power_name}>{dest}")
            if attacks:
                return sorted(set(attacks))
    except Exception as exc:
        logger.debug("[Laya] 状态摘要取攻击记录失败：%s", exc)
    return []
