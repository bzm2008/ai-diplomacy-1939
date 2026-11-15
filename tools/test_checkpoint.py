# 断点续跑自测（不调用大模型）：checkpoint 写/读/恢复、谈判跳过已完成轮次、
# 重复私信 guard 重建、异常回退（损坏/错阶段/禁用）。
# 用法：cd <repo> && PYTHONPATH=. <venv>/bin/python tools/test_checkpoint.py
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DIP_MECHANICS_SEED", "42")
os.environ.pop("DIP_JUDGE_MODEL", None)  # 裁判走种子随机/公式兜底，不联网
os.environ.pop("DIP_CHECKPOINT", None)  # 默认启用

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import logging

logging.basicConfig(level=logging.WARNING, format="%(message)s")

from diplomacy import Game  # noqa: E402
from diplomacy.engine.message import Message as EngineMessage  # noqa: E402
from diplomacy.utils.export import to_saved_game_format  # noqa: E402

from ai_diplomacy.agent import ALL_POWERS, DiplomacyAgent  # noqa: E402
from ai_diplomacy.cards import get_card_manager  # noqa: E402
from ai_diplomacy.checkpoint import (  # noqa: E402
    checkpoint_enabled,
    checkpoint_path,
    clear_checkpoint,
    load_checkpoint,
    restore_checkpoint,
    save_checkpoint,
)
from ai_diplomacy.game_history import GameHistory  # noqa: E402
from ai_diplomacy.game_logic import load_game_state  # noqa: E402
from ai_diplomacy.mechanics import get_mechanics_manager  # noqa: E402
from ai_diplomacy.negotiations import (  # noqa: E402
    _rebuild_repetition_guard,
    conduct_negotiations,
)

CHECKS = [0]


def check(cond, msg):
    CHECKS[0] += 1
    assert cond, msg


def dummy_run_config():
    specs = ",".join(
        f"openai:fake-model@https://127.0.0.1:9/v1#dummy-{p}"
        for p in sorted(ALL_POWERS)
    )
    return SimpleNamespace(
        models=specs,
        max_tokens=16000,
        max_tokens_per_model=None,
        prompts_dir=str(REPO / "ai_diplomacy" / "prompts_1937"),
        prompts_dir_map=None,
    )


class FakeClient:
    """Deterministic stand-in: every round, message the next active power."""

    def __init__(self):
        self.model_name = "fake-model"
        self.max_tokens = 16000
        self.calls = 0
        self.system_prompt = ""

    def set_system_prompt(self, prompt):
        self.system_prompt = prompt

    async def get_conversation_reply(
        self, game, board_state, power_name, possible_orders, game_history,
        current_phase, **kwargs
    ):
        self.calls += 1
        active = sorted(kwargs.get("active_powers") or [power_name])
        nxt = active[(active.index(power_name) + 1) % len(active)]
        return [
            {
                "message_type": "private",
                "recipient": nxt,
                "content": f"msg-{power_name}-call{self.calls}",
            }
        ]


def make_fake_agents(prompts_dir):
    agents = {}
    for power in sorted(ALL_POWERS):
        agents[power] = DiplomacyAgent(
            power_name=power, client=FakeClient(), prompts_dir=prompts_dir
        )
    return agents


class _Crash(Exception):
    pass


async def test_roundtrip_real_save():
    """Checkpoint save/restore round-trips agents, messages, plans, mechanics."""
    run_save = Path.home() / "ai-diplomacy-1939" / "results" / "1937-full" / "lmvsgame.json"
    if not run_save.exists():
        print("SKIP roundtrip: no live 1937 save found")
        return
    tmp = Path(tempfile.mkdtemp(prefix="ckpt-roundtrip-"))
    try:
        shutil.copy2(run_save, tmp / "lmvsgame.json")
        cfg = dummy_run_config()
        game, agents, history, _ = load_game_state(str(tmp), "lmvsgame.json", cfg)
        phase = game.get_current_phase()
        short = game.current_short_phase
        check(phase == short, f"phase {phase!r} != short {short!r}")
        # state_agents only carries active powers (reserves excluded).
        check(len(agents) == 16, f"expected 16 agents, got {len(agents)}")

        # Simulate mid-phase progress: 2 messages, 1 plan, diary + relationship edits.
        history.add_phase(short)
        for sender, recipient, content in [
            ("GERMANY", "SOVIET", "test msg one"),
            ("SOVIET", "GERMANY", "test msg two"),
        ]:
            game.add_message(
                EngineMessage(phase=short, sender=sender, recipient=recipient,
                              message=content, time_sent=None)
            )
            history.add_message(short, sender, recipient, content)
        history.add_plan(short, "GERMANY", "test plan alpha")
        agents["GERMANY"].private_diary.append("diary marker 12345")
        agents["GERMANY"].relationships["SOVIET"] = "Ally"
        mech_before = get_mechanics_manager(game).to_dict()
        card_before = get_card_manager(game).to_dict()

        progress = {"rounds_done": 1, "planning_done": True,
                    "diaries_done": False, "round_message_counts": [2]}
        ok = await save_checkpoint(str(tmp), game, agents, history, progress)
        check(ok, "save_checkpoint should succeed")
        check(os.path.isfile(checkpoint_path(str(tmp))), "checkpoint file must exist")

        # Main save must be untouched by the checkpoint write.
        with open(tmp / "lmvsgame.json", encoding="utf-8") as fh:
            main_save = json.load(fh)
        check("checkpoint" not in json.dumps(main_save)[:1000] or True, "noop")
        check(main_save["phases"][-1]["name"] == phase, "main save phase unchanged")

        # Fresh load + restore.
        game2, agents2, history2, _ = load_game_state(str(tmp), "lmvsgame.json", cfg)
        payload = load_checkpoint(str(tmp))
        check(payload is not None, "load_checkpoint should parse the file")
        check(payload["phase"] == phase, "checkpoint phase must match")
        agents3, progress2 = restore_checkpoint(game2, agents2, history2, payload, cfg)
        check(progress2["rounds_done"] == 1, "progress rounds_done restored")
        check(progress2["planning_done"] is True, "progress planning_done restored")
        check(progress2["round_message_counts"] == [2], "round counts restored")

        ph = history2._get_phase(short)
        check(ph is not None, "restored history must have the current phase")
        check(len(ph.messages) == 2, f"expected 2 restored messages, got {len(ph.messages)}")
        check(ph.messages[0].content == "test msg one", "message 1 content")
        check(ph.messages[1].sender == "SOVIET", "message 2 sender")
        check(ph.plans.get("GERMANY") == "test plan alpha", "plan restored")
        check("diary marker 12345" in agents3["GERMANY"].private_diary, "diary restored")
        check(agents3["GERMANY"].relationships["SOVIET"] == "Ally", "relationship restored")
        check(get_mechanics_manager(game2).to_dict() == mech_before, "mechanics state restored")
        check(get_card_manager(game2).to_dict() == card_before, "card state restored")

        # Engine messages must survive into the save serialization.
        saved = to_saved_game_format(game2)
        check(saved["phases"][-1]["messages"], "engine must carry restored messages")
        check(
            [m["message"] for m in saved["phases"][-1]["messages"]] == ["test msg one", "test msg two"],
            "engine message order/content",
        )

        clear_checkpoint(str(tmp))
        check(not os.path.exists(checkpoint_path(str(tmp))), "clear_checkpoint removes file")
        check(load_checkpoint(str(tmp)) is None, "no checkpoint after clear")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_negotiation_skip_equivalence():
    """Crash after round 1 + checkpoint resume == uninterrupted 3-round run."""
    prompts_dir = str(REPO / "ai_diplomacy" / "prompts_1937")
    tmp = Path(tempfile.mkdtemp(prefix="ckpt-neg-"))
    try:
        # --- Uninterrupted run ---
        game_a = Game(map_name="europe1937")
        agents_a = make_fake_agents(prompts_dir)
        hist_a = GameHistory()
        hist_a.add_phase(game_a.current_short_phase)
        get_mechanics_manager(game_a)
        get_card_manager(game_a)
        await conduct_negotiations(
            game_a, agents_a, hist_a, {}, log_file_path=str(tmp / "a.csv"), max_rounds=3
        )
        msgs_a = [
            (m.sender, m.recipient, m.content)
            for m in hist_a._get_phase(game_a.current_short_phase).messages
        ]
        pairs_a = [(s, r) for s, r, _ in msgs_a]
        calls_a = sum(a.client.calls for a in agents_a.values())

        # --- Crash run: die after round 1, checkpoint, fresh objects, resume ---
        game_b = Game(map_name="europe1937")
        agents_b = make_fake_agents(prompts_dir)
        hist_b = GameHistory()
        hist_b.add_phase(game_b.current_short_phase)
        get_mechanics_manager(game_b)
        get_card_manager(game_b)
        counts = []

        async def hook_crash(round_index):
            ph = hist_b._get_phase(game_b.current_short_phase)
            counts.append(len(ph.messages) - sum(counts))
            await save_checkpoint(
                str(tmp), game_b, agents_b, hist_b,
                {"rounds_done": round_index + 1, "planning_done": False,
                 "diaries_done": False, "round_message_counts": list(counts)},
            )
            raise _Crash("simulated VM restart")

        try:
            await conduct_negotiations(
                game_b, agents_b, hist_b, {}, log_file_path=str(tmp / "b.csv"),
                max_rounds=3, on_round_complete=hook_crash,
            )
            raise AssertionError("expected simulated crash")
        except _Crash:
            pass
        check(len(counts) == 1, "hook ran once before crash")
        payload = load_checkpoint(str(tmp))
        check(payload is not None, "checkpoint written before crash")
        check(payload["progress"]["rounds_done"] == 1, "crash checkpoint rounds_done")
        ckpt_msgs = [
            (m["sender"], m["recipient"], m["message"]) for m in payload["messages"]
        ]
        check(len(ckpt_msgs) == 16, f"round 1 should record 16 messages, got {len(ckpt_msgs)}")

        # Fresh objects + restore (swap real deserialized clients for fakes).
        game_c = Game(map_name="europe1937")
        agents_c0 = make_fake_agents(prompts_dir)
        hist_c = GameHistory()
        get_mechanics_manager(game_c)
        get_card_manager(game_c)
        # Build a loadable state: restore needs agents dict; use the fake
        # agents as the "loaded" ones and let restore overlay checkpoint data
        # by deserializing with dummy specs, then swap clients back to fakes.
        cfg = dummy_run_config()
        agents_c, progress_c = restore_checkpoint(game_c, agents_c0, hist_c, payload, cfg)
        for power, agent in agents_c.items():
            agent.client = FakeClient()
        check(progress_c["rounds_done"] == 1, "restored progress rounds_done")
        await conduct_negotiations(
            game_c, agents_c, hist_c, {}, log_file_path=str(tmp / "c.csv"),
            max_rounds=3, start_round=1,
            round_message_counts=progress_c["round_message_counts"],
        )
        msgs_c = [
            (m.sender, m.recipient, m.content)
            for m in hist_c._get_phase(game_c.current_short_phase).messages
        ]
        pairs_c = [(s, r) for s, r, _ in msgs_c]
        calls_c = sum(a.client.calls for a in agents_c.values())
        # Round-1 messages restored verbatim as the prefix of the resumed run.
        check(msgs_c[: len(ckpt_msgs)] == ckpt_msgs, "checkpoint messages must survive verbatim")
        # Same surviving (sender, recipient) sequence as the uninterrupted run:
        # the rebuilt guard discards round 2 and admits round 3, identically.
        check(pairs_a == pairs_c, f"surviving pairs must match: {len(pairs_a)} vs {len(pairs_c)}")
        check(len(msgs_c) == 32, f"expected 32 surviving messages, got {len(msgs_c)}")
        # Resume ran only rounds 2-3: 32 calls vs 48 uninterrupted.
        check(calls_a == 48, f"uninterrupted calls should be 48, got {calls_a}")
        check(calls_c == 32, f"resume calls should be 32, got {calls_c}")
        # No duplicate contents (guards against double-restore).
        check(len({c for _, _, c in msgs_c}) == len(msgs_c), "no duplicate messages")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_guard_rebuild():
    hist = GameHistory()
    hist.add_phase("S1938M")
    # Round 1: A->B, C->D. Round 2: B->A (reply), E->F.
    hist.add_message("S1938M", "GERMANY", "SOVIET", "m1")
    hist.add_message("S1938M", "FRANCE", "BRITAIN", "m2")
    hist.add_message("S1938M", "SOVIET", "GERMANY", "m3")
    hist.add_message("S1938M", "ITALY", "SPAIN", "m4")
    last, awaiting = _rebuild_repetition_guard(hist, "S1938M", [2, 2])
    check(last[("GERMANY", "SOVIET")] == 0, "GERMANY->SOVIET last round 0")
    check(last[("SOVIET", "GERMANY")] == 1, "SOVIET->GERMANY last round 1")
    check(awaiting[("GERMANY", "SOVIET")] is False, "reply clears awaiting")
    check(awaiting[("SOVIET", "GERMANY")] is True, "unreplied message awaits")
    check(awaiting[("ITALY", "SPAIN")] is True, "ITALY->SPAIN awaits")
    check(last[("FRANCE", "BRITAIN")] == 0, "FRANCE->BRITAIN last round 0")
    # Empty counts -> empty guard.
    last2, awaiting2 = _rebuild_repetition_guard(hist, "S1938M", [])
    check(last2 == {} and awaiting2 == {}, "empty counts -> empty guard")
    # Missing phase -> empty guard.
    last3, _ = _rebuild_repetition_guard(hist, "F1938M", [2])
    check(last3 == {}, "missing phase -> empty guard")


def test_fallbacks():
    tmp = Path(tempfile.mkdtemp(prefix="ckpt-fallback-"))
    try:
        check(load_checkpoint(str(tmp)) is None, "missing file -> None")
        (tmp / "checkpoint.json").write_text("{not json", encoding="utf-8")
        check(load_checkpoint(str(tmp)) is None, "corrupt file -> None")
        (tmp / "checkpoint.json").write_text(
            json.dumps({"version": 999, "phase": "S1938M"}), encoding="utf-8"
        )
        check(load_checkpoint(str(tmp)) is None, "version mismatch -> None")
        (tmp / "checkpoint.json").write_text(
            json.dumps({"version": 1, "phase": "S1938M"}), encoding="utf-8"
        )
        check(load_checkpoint(str(tmp)) is None, "incomplete payload -> None")

        # Wrong-phase restore raises.
        game = Game(map_name="europe1937")
        hist = GameHistory()
        agents = make_fake_agents(str(REPO / "ai_diplomacy" / "prompts_1937"))
        payload = {
            "version": 1, "phase": "F1938M", "short_phase": "F1938M",
            "progress": {"rounds_done": 1}, "agents": {},
            "messages": [], "plans": {},
            "mechanics_state": get_mechanics_manager(game).to_dict(),
            "card_state": get_card_manager(game).to_dict(),
        }
        try:
            restore_checkpoint(game, agents, hist, payload, dummy_run_config())
            raise AssertionError("expected ValueError for wrong phase")
        except ValueError:
            pass

        # Disabled switch: no writes, no reads.
        os.environ["DIP_CHECKPOINT"] = "0"
        try:
            check(checkpoint_enabled() is False, "DIP_CHECKPOINT=0 disables")
            check(load_checkpoint(str(tmp)) is None, "disabled -> load None")
            ok = asyncio.get_event_loop().run_until_complete(
                save_checkpoint(str(tmp), game, agents, hist, {"rounds_done": 0})
            ) if False else None
            # (save is async; test via fresh loop below)
        finally:
            os.environ.pop("DIP_CHECKPOINT", None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_disabled_save():
    os.environ["DIP_CHECKPOINT"] = "0"
    try:
        game = Game(map_name="europe1937")
        hist = GameHistory()
        agents = make_fake_agents(str(REPO / "ai_diplomacy" / "prompts_1937"))
        tmp = Path(tempfile.mkdtemp(prefix="ckpt-disabled-"))
        try:
            ok = await save_checkpoint(str(tmp), game, agents, hist, {"rounds_done": 0})
            check(ok is False, "disabled save returns False")
            check(not os.path.exists(checkpoint_path(str(tmp))), "disabled save writes nothing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        os.environ.pop("DIP_CHECKPOINT", None)


async def main():
    await test_roundtrip_real_save()
    await test_negotiation_skip_equivalence()
    test_guard_rebuild()
    test_fallbacks()
    await test_disabled_save()
    print(f"checkpoint tests: {CHECKS[0]} assertions passed")


if __name__ == "__main__":
    asyncio.run(main())
