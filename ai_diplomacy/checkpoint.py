"""Mid-phase checkpoints: survive a VM restart without replaying a whole phase.

The main save (``lmvsgame.json``) is only written when a phase settles, so a
crash mid-phase used to discard the entire phase's negotiation, planning and
diaries, and a resume replayed all of it from scratch. A checkpoint is a
small sidecar file (``checkpoint.json`` next to the save) written after each
expensive generation stage of the in-progress phase:

* right after the phase-start block (event draw / winter setup / card countdown),
* after every negotiation round,
* after the planning phase,
* after the negotiation diaries + mechanics ``resolve_pending`` calls.

A resume that finds a checkpoint for the engine's current phase restores the
checkpointed agent state, mechanics/card state and the current phase's
messages + plans, then skips the stages the checkpoint already covers.

Safety properties (deliberate):

* The checkpoint NEVER touches the main save. The main save stays the single
  source of truth for settled phases; a checkpoint is only ever an overlay
  on top of a normal resume from the main save.
* Writes are atomic (temp file + rename) and best-effort: a failed write
  only logs a warning and the game continues without a checkpoint.
* A missing, corrupt, version-mismatched or wrong-phase checkpoint is
  ignored, and the phase replays from scratch exactly as before.
* The checkpoint is deleted as soon as its phase settles.
* ``DIP_CHECKPOINT=0`` disables the whole mechanism (no writes, no restore).

What is NOT checkpointed: order generation. It is the cheapest stage (one
call per power) and has the most side effects (card validation + model
swaps, puppet directives, order filters), so a crash there simply re-runs
order generation.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CHECKPOINT_FILENAME = "checkpoint.json"
CHECKPOINT_VERSION = 1


def checkpoint_enabled() -> bool:
    """Kill switch: DIP_CHECKPOINT=0/false/no/off disables checkpoints."""
    return os.environ.get("DIP_CHECKPOINT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def checkpoint_path(run_dir: str) -> str:
    return os.path.join(run_dir, CHECKPOINT_FILENAME)


def _current_phase_messages(game_history, short_phase: str) -> List[Dict[str, str]]:
    phase = game_history._get_phase(short_phase)
    if phase is None:
        return []
    return [
        {"sender": m.sender, "recipient": m.recipient, "message": m.content}
        for m in phase.messages
    ]


def _current_phase_plans(game_history, short_phase: str) -> Dict[str, str]:
    phase = game_history._get_phase(short_phase)
    if phase is None:
        return {}
    return dict(phase.plans)


async def save_checkpoint(
    run_dir: str,
    game,
    agents: Dict[str, Any],
    game_history,
    progress: Dict[str, Any],
) -> bool:
    """Write a mid-phase checkpoint. Best-effort: never raises.

    ``progress`` describes how far the current phase got, e.g.
    ``{"rounds_done": 2, "planning_done": False, "diaries_done": False,
    "round_message_counts": [18, 21]}``. It is stored verbatim.
    """
    if not checkpoint_enabled():
        return False
    try:
        from .cards import get_card_manager
        from .exile import is_active
        from .game_logic import serialize_agent
        from .mechanics import get_mechanics_manager
        from .utils import atomic_write_json_async

        # Same active-power filter save_game_state uses for state_agents, so
        # a checkpoint restore carries exactly the agent set a normal resume
        # would (reserves stay out until a split activates them).
        payload = {
            "version": CHECKPOINT_VERSION,
            "phase": game.get_current_phase(),
            "short_phase": game.current_short_phase,
            "progress": dict(progress),
            "agents": {
                power_name: serialize_agent(agent)
                for power_name, agent in agents.items()
                if is_active(game, power_name)
            },
            "messages": _current_phase_messages(game_history, game.current_short_phase),
            "plans": _current_phase_plans(game_history, game.current_short_phase),
            "mechanics_state": get_mechanics_manager(game).to_dict(),
            "card_state": get_card_manager(game).to_dict(),
        }
        await atomic_write_json_async(payload, checkpoint_path(run_dir))
        logger.info(
            "[断点] 已保存 %s 进度 %s（%d 封信）",
            payload["phase"],
            progress,
            len(payload["messages"]),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - checkpoint must never kill the game
        logger.warning("[断点] 保存失败，继续跑（无断点保护）：%s", exc)
        return False


def load_checkpoint(run_dir: str) -> Optional[Dict[str, Any]]:
    """Read + validate a checkpoint. Returns None when unusable. Never raises."""
    if not checkpoint_enabled():
        return None
    try:
        with open(checkpoint_path(run_dir), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != CHECKPOINT_VERSION:
        return None
    if not isinstance(payload.get("phase"), str):
        return None
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        return None
    if not isinstance(progress.get("rounds_done", 0), int):
        return None
    if not isinstance(payload.get("agents"), dict) or not payload["agents"]:
        return None
    if not isinstance(payload.get("messages"), list):
        return None
    if not isinstance(payload.get("mechanics_state"), dict):
        return None
    if not isinstance(payload.get("card_state"), dict):
        return None
    return payload


def clear_checkpoint(run_dir: str) -> None:
    """Remove the checkpoint file. Best-effort, never raises."""
    try:
        os.remove(checkpoint_path(run_dir))
    except OSError:
        pass


def restore_checkpoint(
    game,
    agents: Dict[str, Any],
    game_history,
    payload: Dict[str, Any],
    run_config,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Overlay checkpoint state onto a freshly loaded game.

    ``game``/``agents``/``game_history`` must already have been restored from
    the main save via :func:`load_game_state`. This replaces the agents and
    the mechanics/card managers with the checkpointed ones, and injects the
    current phase's messages + plans into both the engine and the history.

    Returns ``(new_agents, progress)``. Raises :class:`ValueError` when the
    checkpoint does not belong to the engine's current phase; the caller must
    fall back to a full replay in that case.
    """
    from diplomacy.engine.message import Message

    from .agent import ALL_POWERS
    from .cards import CardManager
    from .game_logic import deserialize_agent
    from .mechanics import MechanicsManager

    current_phase = game.get_current_phase()
    if payload["phase"] != current_phase:
        raise ValueError(
            f"checkpoint phase {payload['phase']!r} != current phase {current_phase!r}"
        )
    short_phase = game.current_short_phase

    # --- Agents: same model-override / prompts-dir rules as load_game_state --
    powers_order = sorted(list(ALL_POWERS))
    default_max_tokens = (
        run_config.max_tokens
        if run_config is not None and hasattr(run_config, "max_tokens")
        else 16000
    )
    model_max_tokens = {p: default_max_tokens for p in powers_order}
    if (
        run_config is not None
        and hasattr(run_config, "max_tokens_per_model")
        and run_config.max_tokens_per_model
    ):
        per_model_values = [
            s.strip() for s in run_config.max_tokens_per_model.split(",")
        ]
        if len(per_model_values) == len(powers_order):
            for power, token_val_str in zip(powers_order, per_model_values):
                model_max_tokens[power] = int(token_val_str)

    power_model_map: Dict[str, str] = {}
    if run_config is not None and getattr(run_config, "models", None):
        provided = [m.strip() for m in run_config.models.split(",")]
        if len(provided) == len(powers_order):
            power_model_map = dict(zip(powers_order, provided))
        elif len(provided) == 1:
            power_model_map = dict(zip(powers_order, provided * len(powers_order)))

    new_agents: Dict[str, Any] = {}
    for power_name, agent_data in payload["agents"].items():
        prompts_dir_from_config = (
            run_config.prompts_dir_map.get(power_name, run_config.prompts_dir)
            if getattr(run_config, "prompts_dir_map", None)
            else run_config.prompts_dir
        )
        new_agents[power_name] = deserialize_agent(
            agent_data,
            prompts_dir=prompts_dir_from_config,
            override_model_id=power_model_map.get(power_name),
            override_max_tokens=model_max_tokens.get(power_name),
        )
    # Defensive: keep any loaded agent the checkpoint did not carry.
    for power_name, agent in agents.items():
        new_agents.setdefault(power_name, agent)

    # --- Mechanics + card managers (post-draw / post-resolve state) ----------
    game.mechanics_manager = MechanicsManager.from_dict(payload["mechanics_state"])
    game.card_manager = CardManager.from_dict(payload["card_state"])

    # --- Current phase messages + plans (engine AND history) ------------------
    game_history.add_phase(short_phase)
    for msg in payload["messages"]:
        game.add_message(
            Message(
                phase=short_phase,
                sender=msg["sender"],
                recipient=msg["recipient"],
                message=msg["message"],
                time_sent=None,
            )
        )
        game_history.add_message(
            short_phase, msg["sender"], msg["recipient"], msg["message"]
        )
    for power_name, plan in payload.get("plans", {}).items():
        game_history.add_plan(short_phase, power_name, plan)

    progress = dict(payload["progress"])
    progress["phase"] = payload["phase"]
    logger.info(
        "[断点] 已恢复 %s：%d 封信、%d 份计划、进度 %s",
        payload["phase"],
        len(payload["messages"]),
        len(payload.get("plans", {})),
        payload["progress"],
    )
    return new_agents, progress
