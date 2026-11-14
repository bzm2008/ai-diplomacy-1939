import asyncio
import logging
from typing import Dict

from .game_history import GameHistory
from .agent import DiplomacyAgent
from .exile import is_active
from .utils import gather_possible_orders

logger = logging.getLogger(__name__)


async def planning_phase(
    game,
    agents: Dict[str, DiplomacyAgent],
    game_history: GameHistory,
    model_error_stats,
    log_file_path: str,
):
    """
    Lets each power generate a strategic plan using their DiplomacyAgent.
    """
    logger.info(f"Starting planning phase for {game.current_short_phase}...")
    active_powers = [p_name for p_name, p_obj in game.powers.items() if is_active(game, p_name)]
    eliminated_powers = [p_name for p_name, p_obj in game.powers.items() if not is_active(game, p_name)]

    logger.info(f"Active powers for planning: {active_powers}")
    if eliminated_powers:
        logger.info(f"Eliminated powers (skipped): {eliminated_powers}")
    else:
        logger.info("No eliminated powers yet.")

    board_state = game.get_state()

    # NOTE: client.get_plan is async. It must be awaited on the event loop;
    # submitting it to a ThreadPoolExecutor only creates an un-awaited
    # coroutine, whose .startswith() then raised AttributeError for every
    # power and silently discarded all plans.
    async def _run_plan(power_name: str, agent: DiplomacyAgent):
        return await agent.client.get_plan(
            game,
            board_state,
            power_name,
            game_history,
            log_file_path=log_file_path,
            agent_goals=agent.goals,
            agent_relationships=agent.relationships,
            agent_private_diary_str=agent.format_private_diary_for_prompt(),
        )

    tasks = {}
    for power_name in active_powers:
        if power_name not in agents:
            logger.warning(f"Agent for {power_name} not found in planning phase. Skipping.")
            continue
        tasks[power_name] = asyncio.create_task(_run_plan(power_name, agents[power_name]))
        logger.debug(f"Submitted get_plan task for {power_name}.")

    logger.info(f"Waiting for {len(tasks)} planning results...")
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for power_name, plan_result in zip(tasks.keys(), results):
        try:
            if isinstance(plan_result, BaseException):
                raise plan_result
            logger.info(f"Received planning result from {power_name}.")

            if plan_result.startswith("Error:"):
                logger.warning(f"Agent {power_name} reported an error during planning: {plan_result}")
                if power_name in model_error_stats:
                    model_error_stats[power_name].setdefault("planning_generation_errors", 0)
                    model_error_stats[power_name]["planning_generation_errors"] += 1
                else:
                    model_error_stats.setdefault(f"{power_name}_planning_generation_errors", 0)
                    model_error_stats[f"{power_name}_planning_generation_errors"] += 1
            elif plan_result:
                agents[power_name].add_journal_entry(
                    f"Generated plan for {game.current_short_phase}: {plan_result[:100]}..."
                )
                game_history.add_plan(game.current_short_phase, power_name, plan_result)
                logger.debug(f"Added plan for {power_name} to history.")
            else:
                logger.warning(f"Agent {power_name} returned an empty plan.")

        except Exception as e:
            logger.error(f"Exception during planning result processing for {power_name}: {e}")
            if power_name in model_error_stats:
                model_error_stats[power_name].setdefault("planning_execution_errors", 0)
                model_error_stats[power_name]["planning_execution_errors"] += 1
            else:
                model_error_stats.setdefault(f"{power_name}_planning_execution_errors", 0)
                model_error_stats[f"{power_name}_planning_execution_errors"] += 1

    logger.info("Planning phase processing complete.")
    return game_history
