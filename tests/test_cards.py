"""Model-free tests for the Europe 1939 card rules."""

import asyncio
import json
import logging

from diplomacy import Game
from ai_diplomacy.cards import (
    CARD_DEFINITIONS,
    CardManager,
    card_context_for_power,
    prepare_special_orders,
)
from ai_diplomacy.clients import BaseModelClient


def test_card_catalog_has_three_cards_per_power():
    assert len(CARD_DEFINITIONS) == 24
    assert all(sum(card.power == power for card in CARD_DEFINITIONS.values()) == 3
               for power in CardManager.POWERS)


def test_card_is_one_time_and_round_trips_in_save_state():
    manager = CardManager()
    assert manager.validate_play("BRITAIN", {"card": "uk_guard", "reason": "守住"}, "S1939M")[0]
    restored = CardManager.from_dict(json.loads(json.dumps(manager.to_dict())))
    assert restored.validate_play("BRITAIN", {"card": "uk_guard", "reason": "重复"}, "F1939M")[0] is False


def test_card_rejects_unknown_missing_reason_and_non_movement_phase():
    manager = CardManager()
    assert manager.validate_play("BRITAIN", {"card": "unknown", "reason": "x"}, "S1939M")[0] is False
    assert manager.validate_play("BRITAIN", {"card": "uk_guard"}, "S1939M")[0] is False
    assert manager.validate_play("BRITAIN", {"card": "uk_guard", "reason": "x"}, "S1939A")[0] is False


def test_delayed_protection_is_public_and_expires():
    manager = CardManager()
    manager.validate_play("BRITAIN", {"card": "uk_guard", "reason": "守住"}, "S1939M")
    assert manager.active_effects() == []
    manager.begin_phase("F1939M")
    assert "uk_guard" in manager.active_effects()
    assert "uk_guard" in card_context_for_power(manager, "FRANCE", "F1939M")
    manager.begin_phase("S1940M")
    manager.begin_phase("F1940M")
    assert "uk_guard" not in manager.active_effects()
    assert manager.status("uk_guard") == "已用完"


def test_build_modifiers_are_bounded_and_model_cards_cost_one_build():
    manager = CardManager()
    assert manager.validate_play("FRANCE", {"card": "fr_cluster", "reason": "多方案"}, "S1939M")[0]
    assert manager.build_modifiers["FRANCE"] == -1
    manager.build_modifiers["BRITAIN"] = -1
    assert manager.adjusted_build_limit("BRITAIN", 0) == 0
    manager.build_modifiers["USA"] = 2
    assert manager.adjusted_build_limit("USA", 1) == 3


def test_model_cards_run_three_candidates_and_log_choice(caplog):
    client = object.__new__(BaseModelClient)
    client.model_name = "test-model"
    responses = iter([
        '{"orders":["A PAR - BUR"]}',
        '{"orders":["A PAR - GAS"]}',
        '{"choice":2,"reason":"更安全"}',
    ])

    async def fake_generate_response(*args, **kwargs):
        return next(responses)

    client.generate_response = fake_generate_response

    async def choose():
        return await client._run_cluster_selection(
            "prompt", '{"orders":["A PAR H"]}', ["A PAR H"],
            {"PAR": ["A PAR H", "A PAR - BUR", "A PAR - GAS"]}, "FRANCE",
        )

    with caplog.at_level(logging.INFO):
        result = asyncio.run(choose())
    assert result == ["A PAR - BUR"]
    assert "集群" in caplog.text


def test_order_json_keeps_card_side_channel_separate():
    client = object.__new__(BaseModelClient)
    client.model_name = "test-model"
    response = 'PARSABLE OUTPUT: {"orders":["A LON H"],"play_card":{"card":"uk_guard","reason":"守住"}}'
    assert client._extract_moves(response, "BRITAIN") == ["A LON H"]
    assert client._extract_card_play(response, "BRITAIN")["card"] == "uk_guard"
    malformed = 'PARSABLE OUTPUT: {"orders":["A LON H"],"play_card":"bad"}'
    assert client._extract_moves(malformed, "BRITAIN") == ["A LON H"]
    assert client._extract_card_play(malformed, "BRITAIN") is None


def test_protection_bonus_applies_to_named_home_unit():
    game = Game(map_name="europe1939")
    manager = CardManager()
    game.card_manager = manager
    manager.active["uk_guard"] = {"card_id": "uk_guard", "power": "BRITAIN", "remaining": 1}
    assert manager.protection_bonus(game, "F LON") == 10
    assert manager.protection_bonus(game, "A BER") == 0


def test_order227_holds_enemy_unit_in_adjacent_province():
    game = Game(map_name="europe1939")
    manager = CardManager()
    game.card_manager = manager
    manager.active["su_order227"] = {
        "card_id": "su_order227", "power": "SOVIET", "target": "WAR", "remaining": 1,
    }
    game.powers["SOVIET"].units = ["A MOS"]
    game.powers["GERMANY"].units = ["A WAR"]
    orders = prepare_special_orders(game, {"GERMANY": ["A WAR - SIL"]})
    assert orders["GERMANY"] == ["A WAR H"]
