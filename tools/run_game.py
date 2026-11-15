# 1937 对局启动器（16 国 + 4 分裂预留位，共 20 个模型位）。密钥只从 ~/ai-diplomacy-1939/.env 读取，不写进代码。
# 用法（都在 ~/ai-diplomacy-1939 目录下，用 venv-test 的 python 运行）：
#   python tools/run_game.py test    试跑：打完 1937 全年（春、秋、冬季调整），停在 1938 春之前，谈判 1 轮
#   python tools/run_game.py full    正式局：1937 春开局，打到 1945 年，谈判 3 轮
#   python tools/run_game.py resume  续跑：从已有存档接着打
# 输出目录：~/ai-diplomacy-1939/results/1937-test 或 results/1937-full
# 路由原则（2026-11-14 用户令）：全都不走 XZZ；换渠道不换模型。Kimi 主用 Max（CN），
# 失败时自动 fallback 到快跑 kimi/kimi-k3（快跑无 kimi-k3.1-pro 渠道，2026-11-15 实测 503）。
import os
import subprocess
import sys
from pathlib import Path

REPO = Path.home() / "ai-diplomacy-1939"
ENV_FILE = REPO / ".env"
RUN_DIR = {"test": REPO / "results" / "1937-test", "full": REPO / "results" / "1937-full"}

# 顺序固定：game_logic.py 按 sorted(ALL_POWERS) 的字母序把 --models 依次分给各国。
# 20 国字母序：BELGIUM, BRITAIN, DENMARK, FRANCE, GERMANY, GREECE, HOLLAND, ITALY,
# NORWAY, POLAND, PORTUGAL, ROMANIA, SOVIET, SPAIN, SPLIT_A, SPLIT_B, SPLIT_C, SPLIT_D, TURKEY, USA
# 8 小国 + 4 分裂预留位统一用 GPT 6 Luna（快跑 gpt-6-luna，2026-11-15 实测可用，JSON 模式亦可）。
MODELS = [
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # BELGIUM
    ("openai", "claude-opus-5-5", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # BRITAIN
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # DENMARK
    ("openai", "kimi-k3.1-pro", "CN_BASE", "CN_KEY"),  # FRANCE（主用 Max）
    ("openai", "grok-4.7", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # GERMANY
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # GREECE
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # HOLLAND
    ("anthropic", "doubao-seed-2.1-turbo", "ARK_BASE", "ARK_AGENT_PLAN_CN_API_KEY"),  # ITALY
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # NORWAY
    ("openai", "glm-5.3", "CN_BASE", "CN_KEY"),  # POLAND（主用 Max）
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # PORTUGAL
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # ROMANIA
    ("openai", "deepseek-v4.1-flash", "CN_BASE", "CN_KEY"),  # SOVIET
    ("openai", "gemini-4-argon", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # SPAIN
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # SPLIT_A
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # SPLIT_B
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # SPLIT_C
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # SPLIT_D
    ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # TURKEY
    ("openai", "gpt-6.1-sol", "KUAIPAO_BASE", "KUAIPAO_KEY"),  # USA
]
# Card upgrade routes are injected at launch so credentials never live in code.
# 只有美/意两张升格牌换模型；西班牙已改为集群牌（es_cluster），不再需要升格路由。
UPGRADES = [
    ("us_upgrade", "openai:gpt-6-astra", "KUAIPAO_BASE", "KUAIPAO_KEY"),
    ("it_upgrade", "anthropic:doubao-seed-2.1-pro", "ARK_BASE", "ARK_AGENT_PLAN_CN_API_KEY"),
]
# 裁判（Luna）路由：judge.py 读取 DIP_JUDGE_MODEL，格式 openai:model@base#key。
JUDGE = ("openai", "gpt-6-luna", "KUAIPAO_BASE", "KUAIPAO_KEY")
# 阶段摘要和 JSON 格式化用便宜模型（走国产网关）
NARRATIVE = "openai:deepseek-v4.1-flash@{CN_BASE}#{CN_KEY}"

MODES = {
    "test": ["--max_year", "1938", "--end_at_phase", "S1938M", "--num_negotiation_rounds", "1"],
    "full": ["--max_year", "1945", "--num_negotiation_rounds", "3"],
}


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip()
    return env


def build(mode, env):
    models = ",".join(f"{p}:{m}@{env[b]}#{env[k]}" for p, m, b, k in MODELS)
    args = [
        sys.executable, str(REPO / "lm_game.py"),
        "--run_dir", str(RUN_DIR[mode]),
        "--prompts_dir", str(REPO / "ai_diplomacy" / "prompts_1937"),
        "--simple_prompts", "false",
        "--planning_phase",
        "--generate_phase_summaries", "true",
        "--use_unformatted_prompts", "false",
        "--country_specific_prompts", "false",
        "--max_tokens", "16000",
        "--models", models,
        *MODES[mode],
    ]
    return args


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("test", "full", "resume"):
        raise SystemExit("用法：run_game.py test | full | resume")
    if mode == "resume":
        mode = "full" if RUN_DIR["full"].exists() else "test"
        if not RUN_DIR[mode].exists():
            raise SystemExit("没有可续跑的存档")
        print("续跑", RUN_DIR[mode])
    env = load_env()
    missing = [k for _, _, _, k in MODELS if not env.get(k)]
    if missing:
        raise SystemExit(f".env 里缺这些密钥：{missing}")
    os.environ.update(env)
    os.environ["DIP_MAP"] = "europe1937"
    # Grok 默认深度推理超过网关 60 秒断线，必须用 low
    os.environ["DIP_REASONING_EFFORT"] = "grok-4.7=low"
    os.environ["DIP_STRICT_MODEL_RESPONSES"] = "1"
    os.environ["DIP_MODEL_FALLBACKS"] = ";".join([
        f"kimi-k3.1-pro=kimi/kimi-k3@{env['KUAIPAO_BASE']}#{env['KUAIPAO_KEY']}",
        f"glm-5.3=glm-5.3-flash@{env['KUAIPAO_BASE']}#{env['KUAIPAO_KEY']}",
    ])
    os.environ["DIP_CARD_UPGRADE_MODELS"] = ";".join(
        f"{card_id}={spec}@{env[base]}#{env[key]}"
        for card_id, spec, base, key in UPGRADES
    )
    jp, jm, jb, jk = JUDGE
    os.environ["DIP_JUDGE_MODEL"] = f"{jp}:{jm}@{env[jb]}#{env[jk]}"
    os.environ["AI_DIPLOMACY_NARRATIVE_MODEL"] = NARRATIVE.format(**env)
    os.environ["AI_DIPLOMACY_FORMATTER_MODEL"] = NARRATIVE.format(**env)
    os.chdir(REPO)
    subprocess.run(build(mode, env), check=False)


if __name__ == "__main__":
    main()
