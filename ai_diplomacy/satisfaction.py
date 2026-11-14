"""Domestic satisfaction system (1937 restart, v3 design).

Every power has a hidden 0-100 satisfaction score, scored once per movement
phase by the Luna judge from factual phase data and a per-power rubric.
Numbers and tiers NEVER enter any prompt: powers only see symptom text
("国内动态") in the phase summary (战报), and exact values live only in the
audit log (this module's state, persisted in the save file).

Thresholds (per-phase change capped at +/-15):
  >=80 振奋: next winter +1 build.
  40-79: no effect.
  20-39 不满: next winter -1 build, and all units Hold/Support-only next
  movement phase.
  <20 (2 consecutive movement phases, or a single phase <10): the power
  splits (majors only, max 2 splits per power per game); minors instead
  defect or reconcile, puppets break free or switch overlords.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Optional

from .mechanics import (
    MAJOR_POWERS,
    MINOR_POWERS,
    RESERVE_POWERS,
    add_unit,
    current_phase_of,
    home_centers_of,
    is_movement_phase,
    phase_year,
    remove_unit,
    satisfaction_enabled,
    unit_loc,
)

logger = logging.getLogger(__name__)

# Capitals never secede.
CAPITALS = {
    "BRITAIN": "LON",
    "FRANCE": "PAR",
    "GERMANY": "BER",
    "ITALY": "ROM",
    "SOVIET": "MOS",
    "POLAND": "WAR",
    "SPAIN": "SPA",
    "USA": "WAS",
}

PROVINCE_ZH = {
    "BER": "柏林", "KIE": "基尔", "MUN": "慕尼黑", "RUH": "鲁尔", "SIL": "西里西亚",
    "WAR": "华沙", "GAL": "加利西亚", "PRU": "普鲁士",
    "MOS": "莫斯科", "SEV": "塞瓦斯托波尔", "STP": "圣彼得堡", "UKR": "乌克兰", "ARM": "亚美尼亚",
    "BRE": "布列塔尼", "MAR": "马赛", "PAR": "巴黎", "BUR": "勃艮第", "GAS": "加斯科涅", "PIC": "皮卡第",
    "NAP": "那不勒斯", "ROM": "罗马", "VEN": "威尼斯", "APU": "阿普利亚", "TUS": "托斯卡纳", "PIE": "皮埃蒙特",
    "SPA": "西班牙本土", "NAF": "北非",
    "EDI": "爱丁堡", "LON": "伦敦", "LVP": "利物浦", "CLY": "克莱德", "WAL": "威尔士", "YOR": "约克",
    "BOS": "波士顿", "NYC": "纽约", "WAS": "华盛顿",
}

MAX_DELTA = 15
MAX_SPLITS_PER_POWER = 2
SPLIT_RELEASE_BONUS = 15

_RUBRIC_WAR = """Score each power's DOMESTIC satisfaction 0-100 (previous score given; move at most 15 points per phase).
Drivers by power: GERMANY (nationalism): up on wins/conquests, down on prolonged peace and defeats.
BRITAIN: up when home centers are safe, on naval wins, when allies keep faith; down on home loss or invasion.
FRANCE: up on peace and recovery; down on war and fear of war. ITALY: up on backing the right side and Mediterranean expansion; down when abandoned or defeated.
SPAIN: up on peace and being courted; down on war and invasion. SOVIET: up on defensive wins at home and expansion; down on home loss and betrayal.
USA: up when the homeland is safe and allies beg for help; down on early war entry (isolationist backlash) and defeats.
POLAND: up on surviving and delivered allied support; down on lost centers and abandonment.
Minors (generic): up on being protected and peace; down on invasion, abandonment, and forced war.
Betrayals, broken promises, and lost home centers weigh heavily. A quiet phase with no news moves a score by at most 3 points."""

_RUBRIC_PEACE = """Score each power's DOMESTIC satisfaction 0-100 (previous score given; move at most 15 points per phase).
PEACE PERIOD (1937-1938): no war is expected. Score UP for peaceful expansion (taking unowned centers), rearmament (building units), signing treaties, and gaining puppets/protectorates. Do NOT penalize any power for not fighting, including GERMANY. Score DOWN only for losing centers, being humiliated (a rival taking a claimed province), broken promises, and isolation. A quiet phase with no news moves a score by at most 3 points."""


def default_score(power_name: str) -> int:
    return 60 if power_name in MAJOR_POWERS else 55


class SatisfactionState:
    """Owns scores, streaks, split records, symptoms, and announcements."""

    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.scores: dict[str, int] = dict(data.get("scores") or {})
        self.history: dict[str, list[int]] = {
            k: list(v) for k, v in (data.get("history") or {}).items()
        }
        self.low_streak: dict[str, int] = dict(data.get("low_streak") or {})
        self.splits_done: dict[str, int] = dict(data.get("splits_done") or {})
        self.splits: list[dict] = list(data.get("splits") or [])
        self.reserve_names: dict[str, str] = dict(data.get("reserve_names") or {})
        self.symptoms: dict[str, dict[str, str]] = {
            k: dict(v) for k, v in (data.get("symptoms") or {}).items()
        }
        self.announcements: dict[str, list[str]] = {
            k: list(v) for k, v in (data.get("announcements") or {}).items()
        }
        self.last_scored_phase: str = str(data.get("last_scored_phase") or "")
        self.last_source: str = str(data.get("last_source") or "")

    def to_dict(self) -> dict:
        return {
            "scores": self.scores,
            "history": self.history,
            "low_streak": self.low_streak,
            "splits_done": self.splits_done,
            "splits": self.splits,
            "reserve_names": self.reserve_names,
            "symptoms": self.symptoms,
            "announcements": self.announcements,
            "last_scored_phase": self.last_scored_phase,
            "last_source": self.last_source,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SatisfactionState":
        return cls(data or {})

    # -- queries ---------------------------------------------------------

    def score_of(self, power_name: str) -> int:
        return int(self.scores.get(power_name, default_score(power_name)))

    def build_modifier(self, power_name: str) -> int:
        """Winter build adjustment from the last scored phase."""
        if not satisfaction_enabled():
            return 0
        score = self.score_of(power_name)
        if score >= 80:
            return 1
        if 20 <= score < 40:
            return -1
        return 0

    def hold_only(self, power_name: str) -> bool:
        """Units Hold/Support-only next movement phase when score < 40."""
        return satisfaction_enabled() and self.score_of(power_name) < 40

    def adjust(self, power_name: str, delta: int, why: str = "") -> int:
        """Direct adjustment (betrayal, grievance, split release). Clamped."""
        new = max(0, min(100, self.score_of(power_name) + delta))
        self.scores[power_name] = new
        logger.info("[满意度] %s %s%d（%s），现 %d", power_name, "+" if delta >= 0 else "", delta, why, new)
        return new

    def latest_symptoms_text(self) -> str:
        """Symptom lines from the most recent scored phase (for 战报)."""
        if not self.symptoms:
            return ""
        phase = sorted(self.symptoms)[-1]
        lines = [f"{power}：{text}" for power, text in sorted(self.symptoms[phase].items())]
        return f"【国内动态·{phase}】\n" + "\n".join(lines) if lines else ""

    def announce(self, phase: str, text: str) -> None:
        self.announcements.setdefault(phase, []).append(text)

    # -- scoring ---------------------------------------------------------

    def score_phase(
        self,
        game: Any,
        phase: str,
        snapshot_before: dict[str, dict],
        attacks: list[dict],
        manager: Any,
        judge: Any,
        rng: random.Random,
    ) -> dict:
        """Score one resolved movement phase. Returns a report dict.

        ``snapshot_before`` maps power -> {"centers": set, "units": int}
        captured before resolution. ``attacks`` are move orders into
        foreign-owned or foreign-occupied provinces (see mechanics
        ``detect_attacks``). ``manager`` provides peace/puppet/exile state.
        """
        if not satisfaction_enabled() or not is_movement_phase(phase):
            return {"scored": [], "source": "disabled"}
        from .exile import is_normal_active

        year = phase_year(phase)
        peace = year is not None and year <= 1938
        powers = [
            name for name in sorted(game.powers)
            if is_normal_active(game, name) and not _is_exile_related(manager, name)
        ]
        if not powers:
            return {"scored": [], "source": "none"}

        facts, fallback = self._build_facts(game, phase, powers, snapshot_before, attacks, manager, peace)
        rubric = _RUBRIC_PEACE if peace else _RUBRIC_WAR
        scored, source = judge.score_all(facts, rubric, powers, fallback)
        self.last_source = source
        self.last_scored_phase = phase

        report: dict = {"scored": [], "source": source, "splits": [], "defections": []}
        new_symptoms: dict[str, str] = {}
        for power in powers:
            prev = self.score_of(power)
            raw, reason = scored.get(power, (fallback[power], "公式兜底"))
            new = max(0, min(100, max(prev - MAX_DELTA, min(prev + MAX_DELTA, int(raw)))))
            self.scores[power] = new
            self.history.setdefault(power, []).append(new)
            delta = new - prev
            report["scored"].append({"power": power, "prev": prev, "score": new, "delta": delta, "reason": reason})
            logger.info("[满意度] %s %d→%d（%+d）%s", power, prev, new, delta, f"：{reason}" if reason else "")
            symptom = _symptom_text(new, delta)
            if symptom:
                new_symptoms[power] = symptom
            # Streaks and split/defection triggers.
            if new < 20:
                self.low_streak[power] = int(self.low_streak.get(power, 0)) + 1
            else:
                self.low_streak[power] = 0
            if power in MAJOR_POWERS and (self.low_streak.get(power, 0) >= 2 or new < 10):
                split = self._try_split(game, phase, power, manager, judge, rng)
                if split:
                    report["splits"].append(split)
                    self.low_streak[power] = 0
            elif power in MINOR_POWERS and self.low_streak.get(power, 0) >= 2:
                outcome = self._resolve_minor_unrest(game, phase, power, manager, judge, rng)
                if outcome:
                    report["defections"].append(outcome)
                    self.low_streak[power] = 0
        if new_symptoms:
            self.symptoms[phase] = new_symptoms
            # Keep only the last two phases of symptoms (narrative lag is one).
            for old in sorted(self.symptoms)[:-2]:
                del self.symptoms[old]
        return report

    def _build_facts(
        self, game: Any, phase: str, powers: list[str],
        snapshot_before: dict[str, dict], attacks: list[dict],
        manager: Any, peace: bool,
    ) -> tuple[str, dict[str, int]]:
        peace_state = getattr(manager, "peace", None)
        puppets = getattr(manager, "puppets", None)
        blocks: list[str] = []
        fallback: dict[str, int] = {}
        for power in powers:
            before = snapshot_before.get(power, {"centers": set(), "units": 0})
            centers_before = set(before.get("centers") or set())
            centers_after = {str(c).upper()[:3] for c in game.powers[power].centers}
            gained = sorted(centers_after - centers_before)
            lost = sorted(centers_before - centers_after)
            units_after = len(game.powers[power].units)
            made = sorted({a["dest"] for a in attacks if a["attacker"] == power})
            suffered = sorted({a["attacker"] for a in attacks if a.get("owner") == power})
            treaties = _treaties_signed_this_phase(peace_state, power, phase)
            parade = bool(peace_state and peace_state.parades.parade_held(power, phase))
            puppet_delta = _puppet_delta(puppets, power, phase)
            grievance = _grievance_note(peace_state, power, phase)
            prev = self.score_of(power)
            blocks.append(
                f"{power}: prev={prev} period={'peace' if peace else 'war'} "
                f"centers {len(centers_before)}->{len(centers_after)} gained={gained or '-'} lost={lost or '-'} "
                f"units {before.get('units', 0)}->{units_after} attacks={made or '-'} attacked_by={suffered or '-'} "
                f"treaties_signed={treaties or '-'} puppets={puppet_delta} parade={'yes' if parade else 'no'} "
                f"grievance={grievance or '-'}"
            )
            delta = 3 * len(gained) - 4 * len(lost) + (5 if parade else 0) + 2 * len(treaties)
            if suffered and not gained:
                delta -= 3
            fallback[power] = max(0, min(100, prev + max(-MAX_DELTA, min(MAX_DELTA, delta))))
        return "\n".join(blocks), fallback

    # -- splits ----------------------------------------------------------

    def _try_split(
        self, game: Any, phase: str, power: str, manager: Any, judge: Any, rng: random.Random,
    ) -> Optional[dict]:
        if self.splits_done.get(power, 0) >= MAX_SPLITS_PER_POWER:
            logger.info("[分裂] %s 已达分裂次数上限，不再分裂", power)
            return None
        owned = {str(c).upper()[:3] for c in game.powers[power].centers}
        capital = CAPITALS.get(power)
        eligible = sorted(
            h for h in home_centers_of(game, power) if h in owned and h != capital
        )
        if not eligible:
            logger.info("[分裂] %s 无可分裂的本土省份", power)
            return None
        reserve = next(
            (r for r in RESERVE_POWERS if not game.powers[r].centers and not game.powers[r].units),
            None,
        )
        if reserve is None:
            logger.info("[分裂] 无空闲分裂名额，%s 暂不分裂", power)
            return None
        options = {prov: f"{PROVINCE_ZH.get(prov, prov)} ({prov})" for prov in eligible}
        province, _conf, source = judge.choose(
            f"{power} satisfaction collapsed; which home province secedes?",
            "split_province",
            "Which home province should break away as an independent rebel state? Pick the most plausible one.",
            options, rng,
        )
        self._execute_split(game, phase, power, province, reserve)
        record = {
            "phase": phase, "power": power, "province": province,
            "reserve": reserve, "source": source,
        }
        self.splits.append(record)
        self.splits_done[power] = int(self.splits_done.get(power, 0)) + 1
        return record

    def _execute_split(self, game: Any, phase: str, power: str, province: str, reserve: str) -> None:
        owner = game.powers[power]
        rebel = game.powers[reserve]
        # Disband the original power's units in the seceding province.
        for unit in [u for u in owner.units if unit_loc(u) == province]:
            remove_unit(game, power, unit)
        # Transfer the center (mirror the engine's _transfer_center).
        if province in owner.centers:
            game.update_hash(power, loc=province, is_center=True)
            owner.centers.remove(province)
        if province not in rebel.centers:
            game.update_hash(reserve, loc=province, is_center=True)
            rebel.centers.append(province)
        # The rebels field one uprising army.
        add_unit(game, reserve, f"A {province}")
        display = f"{PROVINCE_ZH.get(province, province)}独立政权"
        self.reserve_names[reserve] = display
        self.scores[reserve] = default_score(reserve)
        self.low_streak[reserve] = 0
        # Pressure release for the original power.
        self.adjust(power, SPLIT_RELEASE_BONUS, "分裂减压")
        text = f"【分裂】{power} 国内动荡，{display}（{province}）宣布独立，由 {reserve} 执掌！"
        self.announce(phase, text)
        logger.info("[分裂] %s", text)

    def _resolve_minor_unrest(
        self, game: Any, phase: str, power: str, manager: Any, judge: Any, rng: random.Random,
    ) -> Optional[dict]:
        """Minor <20 for 2 phases: judge picks defection or reconciliation."""
        puppets = getattr(manager, "puppets", None)
        if puppets is None:
            return None
        overlord = puppets.overlord_of(power)
        options: dict[str, str] = {}
        if overlord:
            options["break_free"] = f"Break free from {overlord} and restore full independence."
            for major in MAJOR_POWERS:
                if major != overlord and puppets.can_accept(major):
                    options[f"switch_{major}"] = f"Switch allegiance to {major} as its puppet."
        else:
            for major in MAJOR_POWERS:
                if puppets.can_accept(major):
                    options[f"defect_{major}"] = f"Seek protection from {major} as its puppet."
        options["stay"] = "Reconcile: unrest subsides, satisfaction recovers to 40."
        choice, _conf, source = judge.choose(
            f"{power} is a minor power whose satisfaction collapsed.",
            "minor_unrest",
            "What happens to this restless minor power?",
            options, rng,
        )
        if choice == "stay":
            self.scores[power] = 40
            text = f"【国内动态】{power} 局势缓和，民众情绪回稳。"
        elif choice == "break_free":
            puppets.dissolve(game, power, phase, "民众压力下脱离宗主国")
            self.scores[power] = 40
            text = f"【脱离】{power} 在民众压力下脱离 {overlord}，恢复独立！"
        elif choice.startswith("switch_"):
            new_overlord = choice[len("switch_"):]
            puppets.dissolve(game, power, phase, "改投他国")
            puppets.form(game, power, new_overlord, phase)
            self.scores[power] = 45
            text = f"【改投】{power} 脱离 {overlord}，改投 {new_overlord} 寻求保护！"
        elif choice.startswith("defect_"):
            new_overlord = choice[len("defect_"):]
            puppets.form(game, power, new_overlord, phase)
            self.scores[power] = 45
            text = f"【投靠】{power} 国内动荡，投靠 {new_overlord} 寻求保护！"
        else:
            return None
        self.announce(phase, text)
        logger.info("[小国动荡] %s", text)
        return {"phase": phase, "power": power, "outcome": choice, "source": source}


def _is_exile_related(manager: Any, power_name: str) -> bool:
    exile = getattr(manager, "exile", None)
    if exile is None:
        return False
    return bool(exile.is_exiled(power_name) or exile.is_seeking(power_name))


def _treaties_signed_this_phase(peace_state: Any, power: str, phase: str) -> list[str]:
    if peace_state is None:
        return []
    return [
        (t["b"] if t["a"] == power else t["a"])
        for t in peace_state.treaties.treaties
        if t.get("signed_phase") == phase and power in (t.get("a"), t.get("b"))
    ]


def _puppet_delta(puppets: Any, power: str, phase: str) -> str:
    if puppets is None:
        return "0"
    gained = [m for m, e in puppets.puppets.items() if e.get("overlord") == power and e.get("since") == phase]
    lost = [m for m in puppets.dissolved_this_phase(phase) if puppets.dissolved_overlord(m, phase) == power]
    parts = []
    if gained:
        parts.append("+" + ",".join(gained))
    if lost:
        parts.append("-" + ",".join(lost))
    return " ".join(parts) if parts else "0"


def _grievance_note(peace_state: Any, power: str, phase: str) -> str:
    if peace_state is None:
        return ""
    notes = []
    for g in peace_state.grievances.grievances:
        if g.get("created_phase") == phase and power in (g.get("claimant"), g.get("intruder")):
            notes.append(f"{g.get('kind')}:{g.get('intruder')}")
    return ",".join(notes)


def _symptom_text(score: int, delta: int) -> Optional[str]:
    if score >= 80:
        return "民众为胜利欢呼，士气高昂。"
    if score < 20:
        return "各地爆发暴乱，地方势力蠢蠢欲动。"
    if 20 <= score < 40:
        return "国内出现反战游行，民众要求改变现状。"
    if delta <= -10:
        return "国内出现不满情绪，报纸开始质疑政府。"
    if delta >= 10:
        return "国内气氛积极，人们对前景乐观。"
    if abs(delta) >= 5 and score < 60:
        return "民生艰难，街头巷尾怨声渐起。"
    return None
