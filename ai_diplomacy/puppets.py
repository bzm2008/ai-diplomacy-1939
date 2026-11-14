"""Puppet (protectorate) system (1937 restart, v3 design).

A minor power may become the puppet of a major power by mutual consent
(minor ``puppet_request`` + overlord ``puppet_decisions`` in diary/state
JSON, mirroring the exile flow). Effects:

- The puppet may ONLY exchange private messages with its overlord, may not
  send GLOBAL broadcasts, and conducts no diplomacy of its own.
- The overlord gains +1 build per puppet per winter (max 2 puppets).
- The overlord may command up to half the puppet's units (rounded up) via
  ``puppet_orders`` in its order JSON; directives take effect one movement
  phase later and override the puppet's own orders.
- Attacking a puppet gives its overlord a casus belli (grievance registry).
- The puppet may break free in a Spring diary (``break_free``); the
  overlord may expel it (``expel_puppet``). A freed minor waits one year
  before becoming a puppet again.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from .mechanics import (
    MAJOR_POWERS,
    MINOR_POWERS,
    current_phase_of,
    is_spring_phase,
    phase_year,
    puppets_enabled,
    unit_loc,
)

logger = logging.getLogger(__name__)

MAX_PUPPETS_PER_OVERLORD = 2


class PuppetRegistry:
    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.puppets: dict[str, dict] = {k: dict(v) for k, v in (data.get("puppets") or {}).items()}
        self.requests: dict[str, str] = dict(data.get("requests") or {})
        self.decisions: dict[str, dict[str, bool]] = {
            k: dict(v) for k, v in (data.get("decisions") or {}).items()
        }
        self.pending_breakfree: list[str] = list(data.get("pending_breakfree") or [])
        self.pending_expulsions: dict[str, str] = dict(data.get("pending_expulsions") or {})
        self.cooldown_until_year: dict[str, int] = dict(data.get("cooldown_until_year") or {})
        self.dissolved_log: dict[str, dict[str, str]] = {
            k: dict(v) for k, v in (data.get("dissolved_log") or {}).items()
        }
        self.directives: dict[str, dict] = {k: dict(v) for k, v in (data.get("directives") or {}).items()}
        self.announcements: dict[str, list[str]] = {
            k: list(v) for k, v in (data.get("announcements") or {}).items()
        }

    def to_dict(self) -> dict:
        return {
            "puppets": self.puppets,
            "requests": self.requests,
            "decisions": self.decisions,
            "pending_breakfree": self.pending_breakfree,
            "pending_expulsions": self.pending_expulsions,
            "cooldown_until_year": self.cooldown_until_year,
            "dissolved_log": self.dissolved_log,
            "directives": self.directives,
            "announcements": self.announcements,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "PuppetRegistry":
        return cls(data or {})

    def announce(self, phase: str, text: str) -> None:
        self.announcements.setdefault(phase, []).append(text)

    # -- queries ---------------------------------------------------------

    def is_puppet(self, power: str) -> bool:
        return power in self.puppets

    def overlord_of(self, power: str) -> Optional[str]:
        entry = self.puppets.get(power)
        return str(entry["overlord"]) if entry else None

    def puppets_of(self, overlord: str) -> list[str]:
        return sorted(m for m, e in self.puppets.items() if e.get("overlord") == overlord)

    def can_accept(self, overlord: str) -> bool:
        return len(self.puppets_of(overlord)) < MAX_PUPPETS_PER_OVERLORD

    def can_message(self, sender: str, recipient: str) -> bool:
        """Private-message routing: puppets only talk to their overlord."""
        if not puppets_enabled():
            return True
        sender_overlord = self.overlord_of(sender)
        if sender_overlord is not None and recipient != sender_overlord:
            return False
        recipient_overlord = self.overlord_of(recipient)
        if recipient_overlord is not None and sender != recipient_overlord:
            return False
        return True

    def dissolved_this_phase(self, phase: str) -> list[str]:
        return sorted(self.dissolved_log.get(phase, {}))

    def dissolved_overlord(self, minor: str, phase: str) -> Optional[str]:
        return self.dissolved_log.get(phase, {}).get(minor)

    # -- diary/state intents ----------------------------------------------

    def record_fields(self, game: Any, power_name: str, parsed_data: Any) -> None:
        if not puppets_enabled() or not isinstance(parsed_data, dict):
            return
        from .exile import _is_falsy, _is_truthy

        # Minor requests protection.
        request = parsed_data.get("puppet_request")
        if isinstance(request, dict) and power_name in MINOR_POWERS:
            overlord = str(request.get("overlord", "")).upper()
            if overlord in MAJOR_POWERS and not self.is_puppet(power_name):
                year = phase_year(current_phase_of(game))
                if year is None or year > self.cooldown_until_year.get(power_name, -1):
                    self.requests[power_name] = overlord
                    logger.info("[傀儡] %s 请求成为 %s 的傀儡", power_name, overlord)
        if "puppet_request" in parsed_data and parsed_data.get("puppet_request") is None:
            self.requests.pop(power_name, None)

        # Overlord accepts / declines.
        decisions = parsed_data.get("puppet_decisions")
        if isinstance(decisions, dict) and power_name in MAJOR_POWERS:
            for minor, value in decisions.items():
                minor = str(minor).upper()
                if minor not in MINOR_POWERS:
                    continue
                if _is_truthy(value):
                    self.decisions.setdefault(power_name, {})[minor] = True
                elif _is_falsy(value):
                    self.decisions.setdefault(power_name, {})[minor] = False

        # Break free (Spring diaries only).
        if parsed_data.get("break_free") and self.is_puppet(power_name):
            if is_spring_phase(current_phase_of(game)):
                if power_name not in self.pending_breakfree:
                    self.pending_breakfree.append(power_name)
            else:
                logger.info("[傀儡] %s 的脱离声明非春季，已忽略", power_name)

        # Expel a puppet.
        expel = parsed_data.get("expel_puppet")
        if expel and power_name in MAJOR_POWERS:
            minor = str(expel).upper()
            if self.overlord_of(minor) == power_name:
                self.pending_expulsions[power_name] = minor

    def resolve_pending(self, game: Any, phase: str) -> None:
        if not puppets_enabled():
            return
        # Formations: request + acceptance.
        for minor, overlord in list(self.requests.items()):
            decision = self.decisions.get(overlord, {}).pop(minor, None)
            if decision is True:
                if self._can_form(game, minor, overlord, phase):
                    self.form(game, minor, overlord, phase)
                    self.requests.pop(minor, None)
                else:
                    logger.info("[傀儡] %s-%s 条件不符，请求保留", minor, overlord)
            elif decision is False:
                self.requests.pop(minor, None)
                logger.info("[傀儡] %s 拒绝收留 %s", overlord, minor)
        self.decisions.clear()
        # Break-free declarations.
        for minor in list(self.pending_breakfree):
            self.pending_breakfree.remove(minor)
            overlord = self.overlord_of(minor)
            if overlord:
                self.dissolve(game, minor, phase, "春季声明脱离", cooldown=True)
        # Expulsions.
        for overlord, minor in list(self.pending_expulsions.items()):
            self.pending_expulsions.pop(overlord, None)
            if self.overlord_of(minor) == overlord:
                self.dissolve(game, minor, phase, f"{overlord} 驱逐", cooldown=True)

    def _can_form(self, game: Any, minor: str, overlord: str, phase: str) -> bool:
        from .exile import is_normal_active

        if minor in MINOR_POWERS and self.is_puppet(minor):
            return False
        if not self.can_accept(overlord):
            return False
        year = phase_year(phase)
        if year is not None and year <= self.cooldown_until_year.get(minor, -1):
            return False
        return bool(
            is_normal_active(game, minor) and is_normal_active(game, overlord)
        )

    def form(self, game: Any, minor: str, overlord: str, phase: str) -> None:
        self.puppets[minor] = {"overlord": overlord, "since": phase}
        text = f"【傀儡】{minor} 成为 {overlord} 的保护国（傀儡）！"
        self.announce(phase, text)
        logger.info("[傀儡] %s", text)

    def dissolve(
        self, game: Any, minor: str, phase: str, reason: str, cooldown: bool = True,
    ) -> None:
        entry = self.puppets.pop(minor, None)
        if entry is None:
            return
        overlord = str(entry.get("overlord"))
        self.dissolved_log.setdefault(phase, {})[minor] = overlord
        self.directives.pop(minor, None)
        if cooldown:
            year = phase_year(phase)
            if year is not None:
                self.cooldown_until_year[minor] = year
        text = f"【傀儡解除】{minor} 脱离 {overlord}（{reason}），恢复独立。"
        self.announce(phase, text)
        logger.info("[傀儡] %s", text)

    def check_overlords_down(self, game: Any, phase: str) -> list[str]:
        """A fallen overlord frees its puppets (no cooldown).

        Exile-aware: an overlord seeking asylum or in exile still counts as
        present; only a fully-out overlord frees its puppets.
        """
        if not puppets_enabled():
            return []
        from .exile import is_active

        freed = []
        for minor, entry in list(self.puppets.items()):
            overlord = str(entry.get("overlord"))
            if overlord in game.powers and not is_active(game, overlord):
                self.dissolve(game, minor, phase, f"{overlord} 倒下", cooldown=False)
                freed.append(minor)
        return freed

    def check_puppet_losses(self, game: Any, phase: str) -> list[str]:
        """A conquered puppet ceases to be a puppet."""
        if not puppets_enabled():
            return []
        from .exile import is_active

        gone = []
        for minor in list(self.puppets):
            if minor in game.powers and not is_active(game, minor):
                self.puppets.pop(minor, None)
                self.directives.pop(minor, None)
                self.dissolved_log.setdefault(phase, {})[minor] = ""
                gone.append(minor)
                logger.info("[傀儡] %s 已亡国，傀儡关系终止", minor)
        return gone

    # -- overlord command directives --------------------------------------

    def record_directives(self, game: Any, overlord: str, orders: Any, phase: str) -> None:
        """Store an overlord's puppet_orders (effective next movement phase).

        ``orders`` maps minor power -> list of order strings. At most
        ceil(units/2) units per puppet; excess directives are dropped.
        """
        if not puppets_enabled() or not isinstance(orders, dict):
            return
        for minor, minor_orders in orders.items():
            minor = str(minor).upper()
            if self.overlord_of(minor) != overlord or not isinstance(minor_orders, list):
                continue
            power = game.powers.get(minor)
            if power is None:
                continue
            cap = max(1, math.ceil(len(power.units) / 2)) if power.units else 0
            kept: dict[str, str] = {}
            for order in minor_orders:
                text = str(order).upper().strip()
                if len(text) < 2 or text[:2] not in ("A ", "F "):
                    continue
                loc = unit_loc(text)
                if not any(unit_loc(u) == loc for u in power.units):
                    continue
                if loc in kept:
                    continue
                if len(kept) >= cap:
                    logger.info("[傀儡] %s 对 %s 的指挥超出上限（%d 支），多余指令丢弃", overlord, minor, cap)
                    break
                kept[loc] = text
            if kept:
                self.directives[minor] = {
                    "overlord": overlord, "issued_phase": phase, "orders": kept,
                }
                logger.info("[傀儡] %s 下达指挥：%s -> %s", overlord, minor, kept)

    def consume_directives(self, game: Any, phase: str) -> dict[str, dict[str, str]]:
        """Directives issued in an earlier phase, still valid now.

        Consumed (removed) whether or not they end up applying.
        """
        if not puppets_enabled():
            return {}
        ready: dict[str, dict[str, str]] = {}
        for minor, entry in list(self.directives.items()):
            if entry.get("issued_phase") == phase:
                continue  # takes effect next movement phase, not this one
            self.directives.pop(minor, None)
            if self.overlord_of(minor) != entry.get("overlord"):
                continue
            ready[minor] = dict(entry.get("orders") or {})
        return ready

    def pending_directive_text(self, minor: str) -> str:
        entry = self.directives.get(minor)
        if not entry:
            return ""
        orders = entry.get("orders") or {}
        lines = ", ".join(f"{loc}: {order}" for loc, order in sorted(orders.items()))
        return f"宗主国 {entry.get('overlord')} 已下达指挥（下个移动阶段生效，将覆盖你的对应指令）：{lines}。"

    # -- prompts ------------------------------------------------------------

    def diary_instructions(self, game: Any, power_name: str) -> str:
        if not puppets_enabled():
            return ""
        if power_name in MINOR_POWERS and not self.is_puppet(power_name):
            return (
                "【傀儡机制】你可以在日记/状态 JSON 里写 \"puppet_request\": {\"overlord\": \"大国代码\"} 请求成为某大国的保护国（傀儡）；"
                "对方接受后你只能与宗主国私信、不能发全局广播，但宗主国会保护你。写 \"puppet_request\": null 可撤回请求。"
            )
        if self.is_puppet(power_name):
            extra = ""
            if is_spring_phase(current_phase_of(game)):
                extra = "本阶段是春季，你可以在日记里写 \"break_free\": true 声明脱离宗主国（一年内不得再次成为傀儡）。"
            return f"【傀儡身份】你是 {self.overlord_of(power_name)} 的傀儡：你只能与宗主国私信。{extra}"
        if power_name in MAJOR_POWERS:
            return (
                "【傀儡机制】小国请求成为你的傀儡时，在日记/状态 JSON 里写 \"puppet_decisions\": {\"小国代码\": true} 接受、false 拒绝；"
                "每收留一个傀儡，你每个冬季多建造一支（最多 2 个）。写 \"expel_puppet\": \"小国代码\" 可驱逐你的傀儡。"
            )
        return ""

    def context_block_for_power(self, game: Any, power_name: str, phase: str) -> str:
        if not puppets_enabled():
            return ""
        blocks: list[str] = []
        if self.is_puppet(power_name):
            overlord = self.overlord_of(power_name)
            blocks.append(
                f"【傀儡身份】你是 {overlord} 的保护国（傀儡）：你只能与 {overlord} 私信，不能发送全局广播，"
                "不能缔约、宣称。宗主国可以指挥你至多一半的部队（指令延迟一个移动阶段生效，会覆盖你的对应指令）。"
            )
            directive_text = self.pending_directive_text(power_name)
            if directive_text:
                blocks.append(f"【宗主国指挥】{directive_text}")
        else:
            owned = self.puppets_of(power_name)
            if owned:
                blocks.append(
                    f"【你的傀儡】{', '.join(owned)}（每个傀儡使你每冬季多建造一支）。你可以在下单 JSON 里写 "
                    "\"puppet_orders\": {\"傀儡代码\": [\"指令\", ...]} 指挥傀儡部队：至多其部队数的一半（向上取整），"
                    "下个移动阶段生效并覆盖傀儡自己的对应指令。"
                )
            incoming = [m for m, o in self.requests.items() if o == power_name]
            if incoming:
                blocks.append(
                    f"【傀儡请求】{', '.join(incoming)} 请求成为你的傀儡。在日记/状态 JSON 里写 puppet_decisions 接受或拒绝。"
                )
        return "\n\n".join(blocks)
