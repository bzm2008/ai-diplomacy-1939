from dotenv import load_dotenv
import logging
import asyncio
from typing import Dict, TYPE_CHECKING

from diplomacy.engine.message import Message, GLOBAL

from .agent import DiplomacyAgent
from .exile import is_active
from .utils import gather_possible_orders, normalize_recipient_name

if TYPE_CHECKING:
    from .game_history import GameHistory
    from diplomacy import Game

logger = logging.getLogger("negotiations")
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO)

load_dotenv()


def _is_exile_participant(game: "Game", power_name: str) -> bool:
    """Seeking/exiled powers have no units but must keep negotiating."""
    manager = getattr(game, "mechanics_manager", None)
    if manager is None:
        return False
    registry = manager.exile
    return power_name in registry.seeking or power_name in registry.exiled


def _is_feuding_pair(game: "Game", sender: str, recipient: str) -> bool:
    manager = getattr(game, "mechanics_manager", None)
    if manager is None:
        return False
    pair = manager.events.feud_pair()
    return pair is not None and {sender, recipient} == set(pair)


async def conduct_negotiations(
    game: "Game",
    agents: Dict[str, DiplomacyAgent],
    game_history: "GameHistory",
    model_error_stats: Dict[str, Dict[str, int]],
    log_file_path: str,
    max_rounds: int = 3,
):
    """
    Conducts a round-robin conversation among all non-eliminated powers.
    Each power can send up to 'max_rounds' messages, choosing between private
    and global messages each turn. Uses asyncio for concurrent message generation.

    NEW: Prevents a power from sending a private message to the same recipient
    in two consecutive rounds if that recipient has not replied yet.
    """
    logger.info("Starting negotiation phase.")

    active_powers = [p_name for p_name, p_obj in game.powers.items() if is_active(game, p_name)]
    eliminated_powers = [p_name for p_name, p_obj in game.powers.items() if not is_active(game, p_name)]

    logger.info(f"Active powers for negotiations: {active_powers}")
    if eliminated_powers:
        logger.info(f"Eliminated powers (skipped): {eliminated_powers}")
    else:
        logger.info("No eliminated powers yet.")

    # ── new tracking for consecutive private messages ───────────────
    last_sent_round: Dict[tuple[str, str], int] = {}
    awaiting_reply: Dict[tuple[str, str], bool] = {}
    # ────────────────────────────────────────────────────────────────

    # We do up to 'max_rounds' single-message turns for each power
    for round_index in range(max_rounds):
        minor_sent_counts: dict = {}
        logger.info(f"Negotiation Round {round_index + 1}/{max_rounds}")

        # Prepare tasks for asyncio.gather
        tasks = []
        power_names_for_tasks = []

        for power_name in active_powers:
            if power_name not in agents:
                logger.warning(f"Agent for {power_name} not found in negotiations. Skipping.")
                continue
            agent = agents[power_name]
            client = agent.client

            possible_orders = gather_possible_orders(game, power_name)
            if (
                not possible_orders
                and not _is_exile_participant(game, power_name)
                and not game.powers[power_name].centers
            ):
                logger.info(f"No orderable locations for {power_name}; skipping message generation.")
                continue
            board_state = game.get_state()

            # Append the coroutine to the tasks list
            tasks.append(
                client.get_conversation_reply(
                    game,
                    board_state,
                    power_name,
                    possible_orders,
                    game_history,
                    game.current_short_phase,
                    log_file_path=log_file_path,
                    active_powers=active_powers,
                    agent_goals=agent.goals,
                    agent_relationships=agent.relationships,
                    agent_private_diary_str=agent.format_private_diary_for_prompt(),
                )
            )
            power_names_for_tasks.append(power_name)
            logger.debug(f"Prepared get_conversation_reply task for {power_name}.")

        # Run tasks concurrently if any were created
        if tasks:
            logger.debug(f"Running {len(tasks)} conversation tasks concurrently...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            logger.debug("No conversation tasks to run for this round.")
            results = []

        # Process results
        for i, result in enumerate(results):
            power_name = power_names_for_tasks[i]
            agent = agents[power_name]  # Get agent again for journaling
            model_name = agent.client.model_name  # Get model name for stats

            if isinstance(result, Exception):
                logger.error(f"Error getting conversation reply for {power_name}: {result}", exc_info=result)
                if model_name in model_error_stats:
                    model_error_stats[model_name]["conversation_errors"] += 1
                else:
                    model_error_stats.setdefault(power_name, {}).setdefault("conversation_errors", 0)
                    model_error_stats[power_name]["conversation_errors"] += 1
                messages = []
            elif result is None:
                logger.warning(f"Received None instead of messages for {power_name}.")
                messages = []
                if model_name in model_error_stats:
                    model_error_stats[model_name]["conversation_errors"] += 1
                else:
                    model_error_stats.setdefault(power_name, {}).setdefault("conversation_errors", 0)
                    model_error_stats[power_name]["conversation_errors"] += 1
            else:
                messages = result
                logger.debug(f"Received {len(messages)} message(s) from {power_name}.")

            if not messages:
                logger.debug(f"No valid messages returned or error occurred for {power_name}.")
                continue

            for message in messages:
                if not isinstance(message, dict) or "content" not in message:
                    logger.warning(f"Invalid message format received from {power_name}: {message}. Skipping.")
                    continue

                # Determine recipient
                if message.get("message_type") == "private":
                    recipient = normalize_recipient_name(message.get("recipient", GLOBAL))
                    if recipient not in game.powers and recipient != GLOBAL:
                        logger.warning(f"Invalid recipient '{recipient}' in message from {power_name}. Sending globally.")
                        recipient = GLOBAL
                else:
                    recipient = GLOBAL

                # ── diplomatic incident: the feuding pair cannot write to each other ──
                if recipient != GLOBAL and _is_feuding_pair(game, power_name, recipient):
                    logger.info(
                        f"Discarding private message from {power_name} to {recipient} "
                        f"(外交风波：本阶段两国不能互相发私信)."
                    )
                    continue

                # ── puppet routing: puppets only talk to their overlord ──
                if recipient != GLOBAL and not _can_message(game, power_name, recipient):
                    logger.info(
                        f"Discarding private message from {power_name} to {recipient} "
                        f"(傀儡只能与宗主国通信)."
                    )
                    continue

                # ── minors cannot send GLOBAL broadcasts ──
                if recipient == GLOBAL and not _can_send_global(power_name):
                    logger.info(
                        f"Discarding global message from {power_name} "
                        f"(小国不能发全局广播)."
                    )
                    continue

                # ── minors: at most 3 private messages per round ──
                if recipient != GLOBAL and _is_minor_power(power_name):
                    _key = (power_name, round_index)
                    if minor_sent_counts.get(_key, 0) >= 3:
                        logger.info(
                            f"Discarding private message from {power_name} "
                            f"(小国每轮最多 3 封私信)."
                        )
                        continue

                # ── repetition guard for private messages ─────────────
                if recipient != GLOBAL:
                    pair = (power_name, recipient)
                    if awaiting_reply.get(pair, False) and last_sent_round.get(pair) == round_index - 1:
                        logger.info(
                            f"Discarding repeat private message from {power_name} to {recipient} "
                            f"(waiting for reply since last round)."
                        )
                        continue  # skip this message

                    # record outbound and set waiting flag
                    last_sent_round[pair] = round_index
                    awaiting_reply[pair] = True
                    # recipient has now been contacted; when they respond, we'll clear the flag for the reverse pair
                    awaiting_reply[(recipient, power_name)] = False
                # ─────────────────────────────────────────────────────

                if recipient != GLOBAL and _is_minor_power(power_name):
                    _key = (power_name, round_index)
                    minor_sent_counts[_key] = minor_sent_counts.get(_key, 0) + 1

                diplo_message = Message(
                    phase=game.current_short_phase,
                    sender=power_name,
                    recipient=recipient,
                    message=message.get("content", ""),
                    time_sent=None,
                )
                game.add_message(diplo_message)
                game_history.add_message(
                    game.current_short_phase,
                    power_name,
                    recipient,
                    message.get("content", ""),
                )
                journal_recipient = f"to {recipient}" if recipient != GLOBAL else "globally"
                agent.add_journal_entry(
                    f"Sent message {journal_recipient} in {game.current_short_phase}: "
                    f"{message.get('content', '')[:100]}..."
                )
                logger.info(f"[{power_name} -> {recipient}] {message.get('content', '')[:100]}...")

    logger.info("Negotiation phase complete.")
    return game_history



# ---------------------------------------------------------------------------
# 1937 systems: puppet routing + minor diplomacy limits
# ---------------------------------------------------------------------------

def _is_minor_power(power_name: str) -> bool:
    """True for minor powers and breakaway reserves (never for majors or
    powers of other maps, which are in neither list)."""
    try:
        from .mechanics import MINOR_POWERS, RESERVE_POWERS
    except Exception:
        return False
    return power_name in MINOR_POWERS or power_name in RESERVE_POWERS


def _can_send_global(power_name: str) -> bool:
    """Minors and breakaway reserves cannot send GLOBAL broadcasts."""
    return not _is_minor_power(power_name)


def _can_message(game, sender: str, recipient: str) -> bool:
    """Puppet routing rule; True when no puppet registry is attached."""
    manager = getattr(game, "mechanics_manager", None)
    if manager is None:
        return True
    try:
        return manager.puppets.can_message(sender, recipient)
    except Exception:
        return True
