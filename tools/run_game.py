# 1939 对局启动器。密钥只从 ~/ai-diplomacy-1939/.env 读取，不写进命令行。
# 用法（都在 ~/ai-diplomacy-1939 目录下，用 .venv/bin/python 运行）：
#   python3 ~/ai-diplomacy-1939-tools/run_game.py test    试跑：打完 1939 全年（春、秋、冬季调整），停在 1940 春之前，谈判 1 轮
#   python3 ~/ai-diplomacy-1939-tools/run_game.py full    正式局：打到 1945 年，谈判 3 轮
#   python3 ~/ai-diplomacy-1939-tools/run_game.py resume  续跑：从已有存档接着打
# 输出目录：~/ai-diplomacy-1939/results/1939-test 或 results/1939-full
import os
import subprocess
import sys
from pathlib import Path

REPO = Path.home() / "ai-diplomacy-1939"
ENV_FILE = REPO / ".env"
RUN_DIR = {"test": REPO / "results" / "1939-test", "full": REPO / "results" / "1939-full"}

# 顺序固定：BRITAIN, FRANCE, GERMANY, ITALY, POLAND, SOVIET, SPAIN, USA
MODELS = [
    ("openai", "claude-opus-5-5", "XZZ_BASE", "XZZ_CLAUDE"),
    ("openai", "kimi-k3.1-pro", "XZZ_BASE", "XZZ_KIMI_ALT"),
    ("openai", "grok-4.7", "XZZ_BASE", "XZZ_GROK"),
    ("anthropic", "doubao-seed-2.1-lite", "ARK_BASE", "ARK_AGENT_PLAN_CN_API_KEY"),
    ("openai", "glm-5.3", "XZZ_BASE", "XZZ_GLM_ALT"),
    ("openai", "deepseek-v4.1-flash", "CN_BASE", "CN_KEY"),
    ("openai", "gemini-4-argon", "XZZ_BASE", "XZZ_GEMINI"),
    ("openai", "gpt-6-astra", "XZZ_BASE", "XZZ_GPT"),
]
# 阶段摘要和 JSON 格式化用便宜模型（走国产网关）
NARRATIVE = "openai:deepseek-v4.1-flash@{CN_BASE}#{CN_KEY}"

MODES = {
    "test": ["--max_year", "1940", "--end_at_phase", "S1940M", "--num_negotiation_rounds", "1"],
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
        "--prompts_dir", str(REPO / "ai_diplomacy" / "prompts_1939"),
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
    os.environ["DIP_MAP"] = "europe1939"
    # Grok 默认深度推理超过网关 60 秒断线，必须用 low
    os.environ["DIP_REASONING_EFFORT"] = "grok-4.7=low"
    os.environ["DIP_STRICT_MODEL_RESPONSES"] = "1"
    os.environ["AI_DIPLOMACY_NARRATIVE_MODEL"] = NARRATIVE.format(**env)
    os.environ["AI_DIPLOMACY_FORMATTER_MODEL"] = NARRATIVE.format(**env)
    os.chdir(REPO)
    subprocess.run(build(mode, env), check=False)


if __name__ == "__main__":
    main()
