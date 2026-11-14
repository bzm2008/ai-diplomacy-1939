# 把 AI_Diplomacy 里写死的一战七国换成 1939 年八国（地图见 diplomacy/maps/europe1939.map）
# 在原版仓库上运行（先 git checkout 掉旧改动）；可重复运行
# 可重复运行：每处替换都先检查原文还在不在
import re
from pathlib import Path

ROOT = Path.home() / "ai-diplomacy-1939"
OLD7 = ["AUSTRIA", "ENGLAND", "FRANCE", "GERMANY", "ITALY", "RUSSIA", "TURKEY"]
NEW7 = ["BRITAIN", "FRANCE", "GERMANY", "ITALY", "POLAND", "SOVIET", "SPAIN", "USA"]  # 按字母序，和 sorted(ALL_POWERS) 一致
CODES = {"BRITAIN": "GBR", "FRANCE": "FRA", "GERMANY": "GER", "ITALY": "ITA", "POLAND": "POL", "SOVIET": "SOV", "SPAIN": "ESP", "USA": "USA"}
PRETTY = {"BRITAIN": "Britain", "FRANCE": "France", "GERMANY": "Germany", "ITALY": "Italy",
          "POLAND": "Poland", "SOVIET": "Soviet Union", "SPAIN": "Spain", "USA": "United States"}


def edit(rel, pairs):
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    for old, new in pairs:
        if new in s:  # 已经改过（新文本可能包含原文，先查新文本）
            continue
        if old in s:
            s = s.replace(old, new)
        else:
            raise SystemExit(f"{rel}: 找不到要替换的原文：{old[:60]!r}")
    p.write_text(s, encoding="utf-8")
    print("ok", rel)


def py_list(xs):
    return ", ".join(f'"{x}"' for x in xs)


pretty_chain = "".join(f'.replace("{k}", "{v}")' for k, v in PRETTY.items())
aliases = {"UK": "BRITAIN", "BRIT": "BRITAIN", "ENGLAND": "BRITAIN", "England": "BRITAIN", "Britain": "BRITAIN",
           "GERMAN": "GERMANY", "EGMANY": "GERMANY", "Germany": "GERMANY", "France": "FRANCE", "Italy": "ITALY",
           "USSR": "SOVIET", "SOVIET UNION": "SOVIET", "SOVIET_UNION": "SOVIET", "RUSSIA": "SOVIET", "Russia": "SOVIET",
           "Soviet Union": "SOVIET", "Soviet": "SOVIET", "Poland": "POLAND", "Spain": "SPAIN",
           "US": "USA", "AMERICA": "USA", "UNITED STATES": "USA", "UNITED_STATES": "USA", "United States": "USA", "America": "USA"}
alias_src = "\n".join(f'    "{k}": "{v}",' for k, v in aliases.items())

# --- agent.py：全部国家 ---
edit("ai_diplomacy/agent.py", [(
    'ALL_POWERS = frozenset({"AUSTRIA", "ENGLAND", "FRANCE", "GERMANY", "ITALY", "RUSSIA", "TURKEY"})',
    f"ALL_POWERS = frozenset({{{py_list(NEW7)}}})")])

# --- models.py：别名、顺序、枚举 ---
m = (ROOT / "models.py").read_text(encoding="utf-8")
m = re.sub(r"_POWER_ALIASES = \{.*?\n\}", "_POWER_ALIASES = {\n" + alias_src + "\n}", m, count=1, flags=re.S)
m = re.sub(r"POWERS_ORDER: List\[str\] = \[.*?\]", f"POWERS_ORDER: List[str] = [{py_list(NEW7)}]", m, count=1, flags=re.S)
m = re.sub(r"(class PowerEnum\(str, Enum\):\n)(?:    [A-Z]+ = \"[A-Z]+\"\n)+",
           lambda mo: mo.group(1) + "".join(f'    {x} = "{x}"\n' for x in NEW7), m, count=1)
(ROOT / "models.py").write_text(m, encoding="utf-8")
print("ok models.py")

# --- prompt_constructor.py：本土补给中心 + 给模型看的国名 ---
pc = (ROOT / "ai_diplomacy/prompt_constructor.py").read_text(encoding="utf-8")
pc = re.sub(r"HOME_CENTERS: dict\[str, list\[str\]\] = \{.*?\n\}", '''HOME_CENTERS: dict[str, list[str]] = {
    "BRITAIN": ["Edinburgh", "Liverpool", "London"],
    "FRANCE": ["Brest", "Marseilles", "Paris"],
    "GERMANY": ["Berlin", "Kiel", "Munich", "Vienna"],
    "ITALY": ["Naples", "Rome", "Venice"],
    "POLAND": ["Warsaw", "Galicia", "Prussia"],
    "SOVIET": ["Moscow", "Saint Petersburg", "Sevastopol", "Ukraine"],
    "SPAIN": ["Spain", "North Africa"],
    "USA": ["New York", "Boston", "Washington"],
}''', pc, count=1, flags=re.S)
pc = re.sub(r'final_prompt\.replace\("AUSTRIA", "Austria"\)(?:\s*\.replace\("[A-Z]+", "[A-Za-z]+"\))+',
            "final_prompt" + pretty_chain, pc, count=1)
(ROOT / "ai_diplomacy/prompt_constructor.py").write_text(pc, encoding="utf-8")
print("ok prompt_constructor.py")

# --- clients.py：同上的国名替换 ---
cl = (ROOT / "ai_diplomacy/clients.py").read_text(encoding="utf-8")
cl = re.sub(r'final_prompt\.replace\("AUSTRIA", "Austria"\)(?:\s*\.replace\("[A-Z]+", "[A-Za-z]+"\))+',
            "final_prompt" + pretty_chain, cl, count=1)
(ROOT / "ai_diplomacy/clients.py").write_text(cl, encoding="utf-8")
print("ok clients.py")

# --- game_history.py：国名 → 三字码 ---
codes_src = "{" + ", ".join(f'"{k}": "{v}"' for k, v in CODES.items()) + "}"
gh = (ROOT / "ai_diplomacy/game_history.py").read_text(encoding="utf-8")
gh = re.sub(r'eng2code = \{\s*"AUSTRIA".*?\}', "eng2code = " + codes_src, gh, flags=re.S)
(ROOT / "ai_diplomacy/game_history.py").write_text(gh, encoding="utf-8")
print("ok game_history.py")

# --- utils.py：三字码 → 国名、收件人别名、默认模型表 ---
ut = (ROOT / "ai_diplomacy/utils.py").read_text(encoding="utf-8")
ut = re.sub(r'CODE_TO_ENGINE = \{\s*"AUT".*?\}',
            "CODE_TO_ENGINE = {" + ", ".join(f'"{v}": "{k}"' for k, v in CODES.items()) + "}", ut, count=1, flags=re.S)
ut = re.sub(r'name_mapping = \{\s*"EGMANY".*?\n    \}', "name_mapping = {\n" + "\n".join(
    "    " + line for line in alias_src.splitlines()) + "\n" + "\n".join(f'        "{x}": "{x}",' for x in NEW7) + "\n    }", ut, count=1, flags=re.S)
ut = re.sub(r'(# POWER MODELS\s*\n\s*return )\{\n.*?\n    \}',
            lambda mo: mo.group(1) + "{" + ", ".join(f'"{x}": "deepseek-v4.1-flash"' for x in NEW7) + "}", ut, count=1, flags=re.S)
(ROOT / "ai_diplomacy/utils.py").write_text(ut, encoding="utf-8")
print("ok utils.py")

# --- lm_game.py：用 1939 地图开局，胜利线跟地图走 ---
edit("lm_game.py", [
    ("        game = Game()\n", '        game = Game(map_name=os.environ.get("DIP_MAP", "europe1939"))\n'),
    ("def _detect_victory(game: Game, threshold: int = 18) -> bool:",
     "def _detect_victory(game: Game, threshold: int | None = None) -> bool:"),
    ('    return any(len(p.centers) >= threshold for p in game.powers.values())',
     '    threshold = threshold or game.win or 18\n    return any(len(p.centers) >= threshold for p in game.powers.values())'),
    ("AUSTRIA, ENGLAND, FRANCE, GERMANY, ITALY, RUSSIA, TURKEY", ", ".join(NEW7)),
])

# --- 国家数不再写死为 7 ---
edit("ai_diplomacy/game_logic.py", [
    ("        if len(per_model_values) == 7:\n", "        if len(per_model_values) == len(powers_order):\n"),
    ('            logger.warning("Expected 7 values for --max_tokens_per_model, using default.")',
     '            logger.warning(f"Expected {len(powers_order)} values for --max_tokens_per_model, using default.")'),
    ("provided_models * 7))", "provided_models * len(powers_order)))"),
    ('"Invalid number of models. Models list must be either exactly 1 or 7 models, comma delimited."',
     'f"Invalid number of models. Models list must be either exactly 1 or {len(powers_order)} models, comma delimited."'),
])
edit("ai_diplomacy/utils.py", [
    ("    if len(parts) not in {1, 7}:", "    if len(parts) not in {1, len(POWERS_ORDER)}:"),
    ('f"--prompts_dir expects 1 or 7 paths, got {len(parts)} "', 'f"--prompts_dir expects 1 or {len(POWERS_ORDER)} paths, got {len(parts)} "'),
])
edit("lm_game.py", [("Comma-separated list of 7 token limits", "Comma-separated list of per-power token limits")])
