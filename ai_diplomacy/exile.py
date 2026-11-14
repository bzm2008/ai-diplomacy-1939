"""Exile governments (1939): a defeated power may ask a surviving power for
asylum; only an explicit ``accept`` in the host's diary JSON founds the exile.
Exiles keep negotiating; the host may fund one restoration army per year and
may expel the exile. Restoration on recapturing any home supply center.

All new JSON fields (``exile_decisions`` / ``fund_exile_unit`` /
``expel_exile``) are optional and parsed tolerantly: a malformed field is
ignored with a warning and never retries, so one bad field cannot stall a game.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .mechanics import (
    POWERS_1939,
    add_unit,
    current_phase_of,
    exile_enabled,
    home_centers_of,
    is_movement_phase,
    phase_year,
    unit_loc,
)

logger = logging.getLogger(__name__)

SEEKING_PHASES = 2
POLAND_SEEKING_PHASES = 3
MAX_HOSTED_PER_HOST = 1
RETURN_PHASES = 1  # seeking window after expulsion / host collapse


def _is_engine_eliminated(game: Any, power_name: str) -> bool:
    power = game.powers.get(power_name)
    return bool(power is not None and power.is_eliminated())


def get_exile_registry(game: Any) -> "ExileRegistry":
    from .mechanics import get_mechanics_manager

    return get_mechanics_manager(game).exile


def is_active(game: Any, power_name: str) -> bool:
    """Participation gate: alive, seeking asylum, or an established exile.

    This replaces ``is_eliminated()`` at every participation call site when
    the exile system is on; with ``DIP_EXILE=0`` it is exactly
    ``not is_eliminated()``.
    """
    if not _is_engine_eliminated(game, power_name):
        return True
    if not exile_enabled():
        return False
    manager = getattr(game, "mechanics_manager", None)
    if manager is None:
        return False
    registry = manager.exile
    return power_name in registry.seeking or power_name in registry.exiled


def is_normal_active(game: Any, power_name: str) -> bool:
    """Alive and neither seeking nor exiled (event targeting eligibility)."""
    if _is_engine_eliminated(game, power_name):
        return False
    if not exile_enabled():
        return True
    manager = getattr(game, "mechanics_manager", None)
    if manager is None:
        return True
    registry = manager.exile
    return power_name not in registry.seeking and power_name not in registry.exiled


def _next_movement_phase(phase: str) -> str:
    """Name of the next movement phase after ``phase`` (S->F, F/W->next S)."""
    from .mechanics import parse_phase

    parsed = parse_phase(phase)
    if not parsed:
        return phase
    season, year, _ = parsed
    if season == "S":
        return f"F{year}M"
    return f"S{year + 1}M"


class ExileRegistry:
    """Exile state. JSON-serializable; pending intents are transient."""

    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.seeking: dict[str, dict] = {k: dict(v) for k, v in (data.get("seeking") or {}).items()}
        self.exiled: dict[str, dict] = {k: dict(v) for k, v in (data.get("exiled") or {}).items()}
        self.processed: dict[str, str] = dict(data.get("processed") or {})
        self.funded_years: dict[str, list[int]] = {
            k: [int(y) for y in v] for k, v in (data.get("funded_years") or {}).items()
        }
        self.pending_funding: dict[str, str] = dict(data.get("pending_funding") or {})
        self.winter_funding: dict[str, str] = dict(data.get("winter_funding") or {})
        self.announcements: list[dict] = [dict(a) for a in data.get("announcements", [])]
        self.decisions_log: list[dict] = [dict(d) for d in data.get("decisions_log", [])]
        # Transient intents recorded during diary/state-update parsing.
        self.pending_decisions: list[dict] = []
        self.pending_expulsions: list[str] = []
        self.pending_funding_intents: list[str] = []

    def to_dict(self) -> dict:
        return {
            "seeking": {k: dict(v) for k, v in self.seeking.items()},
            "exiled": {k: dict(v) for k, v in self.exiled.items()},
            "processed": dict(self.processed),
            "funded_years": {k: list(v) for k, v in self.funded_years.items()},
            "pending_funding": dict(self.pending_funding),
            "winter_funding": dict(self.winter_funding),
            "announcements": [dict(a) for a in self.announcements],
            "decisions_log": [dict(d) for d in self.decisions_log],
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ExileRegistry":
        return cls(data or {})

    # -- queries ----------------------------------------------------------

    def is_exiled(self, power_name: str) -> bool:
        return power_name in self.exiled

    def is_seeking(self, power_name: str) -> bool:
        return power_name in self.seeking

    def host_of(self, power_name: str) -> Optional[str]:
        entry = self.exiled.get(power_name)
        return entry.get("host") if entry else None

    def hosted_by(self, host: str) -> Optional[str]:
        for exile_power, entry in self.exiled.items():
            if entry.get("host") == host:
                return exile_power
        return None

    def can_host(self, game: Any, host: str) -> bool:
        from .mechanics import MAJOR_POWERS

        if host not in MAJOR_POWERS:
            return False
        if not is_normal_active(game, host):
            return False
        return self.hosted_by(host) is None

    # -- lifecycle ----------------------------------------------------------

    def seed_processed(self, game: Any) -> None:
        """Mark powers already eliminated when the registry first appears, so
        a resumed game does not resurrect long-dead powers as asylum seekers."""
        phase = current_phase_of(game)
        for name in game.powers:
            if _is_engine_eliminated(game, name) and name not in self.processed:
                self.processed[name] = phase

    def detect_triggers(self, game: Any, phase: str) -> list[str]:
        """After a phase resolves: newly eliminated powers enter seeking."""
        if not exile_enabled():
            return []
        from .mechanics import MAJOR_POWERS

        triggered = []
        for name in sorted(game.powers):
            if name not in MAJOR_POWERS:
                continue
            if not _is_engine_eliminated(game, name):
                continue
            if name in self.seeking or name in self.exiled or name in self.processed:
                continue
            window = POLAND_SEEKING_PHASES if name == "POLAND" else SEEKING_PHASES
            self.seeking[name] = {"phases_left": window, "since": phase}
            self.processed[name] = phase
            triggered.append(name)
            logger.info("[流亡] %s 战败，进入待流亡状态（求助期 %d 个移动阶段）", name, window)
        return triggered

    def end_movement_phase(self, game: Any, phase: str) -> list[str]:
        """Count down seeking windows after a movement phase resolves."""
        if not exile_enabled() or not is_movement_phase(phase):
            return []
        expired = []
        for name, entry in list(self.seeking.items()):
            if entry.get("since") == phase:
                continue  # triggered (or returned) this very phase
            entry["phases_left"] = int(entry.get("phases_left", 0)) - 1
            if entry["phases_left"] <= 0:
                del self.seeking[name]
                expired.append(name)
                self._announce(game, phase, f"{name} 流亡求助无果，正式退出历史舞台。")
                logger.info("[流亡] %s 求助期满无人收留，正式淘汰", name)
        return expired

    def check_restorations(self, game: Any, phase: str) -> list[str]:
        """Exiles that retook a home center return to normal status."""
        if not exile_enabled():
            return []
        restored = []
        for name in list(self.exiled):
            owned = {str(c).upper()[:3] for c in game.powers[name].centers}
            if any(home in owned for home in home_centers_of(game, name)):
                host = self.exiled[name].get("host")
                del self.exiled[name]
                self.processed.pop(name, None)
                restored.append(name)
                self._announce(game, phase, f"{name} 夺回本土补给中心，流亡政府还乡，恢复正常国家身份（原收留国：{host}）。")
                logger.info("[流亡] %s 还乡，流亡状态解除", name)
        return restored

    def check_hosts_down(self, game: Any, phase: str) -> list[str]:
        """When a host falls, its exile returns to seeking with 1 phase."""
        if not exile_enabled():
            return []
        returned = []
        for name, entry in list(self.exiled.items()):
            host = entry.get("host")
            if host and _is_engine_eliminated(game, host):
                del self.exiled[name]
                self.seeking[name] = {"phases_left": RETURN_PHASES, "since": phase}
                returned.append(name)
                self._announce(game, phase, f"收留国 {host} 倒下，{name} 流亡政府回到待流亡状态（求助期 1 个阶段）。")
                logger.info("[流亡] 收留国 %s 倒下，%s 回到待流亡", host, name)
        return returned

    # -- diary / state-update intents (tolerant parsing) --------------------

    def record_fields(self, game: Any, power_name: str, parsed_data: Any) -> None:
        """Record exile JSON fields from a parsed diary/state-update object.

        Malformed values are ignored with a warning; nothing here retries.
        """
        if not exile_enabled() or not isinstance(parsed_data, dict):
            return
        decisions = parsed_data.get("exile_decisions")
        if decisions is not None:
            if isinstance(decisions, dict):
                for target, verdict in decisions.items():
                    target_name = str(target).upper()
                    if target_name in POWERS_1939 and _is_truthy(verdict):
                        decision = "accept"
                    elif target_name in POWERS_1939 and _is_falsy(verdict):
                        decision = "decline"
                    else:
                        logger.warning("[流亡] %s 的 exile_decisions 条目无效已忽略：%r -> %r", power_name, target, verdict)
                        continue
                    self.pending_decisions.append(
                        {"decider": power_name, "target": target_name, "decision": decision}
                    )
            else:
                logger.warning("[流亡] %s 的 exile_decisions 不是对象已忽略：%r", power_name, type(decisions).__name__)
        fund = parsed_data.get("fund_exile_unit")
        if fund is not None:
            if _is_truthy(fund):
                self.pending_funding_intents.append(power_name)
            elif not _is_falsy(fund):
                logger.warning("[流亡] %s 的 fund_exile_unit 值无效已忽略：%r", power_name, fund)
        expel = parsed_data.get("expel_exile")
        if expel is not None:
            if _is_truthy(expel):
                self.pending_expulsions.append(power_name)
            elif not _is_falsy(expel):
                logger.warning("[流亡] %s 的 expel_exile 值无效已忽略：%r", power_name, expel)

    def resolve_pending(self, game: Any, phase: str) -> None:
        """Apply recorded intents in deterministic order (after diary tasks)."""
        if not exile_enabled():
            self.pending_decisions.clear()
            self.pending_expulsions.clear()
            self.pending_funding_intents.clear()
            return
        year = phase_year(phase)
        for intent in sorted(self.pending_decisions, key=lambda d: (d["decider"], d["target"])):
            self._apply_decision(game, phase, intent)
        self.pending_decisions.clear()
        for host in sorted(set(self.pending_expulsions)):
            self._apply_expulsion(game, phase, host)
        self.pending_expulsions.clear()
        for host in sorted(set(self.pending_funding_intents)):
            self._apply_funding_intent(game, phase, host, year)
        self.pending_funding_intents.clear()

    def _apply_decision(self, game: Any, phase: str, intent: dict) -> None:
        decider, target, decision = intent["decider"], intent["target"], intent["decision"]
        self.decisions_log.append({"phase": phase, "decider": decider, "target": target, "decision": decision})
        if decision != "accept":
            logger.info("[流亡] %s 拒绝收留 %s", decider, target)
            return
        if target not in self.seeking:
            logger.info("[流亡] %s 同意收留 %s，但 %s 已不在待流亡状态，忽略", decider, target, target)
            return
        if not self.can_host(game, decider):
            logger.info("[流亡] %s 同意收留 %s，但 %s 不能收留（已收留他国或自身不在正常状态），忽略", decider, target, decider)
            return
        del self.seeking[target]
        self.exiled[target] = {"host": decider, "since": phase}
        self._announce(game, phase, f"{target} 流亡政府成立，收留国：{decider}。")
        logger.info("[流亡] %s 流亡政府在 %s 成立", target, decider)

    def _apply_expulsion(self, game: Any, phase: str, host: str) -> None:
        exile_power = self.hosted_by(host)
        if exile_power is None:
            return
        del self.exiled[exile_power]
        self.seeking[exile_power] = {"phases_left": RETURN_PHASES, "since": phase}
        self._announce(game, phase, f"{host} 驱逐了 {exile_power} 流亡政府，{exile_power} 回到待流亡状态（求助期 1 个阶段）。")
        logger.info("[流亡] %s 驱逐 %s 流亡政府", host, exile_power)

    def _apply_funding_intent(self, game: Any, phase: str, host: str, year: Optional[int]) -> None:
        exile_power = self.hosted_by(host)
        if exile_power is None:
            logger.info("[流亡] %s 想资助复国军，但它没有收留流亡政府，忽略", host)
            return
        if year is not None and year in self.funded_years.get(host, []):
            logger.info("[流亡] %s 今年已资助过复国军，忽略重复资助", host)
            return
        self.pending_funding[host] = exile_power
        logger.info("[流亡] %s 决定在冬季资助 %s 一支复国军", host, exile_power)

    # -- winter funding -------------------------------------------------------

    def begin_winter(self, game: Any, phase: str) -> None:
        """Finalize funding for this winter: host must be able to build and
        have a free home center for the restoration army."""
        if not exile_enabled():
            self.pending_funding.clear()
            return
        from .mechanics import get_mechanics_manager

        manager = get_mechanics_manager(game)
        for host, exile_power in sorted(self.pending_funding.items()):
            if self.hosted_by(host) != exile_power:
                logger.info("[流亡] %s 的资助失效：%s 已不在其收留之下", host, exile_power)
                continue
            power = game.powers.get(host)
            if power is None:
                continue
            balance = len(power.centers) - len(power.units)
            try:
                from .cards import get_card_manager

                card_manager = get_card_manager(game)
                if hasattr(card_manager, "adjustment_balance"):
                    balance = card_manager.adjustment_balance(game, host)
            except Exception:
                pass
            balance = manager.adjust_build_balance(game, host, balance)
            if balance <= 0:
                logger.info("[流亡] %s 本冬季无法建造，资助 %s 顺延失效", host, exile_power)
                continue
            if _free_home_center(game, host) is None:
                logger.info("[流亡] %s 没有空闲本土中心，资助 %s 失败", host, exile_power)
                continue
            self.winter_funding[host] = exile_power
            logger.info("[流亡] %s 本冬季资助 %s 复国军（少建一支）", host, exile_power)
        self.pending_funding.clear()

    def complete_winter(self, game: Any, phase: str) -> None:
        """Place funded restoration armies after the adjustment resolves."""
        if not exile_enabled():
            self.winter_funding.clear()
            return
        year = phase_year(phase)
        for host, exile_power in sorted(self.winter_funding.items()):
            site = _free_home_center(game, host)
            if site is None:
                logger.warning("[流亡] 冬季结束时 %s 已无空闲本土中心，%s 的复国军未能组建", host, exile_power)
                continue
            if add_unit(game, exile_power, f"A {site}"):
                logger.info("[流亡] %s 的复国军在 %s（%s 本土）组建", exile_power, site, host)
                if year is not None:
                    self.funded_years.setdefault(host, []).append(year)
        self.winter_funding.clear()

    # -- announcements & context ----------------------------------------------

    def _announce(self, game: Any, phase: str, text: str) -> None:
        self.announcements.append(
            {"phase": phase, "show_phase": _next_movement_phase(phase), "text": text}
        )
        if len(self.announcements) > 50:
            self.announcements = self.announcements[-50:]

    def announcements_for(self, phase: str) -> list[str]:
        return [a["text"] for a in self.announcements if a.get("show_phase") == phase]

    def context_block_for_power(self, game: Any, power_name: str, phase: str) -> str:
        blocks: list[str] = []
        notices = self.announcements_for(phase)
        if notices:
            blocks.append("# 流亡政府公告\n" + "\n".join(f"- {text}" for text in notices))
        if power_name in self.seeking:
            left = int(self.seeking[power_name].get("phases_left", 0))
            plural = "两国" if power_name == "POLAND" else "一国"
            blocks.append(
                "# 你正在寻求流亡\n"
                "你已被击败（0 个补给中心、0 支部队）。在谈判中向存活国家写信请求庇护，"
                f"每阶段限向{plural}求助；对方须在谈判日记里明确同意，你才能成立流亡政府。"
                f"剩余求助时间：{left} 个移动阶段。被拒或无人回应时，下阶段可换一国再试。"
            )
        elif power_name in self.exiled:
            host = self.exiled[power_name].get("host")
            blocks.append(
                "# 流亡政府状态\n"
                f"你的流亡政府设在 {host}。夺回任意一个本土补给中心即可复国；"
                "收留国每年冬季可资助你一支复国军，也可能驱逐你。"
            )
        hosted = self.hosted_by(power_name)
        if hosted:
            blocks.append(
                "# 你收留的流亡政府\n"
                f"{hosted} 的流亡政府在你这里。在谈判日记 JSON 里写 "
                "`\"expel_exile\": true` 可驱逐它；在冬季前的状态更新里写 "
                "`\"fund_exile_unit\": true` 可资助它一支复国军（你本冬季少建一支，每年限一支）。"
            )
        seekers = sorted(self.seeking)
        if seekers and power_name not in self.seeking and power_name not in self.exiled:
            if self.can_host(game, power_name):
                blocks.append(
                    "# 流亡求助\n"
                    f"{'、'.join(seekers)} 正在寻求流亡庇护。如你愿意收留，在谈判日记 JSON 里写 "
                    "`\"exile_decisions\": {\"<国家>\": \"accept\"}`；不表态视为拒绝。"
                    "一国最多收留一个流亡政府。"
                )
        return "\n\n".join(blocks)

    def diary_instructions(self, game: Any, power_name: str) -> str:
        """Short JSON-field hints appended to diary/state-update prompts when
        relevant; empty when the power has no exile decisions to make."""
        hints: list[str] = []
        if self.seeking and self.can_host(game, power_name):
            hints.append(
                "如有国家向你寻求流亡庇护，可在 JSON 中加 "
                "`\"exile_decisions\": {\"<国家>\": \"accept\"/\"decline\"}`；不写视为拒绝。"
            )
        if self.hosted_by(power_name):
            hints.append(
                "你在收留流亡政府：JSON 中 `\"expel_exile\": true` 驱逐它，"
                "`\"fund_exile_unit\": true` 在冬季资助它一支复国军（你少建一支，每年限一支）。"
            )
        if not hints:
            return ""
        return "\n# 流亡相关（仅当适用时填写，可省略）\n" + "\n".join(hints) + "\n"


def _is_truthy(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in {
        "true", "yes", "1", "accept", "accepted", "agree", "agreed", "allow",
    }


def _is_falsy(value: Any) -> bool:
    if value is False or value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in {
        "false", "no", "0", "", "decline", "declined", "refuse", "refused", "reject",
    }


def _free_home_center(game: Any, power_name: str) -> Optional[str]:
    power = game.powers.get(power_name)
    if power is None:
        return None
    occupied = {unit_loc(u) for p in game.powers.values() for u in p.units}
    owned = {str(c).upper()[:3] for c in power.centers}
    for home in home_centers_of(game, power_name):
        if home in owned and home not in occupied:
            return home
    return None
