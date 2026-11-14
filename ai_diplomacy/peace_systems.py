"""Peace-period and accord systems (1937 restart, v3 design).

Four peace-period mechanisms plus the grievance machinery shared with the
puppet system:

- Treaties: public alliances and secret pacts, signed in 1937-1938 via
  diary/state JSON (``treaty_offer`` / ``treaty_accept``). Max 2 per power.
  Minors and breakaway regimes may only accept treaties, never offer them.
  From 1939, treaty partners attacking the same province each gain +1
  strength (engine hook, see MechanicsManager.joint_attack_bonus); attacking
  a public ally is betrayal (-15 satisfaction + global broadcast).
- Claims: up to 3 unowned provinces claimed publicly in the peace period
  (``claims``). An intrusion logs a grievance: attacking the intruder from
  1939 is "avenging" (+5), never acting by 1941 costs -5.
- Reserve bank: unused peace-winter builds are banked (max 2); from 1939
  each winter auto-spends one for +1 build (or one fewer disband).
- Parades: once per peace year, requires >= 4 units (``parade``), +5
  satisfaction at scoring, broadcast.
- Grievances: claim intrusions and puppet attacks share one registry with
  retaliation/deadline evaluation at scoring time.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .mechanics import (
    MINOR_POWERS,
    RESERVE_POWERS,
    current_phase_of,
    is_movement_phase,
    peace_systems_enabled,
    phase_year,
)

logger = logging.getLogger(__name__)

MAX_TREATIES_PER_POWER = 2
MAX_CLAIMS_PER_POWER = 3
MAX_RESERVES = 2
PEACE_LAST_YEAR = 1938
CLAIM_DEADLINE_PHASE = "S1941M"


def _phase_key(phase: str) -> tuple[int, int, int]:
    """Sortable phase key: (year, season, type). S=0, F=1, W=2; M=0, R=1, A=2."""
    from .mechanics import parse_phase

    parsed = parse_phase(phase)
    if not parsed:
        return (0, 0, 0)
    season, year, kind = parsed
    return (year, {"S": 0, "F": 1, "W": 2}[season], {"M": 0, "R": 1, "A": 2}[kind])


def _add_movement_phases(phase: str, count: int) -> str:
    """The movement phase `count` movement phases after `phase`."""
    from .mechanics import parse_phase

    parsed = parse_phase(phase)
    if not parsed:
        return phase
    season, year, _kind = parsed
    steps = {"S": 0, "F": 1}.get(season, 0) + count
    year += steps // 2
    season = "S" if steps % 2 == 0 else "F"
    return f"{season}{year}M"


def _is_peace_year(year: Optional[int]) -> bool:
    return year is not None and year <= PEACE_LAST_YEAR


# ---------------------------------------------------------------------------
# Treaties
# ---------------------------------------------------------------------------

class TreatyRegistry:
    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.treaties: list[dict] = list(data.get("treaties") or [])
        self.offers: dict[str, dict[str, str]] = {
            k: dict(v) for k, v in (data.get("offers") or {}).items()
        }
        self.accepts: dict[str, dict[str, str]] = {
            k: dict(v) for k, v in (data.get("accepts") or {}).items()
        }
        self.announcements: dict[str, list[str]] = {
            k: list(v) for k, v in (data.get("announcements") or {}).items()
        }

    def to_dict(self) -> dict:
        return {
            "treaties": self.treaties,
            "offers": self.offers,
            "accepts": self.accepts,
            "announcements": self.announcements,
        }

    def announce(self, phase: str, text: str) -> None:
        self.announcements.setdefault(phase, []).append(text)

    def active_treaties(self) -> list[dict]:
        return [t for t in self.treaties if t.get("active")]

    def treaty_between(self, a: str, b: str) -> Optional[dict]:
        for treaty in self.active_treaties():
            if {treaty.get("a"), treaty.get("b")} == {a, b}:
                return treaty
        return None

    def partners_of(self, power: str) -> list[str]:
        out = []
        for treaty in self.active_treaties():
            if treaty.get("a") == power:
                out.append(str(treaty.get("b")))
            elif treaty.get("b") == power:
                out.append(str(treaty.get("a")))
        return out

    def count_active(self, power: str) -> int:
        return len(self.partners_of(power))

    def record_fields(self, game: Any, power_name: str, parsed_data: Any, manager: Any) -> None:
        if not peace_systems_enabled() or not isinstance(parsed_data, dict):
            return
        year = phase_year(current_phase_of(game))
        if not _is_peace_year(year):
            return
        puppets = getattr(manager, "puppets", None)
        if puppets is not None and puppets.is_puppet(power_name):
            return  # puppets conduct no diplomacy of their own
        for field, store in (("treaty_offer", self.offers), ("treaty_accept", self.accepts)):
            value = parsed_data.get(field)
            if not isinstance(value, dict):
                continue
            if field == "treaty_offer" and (
                power_name in MINOR_POWERS or power_name in RESERVE_POWERS
            ):
                logger.info("[条约] %s 不能主动缔约（小国/分裂政权只能接受），已忽略", power_name)
                continue
            target = str(value.get("target", "")).upper()
            kind = str(value.get("type", "public")).lower()
            if target == power_name or target not in game.powers or kind not in ("public", "secret"):
                logger.warning("[条约] %s 的 %s 字段无效，已忽略：%r", power_name, field, value)
                continue
            store.setdefault(power_name, {})[target] = kind
            logger.info("[条约] %s %s %s（%s）", power_name, field, target, kind)

    def resolve_pending(self, game: Any, phase: str) -> list[dict]:
        """Match offers with accepts; form treaties (peace years only)."""
        if not peace_systems_enabled():
            return []
        from .exile import is_normal_active

        year = phase_year(phase)
        if not _is_peace_year(year):
            self.offers.clear()
            self.accepts.clear()
            return []
        formed = []
        for offerer, targets in list(self.offers.items()):
            for target, kind in list(targets.items()):
                if self.accepts.get(target, {}).get(offerer) != kind:
                    continue
                if self.treaty_between(offerer, target) is not None:
                    continue
                if not (is_normal_active(game, offerer) and is_normal_active(game, target)):
                    continue
                if self.count_active(offerer) >= MAX_TREATIES_PER_POWER or self.count_active(target) >= MAX_TREATIES_PER_POWER:
                    logger.info("[条约] %s-%s 已达条约上限，无法缔结", offerer, target)
                    continue
                treaty = {
                    "id": f"T{len(self.treaties) + 1}",
                    "type": kind,
                    "a": offerer,
                    "b": target,
                    "signed_phase": phase,
                    "active": True,
                    "revealed": kind == "public",
                }
                self.treaties.append(treaty)
                formed.append(treaty)
                self.offers[offerer].pop(target, None)
                self.accepts[target].pop(offerer, None)
                if kind == "public":
                    text = f"【条约】{offerer} 与 {target} 缔结公开同盟！"
                    self.announce(phase, text)
                    logger.info("[条约] %s", text)
                else:
                    logger.info("[条约] %s 与 %s 缔结秘密协定（仅双方知晓）", offerer, target)
        return formed

    def check_betrayals(
        self, attacks: list[dict], phase: str, satisfaction: Any,
    ) -> list[dict]:
        """From 1939: attacking a treaty partner dissolves the treaty.

        Public treaties: betrayal costs -15 satisfaction + global broadcast.
        Secret treaties: dissolved silently (no betrayal cost).
        """
        if not peace_systems_enabled():
            return []
        year = phase_year(phase)
        if year is None or year < 1939:
            return []
        betrayed = []
        for attack in attacks:
            attacker, owner = attack.get("attacker"), attack.get("owner")
            if not attacker or not owner or attacker == owner:
                continue
            treaty = self.treaty_between(attacker, owner)
            if treaty is None:
                continue
            treaty["active"] = False
            record = {"treaty": treaty["id"], "attacker": attacker, "victim": owner, "phase": phase}
            betrayed.append(record)
            if treaty.get("type") == "public":
                if satisfaction is not None:
                    satisfaction.adjust(attacker, -15, "背弃盟约")
                text = f"【背弃盟约】{attacker} 进攻盟友 {owner}，公开同盟撕毁！"
                self.announce(phase, text)
                logger.info("[条约] %s", text)
            else:
                logger.info("[条约] %s 进攻密约伙伴 %s，秘密协定无声作废", attacker, owner)
        return betrayed

    def check_joint_attacks(self, attacks: list[dict], phase: str) -> list[dict]:
        """A secret treaty is revealed when the partners jointly attack."""
        if not peace_systems_enabled():
            return []
        year = phase_year(phase)
        if year is None or year < 1939:
            return []
        by_dest: dict[str, set[str]] = {}
        for attack in attacks:
            by_dest.setdefault(str(attack.get("dest")), set()).add(str(attack.get("attacker")))
        revealed = []
        for dest, attackers in by_dest.items():
            if len(attackers) < 2:
                continue
            attackers = sorted(attackers)
            for i, a in enumerate(attackers):
                for b in attackers[i + 1:]:
                    treaty = self.treaty_between(a, b)
                    if treaty and treaty.get("type") == "secret" and not treaty.get("revealed"):
                        treaty["revealed"] = True
                        text = f"【密约曝光】{a} 与 {b} 联合进攻 {dest}，两国的秘密协定暴露！"
                        self.announce(phase, text)
                        revealed.append({"treaty": treaty["id"], "a": a, "b": b, "dest": dest})
                        logger.info("[条约] %s", text)
        return revealed

    def reveal_one_secret(self, phase: str, rng: Any) -> Optional[dict]:
        """A leak event exposes one random secret treaty (if any exist)."""
        hidden = [t for t in self.active_treaties() if t.get("type") == "secret" and not t.get("revealed")]
        if not hidden:
            return None
        treaty = rng.choice(hidden)
        treaty["revealed"] = True
        text = f"【密约曝光】密电显示 {treaty['a']} 与 {treaty['b']} 签有秘密协定！"
        self.announce(phase, text)
        logger.info("[条约] %s", text)
        return treaty


# ---------------------------------------------------------------------------
# Claims + grievances
# ---------------------------------------------------------------------------

class ClaimRegistry:
    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.claims: dict[str, list[str]] = {
            k: list(v) for k, v in (data.get("claims") or {}).items()
        }

    def to_dict(self) -> dict:
        return {"claims": self.claims}

    def claims_of(self, power: str) -> list[str]:
        return list(self.claims.get(power, []))

    def record_fields(self, game: Any, power_name: str, parsed_data: Any, manager: Any) -> None:
        if not peace_systems_enabled() or not isinstance(parsed_data, dict):
            return
        year = phase_year(current_phase_of(game))
        if not _is_peace_year(year):
            return
        puppets = getattr(manager, "puppets", None)
        if puppets is not None and puppets.is_puppet(power_name):
            return
        value = parsed_data.get("claims")
        if not isinstance(value, list):
            return
        owned = {str(c).upper()[:3] for p in game.powers.values() for c in p.centers}
        loc_type = getattr(getattr(game, "map", None), "loc_type", {}) or {}
        valid: list[str] = []
        for item in value:
            prov = str(item).upper()[:3]
            if prov in owned or prov in valid:
                continue
            if loc_type and loc_type.get(prov) == "WATER":
                continue
            valid.append(prov)
        if len(valid) > MAX_CLAIMS_PER_POWER:
            valid = valid[:MAX_CLAIMS_PER_POWER]
        self.claims[power_name] = valid
        if valid:
            logger.info("[宣称] %s 宣称势力范围：%s", power_name, ", ".join(valid))

    def expire(self, phase: str) -> bool:
        """Claims lapse at the end of 1938 (grievances survive)."""
        year = phase_year(phase)
        if year is not None and year >= 1939 and self.claims:
            self.claims = {}
            logger.info("[宣称] 势力范围宣称已过期（1938 年底）")
            return True
        return False


class GrievanceRegistry:
    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.grievances: list[dict] = list(data.get("grievances") or [])
        self.announcements: dict[str, list[str]] = {
            k: list(v) for k, v in (data.get("announcements") or {}).items()
        }

    def to_dict(self) -> dict:
        return {"grievances": self.grievances, "announcements": self.announcements}

    def announce(self, phase: str, text: str) -> None:
        self.announcements.setdefault(phase, []).append(text)

    def add(
        self, kind: str, claimant: str, intruder: str, phase: str,
        province: Optional[str] = None, deadline_phase: Optional[str] = None,
    ) -> Optional[dict]:
        for existing in self.grievances:
            if (
                not existing.get("resolved")
                and existing.get("kind") == kind
                and existing.get("claimant") == claimant
                and existing.get("intruder") == intruder
                and existing.get("province") == province
            ):
                return None
        if deadline_phase is None:
            deadline_phase = (
                CLAIM_DEADLINE_PHASE if kind == "claim"
                else _add_movement_phases(phase, 2)
            )
        record = {
            "kind": kind, "claimant": claimant, "intruder": intruder,
            "province": province, "created_phase": phase,
            "deadline_phase": deadline_phase, "resolved": False, "outcome": None,
        }
        self.grievances.append(record)
        logger.info("[积怨] %s：%s 对 %s（%s），截止 %s", kind, claimant, intruder, province or "-", deadline_phase)
        return record

    def check_claim_intrusions(self, game: Any, phase: str, claims: ClaimRegistry) -> list[dict]:
        """Peace phases: a foreign unit in a claimed province logs a grievance."""
        if not peace_systems_enabled() or not is_movement_phase(phase):
            return []
        year = phase_year(phase)
        if not _is_peace_year(year):
            return []
        created = []
        for claimant, provinces in claims.claims.items():
            for province in provinces:
                for name, power in game.powers.items():
                    if name == claimant:
                        continue
                    if any(str(u).split()[1].split("/")[0].upper()[:3] == province for u in power.units):
                        record = self.add("claim", claimant, name, phase, province)
                        if record:
                            created.append(record)
        return created

    def check_puppet_attacks(
        self, centers_before: dict[str, str], game: Any, phase: str, puppets: Any,
    ) -> list[dict]:
        """A puppet losing a center to an attacker gives its overlord a casus belli."""
        if not peace_systems_enabled() or puppets is None:
            return []
        created = []
        centers_after = {
            str(c).upper()[:3]: name for name, p in game.powers.items() for c in p.centers
        }
        for province, before_owner in centers_before.items():
            after_owner = centers_after.get(province)
            if after_owner in (None, before_owner):
                continue
            overlord = puppets.overlord_of(before_owner)
            if overlord is None or after_owner == overlord:
                continue
            record = self.add("puppet_attack", overlord, after_owner, phase, province)
            if record:
                created.append(record)
                text = f"【宣战借口】{after_owner} 攻占 {before_owner}（{overlord} 的傀儡）的 {province}！"
                self.announce(phase, text)
                logger.info("[积怨] %s", text)
        return created

    def evaluate(self, attacks: list[dict], phase: str, satisfaction: Any) -> list[dict]:
        """Resolve grievances: retaliation pays, inaction by the deadline costs."""
        if not peace_systems_enabled():
            return []
        year = phase_year(phase)
        resolved = []
        for grievance in self.grievances:
            if grievance.get("resolved"):
                continue
            claimant, intruder = grievance.get("claimant"), grievance.get("intruder")
            retaliated = any(
                a.get("attacker") == claimant
                and (a.get("owner") == intruder or a.get("occupant") == intruder)
                for a in attacks
            )
            if retaliated and (grievance.get("kind") != "claim" or (year is not None and year >= 1939)):
                grievance["resolved"] = True
                grievance["outcome"] = "retaliated"
                if satisfaction is not None:
                    satisfaction.adjust(claimant, 5, "雪耻" if grievance.get("kind") == "claim" else "护犊")
                text = f"【雪耻】{claimant} 向 {intruder} 动手，积怨得报！"
                self.announce(phase, text)
                resolved.append(grievance)
                continue
            if _phase_key(phase) >= _phase_key(str(grievance.get("deadline_phase"))):
                grievance["resolved"] = True
                grievance["outcome"] = "expired"
                penalty = -5 if grievance.get("kind") == "claim" else -10
                if satisfaction is not None:
                    satisfaction.adjust(claimant, penalty, "忍气吞声" if penalty == -5 else "软弱")
                text = f"【积怨未报】{claimant} 迟迟不对 {intruder} 动手，国内失望。"
                self.announce(phase, text)
                resolved.append(grievance)
        return resolved


# ---------------------------------------------------------------------------
# Reserve bank + parades
# ---------------------------------------------------------------------------

class ReserveBank:
    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.reserves: dict[str, int] = dict(data.get("reserves") or {})
        # Powers spending a reserve THIS winter (fixed at winter start so the
        # engine's repeated balance queries all see the same number).
        self.active_spend: list[str] = list(data.get("active_spend") or [])

    def to_dict(self) -> dict:
        return {
            "reserves": self.reserves,
            "active_spend": self.active_spend,
        }

    def balance_of(self, power: str) -> int:
        return int(self.reserves.get(power, 0))

    def bank_unused(self, power: str, unused: int, free_home_centers: int, phase: str) -> int:
        """Bank unused peace-winter builds (max 2). Returns the amount banked."""
        if not peace_systems_enabled():
            return 0
        year = phase_year(phase)
        if not _is_peace_year(year):
            return 0
        room = MAX_RESERVES - self.balance_of(power)
        amount = max(0, min(int(unused), int(free_home_centers), room))
        if amount:
            self.reserves[power] = self.balance_of(power) + amount
            logger.info("[预备役] %s 存入 %d 个预备役名额（现 %d）", power, amount, self.reserves[power])
        return amount

    def begin_winter(self, phase: str) -> None:
        """Auto-spend one banked reserve per power (1939+ winters).

        Spending is automatic: a reserve is always beneficial (+1 build or
        one fewer disband) and has no other use or expiry, so there is no
        reason to hoard it. Fixed at winter start so the engine's repeated
        balance queries all see the same number.
        """
        self.active_spend = []
        if not peace_systems_enabled():
            return
        year = phase_year(phase)
        if year is None or year < 1939:
            return
        for power in sorted(self.reserves):
            if self.balance_of(power) > 0:
                self.reserves[power] = self.balance_of(power) - 1
                self.active_spend.append(power)
                logger.info(
                    "[预备役] %s 自动动用 1 个预备役名额（剩余 %d）",
                    power, self.balance_of(power),
                )

    def spend_bonus(self, power: str) -> int:
        """+1 build if the power is spending a reserve this winter (pure query)."""
        return 1 if power in self.active_spend else 0

    def complete_winter(self) -> None:
        self.active_spend = []



class ParadeLog:
    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.parades: dict[str, list[int]] = {
            k: list(v) for k, v in (data.get("parades") or {}).items()
        }
        self.announcements: dict[str, list[str]] = {
            k: list(v) for k, v in (data.get("announcements") or {}).items()
        }

    def to_dict(self) -> dict:
        return {"parades": self.parades, "announcements": self.announcements}

    def announce(self, phase: str, text: str) -> None:
        self.announcements.setdefault(phase, []).append(text)

    def parade_held(self, power: str, phase: str) -> bool:
        year = phase_year(phase)
        return year is not None and year in self.parades.get(power, [])

    def record_fields(self, game: Any, power_name: str, parsed_data: Any) -> None:
        if not peace_systems_enabled() or not isinstance(parsed_data, dict):
            return
        if not parsed_data.get("parade"):
            return
        phase = current_phase_of(game)
        year = phase_year(phase)
        if not _is_peace_year(year) or not is_movement_phase(phase):
            return
        if self.parade_held(power_name, phase):
            return
        power = game.powers.get(power_name)
        if power is None or len(power.units) < 4:
            logger.info("[阅兵] %s 部队不足 4 支，阅兵无效", power_name)
            return
        self.parades.setdefault(power_name, []).append(int(year))
        text = f"【阅兵】{power_name} 举行盛大阅兵/军事演习，举国振奋！"
        self.announce(phase, text)
        logger.info("[阅兵] %s", text)


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------

class PeaceSystems:
    """Treaties + claims + grievances + reserves + parades, one state blob."""

    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.treaties = TreatyRegistry(data.get("treaties"))
        self.claims = ClaimRegistry(data.get("claims"))
        self.grievances = GrievanceRegistry(data.get("grievances"))
        self.reserves = ReserveBank(data.get("reserves"))
        self.parades = ParadeLog(data.get("parades"))

    def to_dict(self) -> dict:
        return {
            "treaties": self.treaties.to_dict(),
            "claims": self.claims.to_dict(),
            "grievances": self.grievances.to_dict(),
            "reserves": self.reserves.to_dict(),
            "parades": self.parades.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "PeaceSystems":
        return cls(data or {})

    # -- diary/state intents ------------------------------------------------

    def record_fields(self, game: Any, power_name: str, parsed_data: Any, manager: Any) -> None:
        self.treaties.record_fields(game, power_name, parsed_data, manager)
        self.claims.record_fields(game, power_name, parsed_data, manager)
        self.parades.record_fields(game, power_name, parsed_data)

    def resolve_pending(self, game: Any, phase: str) -> None:
        self.treaties.resolve_pending(game, phase)
        self.claims.expire(phase)

    # -- prompts ------------------------------------------------------------

    def diary_instructions(self, game: Any, power_name: str) -> str:
        if not peace_systems_enabled():
            return ""
        year = phase_year(current_phase_of(game))
        if not _is_peace_year(year):
            return ""
        lines = [
            "【和平期机制】1937-1938 年为和平期，你可以在日记/状态 JSON 里使用以下可选字段（全部可选）：",
            '- "treaty_offer": {"target": "国家代码", "type": "public"|"secret"}：向对方提议缔约（公开同盟会广播，秘密协定仅双方知晓）；对方在日记里写 "treaty_accept": {"target": "你的代码", "type": "同上"} 即缔结。每国最多 2 份条约。',
            '- "claims": ["省代码", ...]：公开宣称最多 3 个无主省份为势力范围（本字段会覆盖你之前的宣称）；他国部队进入被宣称的省会记一笔积怨。',
            '- "parade": true：举行阅兵/演习（须至少 4 支部队，每年一次），提振国内士气。',
        ]
        return "\n".join(lines)

    def context_block_for_power(self, game: Any, power_name: str, phase: str) -> str:
        if not peace_systems_enabled():
            return ""
        blocks: list[str] = []
        year = phase_year(phase)
        if _is_peace_year(year) and is_movement_phase(phase):
            blocks.append(
                "【和平期】1937-1938 年为和平期：进攻外国单位或外国补给站的指令会被改为驻守；"
                "你可以进驻无主省份、建造、缔约、宣称势力范围、举行阅兵、收留傀儡。1939 年起自由开战。"
            )
        own_treaties = [
            t for t in self.treaties.active_treaties()
            if power_name in (t.get("a"), t.get("b"))
        ]
        if own_treaties:
            parts = []
            for treaty in own_treaties:
                other = treaty["b"] if treaty["a"] == power_name else treaty["a"]
                kind = "公开同盟" if treaty.get("type") == "public" else "秘密协定"
                parts.append(f"{other}（{kind}，{treaty.get('signed_phase')}）")
            blocks.append("【你的条约】" + "；".join(parts) + "。")
        pending_in = [
            f"{offerer}（{kind}）"
            for offerer, targets in self.treaties.offers.items()
            for target, kind in targets.items()
            if target == power_name
        ]
        if pending_in:
            blocks.append("【待复缔约提议】" + "、".join(pending_in) + " 向你提议缔约，在日记里写 treaty_accept 接受。")
        all_claims = [
            f"{p}：{','.join(ps)}" for p, ps in sorted(self.claims.claims.items()) if ps
        ]
        if all_claims:
            blocks.append("【势力范围宣称】" + "；".join(all_claims) + "。")
        balance = self.reserves.balance_of(power_name)
        if balance:
            blocks.append(f"【预备役储备】你存有 {balance} 个预备役名额；1939 年后的每个冬季自动动用 1 个（多造一支或少解散一支）。")
        return "\n\n".join(blocks)
