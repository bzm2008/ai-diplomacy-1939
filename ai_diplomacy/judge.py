"""Game judge: GPT 6 Luna (OpenAI-compatible API) integration.

The judge replaces Laya as the game's referee for every judgement call the
rules leave open: annual event targets/timing, per-phase domestic
satisfaction scores, split-province choices, and minor-defection choices.

Unlike Laya (a 421M encoder that can only answer choice/score questions from
a fixed set), Luna applies written rubrics to a factual state summary and
returns strict JSON. Every answer is validated against the allowed set and
clamped to range; any failure (missing route, timeout, malformed JSON, an
answer outside the allowed set, low self-reported confidence) falls back to
the seeded RNG / a deterministic formula, and the game never crashes
because of the judge.

Route: ``DIP_JUDGE_MODEL`` holds a resolved OpenAI-compatible spec of the
same shape the launcher uses for cards, e.g.
``openai:gpt-6-luna@https://kuaipao.ai/v1#sk-...``. The launcher injects it
from the environment; this module never reads API keys from files.
"""

from __future__ import annotations

import json
import logging
import os
import random
from typing import Any, Optional

from .mechanics import judge_enabled

logger = logging.getLogger(__name__)

DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_TIMEOUT = 60


def judge_model_spec() -> str:
    return os.environ.get("DIP_JUDGE_MODEL", "").strip()


def min_confidence() -> float:
    try:
        return float(os.environ.get("DIP_JUDGE_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIDENCE


def _parse_spec(spec: str) -> Optional[tuple[str, str, str]]:
    """Parse ``openai:model@base#key`` into (model, base_url, api_key)."""
    if not spec:
        return None
    proto, sep, rest = spec.partition(":")
    if sep != ":" or proto.strip().lower() != "openai":
        return None
    model, sep, tail = rest.partition("@")
    base, sep, key = tail.partition("#")
    model, base, key = model.strip(), base.strip(), key.strip()
    if not model or not base or not key:
        return None
    return model, base, key


class JudgeDirector:
    """Lazy-loading wrapper around a Luna chat client with RNG fallback."""

    def __init__(self, model_spec: Optional[str] = None, threshold: Optional[float] = None):
        self._model_spec = model_spec if model_spec is not None else judge_model_spec()
        self._threshold = DEFAULT_MIN_CONFIDENCE if threshold is None else threshold
        self._client: Any = None
        self._parsed: Optional[tuple[str, str, str]] = None
        self._load_tried = False
        self._load_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> Optional[Any]:
        if self._client is not None:
            return self._client
        if self._load_tried:
            return None
        self._load_tried = True
        parsed = _parse_spec(self._model_spec)
        if parsed is None:
            self._load_error = "DIP_JUDGE_MODEL 未设置或格式错误（应为 openai:model@base#key）"
            logger.warning("[裁判] %s，退回种子随机/公式兜底", self._load_error)
            return None
        try:
            from openai import OpenAI
        except Exception as exc:
            self._load_error = f"openai 包不可用：{exc}"
            logger.warning("[裁判] %s，退回种子随机/公式兜底", self._load_error)
            return None
        try:
            model, base, key = parsed
            self._client = OpenAI(api_key=key, base_url=base, timeout=DEFAULT_TIMEOUT)
            self._parsed = parsed
        except Exception as exc:
            self._load_error = f"客户端初始化失败：{exc}"
            logger.warning("[裁判] %s，退回种子随机/公式兜底", self._load_error)
            return None
        logger.info("[裁判] Luna 客户端已就绪：%s", parsed[0])
        return self._client

    # -- low-level JSON call --------------------------------------------

    def _ask_json(self, system: str, user: str, max_tokens: int = 1200) -> Optional[dict]:
        client = self._load()
        if client is None or self._parsed is None:
            return None
        try:
            response = client.chat.completions.create(
                model=self._parsed[0],
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning("[裁判] 调用失败（%s），退回兜底", exc)
            return None
        try:
            text = response.choices[0].message.content or ""
            parsed = json.loads(text)
        except Exception as exc:
            logger.warning("[裁判] 返回解析失败（%s），退回兜底", exc)
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- choice questions (event director interface) ---------------------

    def choose(
        self,
        state_summary: str,
        question_id: str,
        instructions: str,
        options: dict[str, str],
        rng: random.Random,
    ) -> tuple[str, float, str]:
        """Pick one option key. Returns (choice, confidence, source).

        ``source`` is ``"judge"``, ``"random"`` (fallback) or ``"forced"``.
        Same call shape as the old Laya director so event code is unchanged.
        """
        keys = list(options.keys())
        if not keys:
            raise ValueError("Judge choice needs at least one option")
        if len(keys) == 1:
            return keys[0], 1.0, "forced"
        if not judge_enabled():
            return rng.choice(keys), 0.0, "random"
        options_text = "\n".join(f"- {key}: {desc}" for key, desc in options.items())
        parsed = self._ask_json(
            "You are the impartial referee of a Diplomacy board game. Answer with strict JSON only.",
            f"GAME STATE:\n{state_summary}\n\nQUESTION: {instructions}\n\nOPTIONS:\n{options_text}\n\n"
            'Respond with JSON: {"choice": "<one option key>", "confidence": <0.0-1.0>, "reason": "<one short sentence>"}.',
            max_tokens=400,
        )
        if parsed is None:
            return rng.choice(keys), 0.0, "random"
        choice = parsed.get("choice")
        confidence = parsed.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = 0.5
        confidence = max(0.0, min(1.0, float(confidence)))
        if choice not in options:
            logger.warning("[裁判] 选择 %r 不在允许集合内，退回种子随机", choice)
            return rng.choice(keys), confidence, "random"
        if confidence < self._threshold:
            logger.info("[裁判] 置信度 %.3f 低于阈值 %.3f，退回种子随机", confidence, self._threshold)
            return rng.choice(keys), confidence, "random"
        return str(choice), confidence, "judge"

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

    # -- satisfaction scoring --------------------------------------------

    def score_all(
        self,
        facts_text: str,
        rubric_text: str,
        powers: list[str],
        fallback_scores: dict[str, int],
    ) -> tuple[dict[str, tuple[int, str]], str]:
        """Score every power's domestic satisfaction (0-100).

        Returns ({power: (score, reason)}, source) where source is "judge"
        when every power was scored by Luna, "mixed" when some fell back to
        the deterministic formula, and "formula" when Luna was unavailable.
        Scores are clamped to 0-100; per-phase movement clamping happens in
        the caller (SatisfactionState), which owns the previous scores.
        """
        if not judge_enabled() or not self.available:
            return {p: (int(fallback_scores.get(p, 60)), "公式兜底") for p in powers}, "formula"
        parsed = self._ask_json(
            "You are the impartial referee of a Diplomacy board game. Score domestic satisfaction from the facts and the rubric. Answer with strict JSON only.",
            f"FACTS (one block per power):\n{facts_text}\n\nRUBRIC:\n{rubric_text}\n\n"
            'Respond with JSON: {"scores": {"POWER": {"score": <0-100 integer>, "reason": "<one short sentence in Chinese>"}, ...}}. '
            "Score EVERY power listed in the facts.",
            max_tokens=2500,
        )
        if parsed is None:
            return {p: (int(fallback_scores.get(p, 60)), "公式兜底") for p in powers}, "formula"
        raw_scores = parsed.get("scores")
        if not isinstance(raw_scores, dict):
            logger.warning("[裁判] 满意度返回结构异常，退回公式兜底")
            return {p: (int(fallback_scores.get(p, 60)), "公式兜底") for p in powers}, "formula"
        result: dict[str, tuple[int, str]] = {}
        used_fallback = False
        for power in powers:
            entry = raw_scores.get(power)
            score = entry.get("score") if isinstance(entry, dict) else None
            reason = entry.get("reason") if isinstance(entry, dict) else ""
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                result[power] = (max(0, min(100, int(round(float(score))))), str(reason or "")[:80])
            else:
                result[power] = (int(fallback_scores.get(power, 60)), "公式兜底")
                used_fallback = True
        source = "mixed" if used_fallback else "judge"
        if used_fallback:
            logger.warning("[裁判] 满意度部分国家退回公式兜底")
        return result, source


# Module-level singleton (tests replace it via set_director).
_DIRECTOR: Optional[JudgeDirector] = None


def get_director() -> JudgeDirector:
    global _DIRECTOR
    if _DIRECTOR is None:
        _DIRECTOR = JudgeDirector()
    return _DIRECTOR


def set_director(director: Optional[JudgeDirector]) -> None:
    global _DIRECTOR
    _DIRECTOR = director


# State summaries are director-agnostic; reuse Laya's compact builder.
from .laya_director import build_state_summary  # noqa: E402  (re-export)

__all__ = [
    "JudgeDirector",
    "build_state_summary",
    "get_director",
    "judge_model_spec",
    "min_confidence",
    "set_director",
]
