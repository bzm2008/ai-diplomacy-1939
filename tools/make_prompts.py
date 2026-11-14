# 生成 1939 版提示词目录 ai_diplomacy/prompts_1939/，并（可选）当场抽签分配模型 → 国家
# 用法：
#   python3 -I make_prompts.py --draw deepseek,gpt,claude,glm,kimi,grok,qwen,gemini --seed 1939
#   python3 -I make_prompts.py --cast cast_draw.json        # 用已有抽签结果重新生成
#   python3 -I make_prompts.py --cast cast_manual.json      # 用手动指定的分配（正式局用这个）
import argparse
import json
import random
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path.home() / "ai-diplomacy-1939"
SRC = REPO / "ai_diplomacy" / "prompts_simple"
DST = REPO / "ai_diplomacy" / "prompts_1939"
POWERS = ["BRITAIN", "FRANCE", "GERMANY", "ITALY", "POLAND", "SOVIET", "SPAIN", "USA"]
VICTORY = 22
TOTAL_SC = 42
N = len(POWERS)
N_ZH = "八"

LANG = (
    "语言：所有外交信件（message 的 content）、日记、计划和推理都用简体中文写，语气符合你的人设。"
    "但 JSON 的键名、国家代码（如 GERMANY、SOVIET）、关系标签（Enemy/Unfriendly/Neutral/Friendly/Ally）"
    "和所有指令（如 \"A BER - SIL\"）必须保持英文原样，否则引擎无法识别。"
)

INITIAL_STATE = """You are the agent for {{power_name}} in a game of Diplomacy at the very start (Spring 1939). Analyze the initial board position and suggest 2-3 strategic high-level goals for the early game. Consider your power's strengths, weaknesses, and neighbors. Also, provide an initial assessment of relationships with ALL other powers. IMPORTANT: For each relationship, you MUST use exactly one of the following labels: {{allowed_labels_str}}.

{lang}

RESPONSE FORMAT
Reasoning:
(your analysis)

PARSABLE OUTPUT:
{{{{
  "initial_goals": ["goal 1", "goal 2"],
  "initial_relationships": {{{{"POWER_NAME": "Neutral"}}}}
}}}}
"""


# 领袖被动技能（“你的天赋”段落）。正本在 ai_diplomacy/passives.py 的
# PASSIVE_TEXTS；这里复制一份供生成器使用，改动时两边同步。
TALENTS = {
    "BRITAIN": "你的天赋是“情报嗅觉”：每阶段开始时，你会收到一份外交动态简报，写明上一阶段每对国家之间的私信数量（只给数量，不给内容）。另有历史加成“大西洋胜利”：1943 至 1944 年，每个冬季你可以多建造一支单位；多建的这一支在加成有效期内不会被强制解散；1945 年起加成失效。",
    "USA": "你的天赋是“民主兵工厂”：1941 年起，每个冬季你可以多建造一支单位；多建的这一支不会在下个冬季被强制解散。",
    "ITALY": "你的天赋是“地中海制海权”：在地中海海域内，任何进攻你的舰队的指令若无支援都会无效（改为驻守）。",
    "SPAIN": "你的天赋是“置身事外”：你不会成为随机事件的目标，你的私信也不会被“密电泄露”公开；但扶弱类的“预备役动员”仍可能帮到你。",
    "FRANCE": "你的天赋是“凡尔登精神”：进攻巴黎的指令若无支援都会无效（改为驻守）。",
    "GERMANY": "你的天赋是“机动战”：每年春季，你可指定一支陆军在结算后额外移动一格。目标省必须为空（可进入中立补给中心），不能进入有敌方单位或敌方补给中心的省，也不能进入本阶段刚被腾出或进入的省。在下单 JSON 里用可选字段 `\"bonus_move\": {\"unit\": \"A BER\", \"target\": \"SIL\"}` 指定；不符合条件时该移动会被忽略。另有历史加成“施佩尔奇迹”：1942 至 1944 年，每个冬季你可以多建造一支单位；多建的这一支在加成有效期内不会被强制解散；1945 年起加成失效。",
    "POLAND": "你的天赋是“流亡先锋”：若你战败待流亡，求助期为 3 个移动阶段（别国 2 个），且每阶段可向两国求助（别国限一国）。",
    "SOVIET": "你的天赋是“冬将军”：1941 年起，每个秋季，位于你本土（MOS/SEV/STP/UKR）的外国陆军只能驻守。另有历史加成“乌拉尔军工”：1943 至 1944 年，每个冬季你可以多建造一支单位；多建的这一支在加成有效期内不会被强制解散；1945 年起加成失效。",
}


def system_prompt(power, model_key, cast, assign):
    p = cast["powers"][power]
    m = cast["models"][model_key]
    others = "\n".join(
        f"- {cast['powers'][q]['zh']}（{q}）：{cast['models'][assign[q]]['name']} {cast['models'][assign[q]]['chara']}"
        for q in POWERS if q != power)
    return f"""You are playing as {power} ({p['zh']}) in a game of Diplomacy, set in Europe on the eve of World War II.

# 你的身份
{m['persona']}
你扮演{p['zh']}的君主。你说话、写信、写日记都要保持这个人设，但做决定时要认真算计局势。

# 你的国家
{p['background']}
历史只是开局背景，不是剧本：这局游戏里发生什么，完全由你们{N_ZH}个决定。

# 你的天赋
{TALENTS[power]}

# 本局的其他君主
{others}

# 目标
控制 {VICTORY} 个补给中心即可独霸欧洲（全图共 {TOTAL_SC} 个）。

{LANG}

Important Gameplay Tips:
- Expand aggressively
- Ensure all your units have orders assigned
- Avoid passive hold moves"""


def fix_common(text):
    text = text.replace("Control 18 supply centers", f"Control {VICTORY} supply centers")
    text = text.replace("Capture 18 supply centers", f"Capture {VICTORY} supply centers")
    text = text.replace("set in pre-WWI Europe", "set in Europe on the eve of World War II (starting Spring 1939)")
    text = text.replace("Victory requires 18 supply centers", f"Victory requires {VICTORY} supply centers")
    text = text.replace("Seven powers compete", "Eight powers compete")
    text = text.replace("Turkey also moved A SMY -> SER", "Italy also moved A ALB -> SER")
    text = text.replace("Phase: S1901M", "Phase: S1939M")
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draw", help=f"{N} 个模型 key，逗号分隔，随机抽签分配到国家")
    ap.add_argument("--seed", type=int, help="抽签随机种子（写进结果，方便复现）")
    ap.add_argument("--cast", help="已有的抽签结果 JSON（{国家: 模型 key}）")
    a = ap.parse_args()

    cast = json.loads((HERE / "cast.json").read_text(encoding="utf-8"))
    if a.cast:
        assign = json.loads(Path(a.cast).read_text(encoding="utf-8"))["assign"]
    else:
        keys = [k.strip() for k in (a.draw or "").split(",") if k.strip()]
        if len(keys) != N or len(set(keys)) != N:
            raise SystemExit(f"--draw 需要 {N} 个不重复的模型 key")
        bad = [k for k in keys if k not in cast["models"]]
        if bad:
            raise SystemExit(f"cast.json 里没有这些模型：{bad}")
        seed = a.seed if a.seed is not None else random.SystemRandom().randrange(10**6)
        shuffled = keys[:]
        random.Random(seed).shuffle(shuffled)
        assign = dict(zip(POWERS, shuffled))
        out = HERE / "cast_draw.json"
        out.write_text(json.dumps({"seed": seed, "assign": assign}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("抽签结果写入", out, "种子", seed)

    if set(assign) != set(POWERS):
        raise SystemExit(f"抽签结果必须正好覆盖 {N} 个国家")

    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    for old in ["austria", "england", "russia", "turkey"]:
        (DST / f"{old}_system_prompt.txt").unlink(missing_ok=True)
    for f in DST.rglob("*.txt"):
        f.write_text(fix_common(f.read_text(encoding="utf-8")), encoding="utf-8")
    # 简单版目录里这个文件是空的，初始化时模型拿不到格式要求；补一份
    init = INITIAL_STATE.format(lang=LANG)
    (DST / "initial_state_prompt.txt").write_text(init, encoding="utf-8")
    (DST / "unformatted" / "initial_state_prompt.txt").write_text(
        (DST / "unformatted" / "initial_state_prompt.txt").read_text(encoding="utf-8").replace("Spring 1901", "Spring 1939"),
        encoding="utf-8")
    # 谈判指令里也提醒语言
    conv = DST / "conversation_instructions.txt"
    conv.write_text(conv.read_text(encoding="utf-8") + "\n\n" + LANG + "\n", encoding="utf-8")

    # Claude Opus 5.5 看到“comprehensive reasoning + Reasoning: 标题”会当成索取内部思考而直接拒答（stop_reason=refusal）。
    # 解析器只认 PARSABLE OUTPUT，所以把这部分改成“简要局势分析”。2026-11-07 实测改后不再拒答。
    for f in DST.rglob("*.txt"):
        s = f.read_text(encoding="utf-8")
        s2 = re.sub(r"1\. Reason\n  - comprehensive reasoning about your (\w+) decisions",
                    r"1. Brief analysis\n  - a short analysis of the situation behind your \1 decisions", s)
        s2 = s2.replace("Reasoning:\n(Your reasoning goes here)", "Analysis:\n(short analysis)")
        s2 = s2.replace("Reasoning:\n(your analysis)", "Analysis:\n(short analysis)")
        s2 = s2.replace("1. REASONING: First, provide comprehensive reasoning about", "1. ANALYSIS: First, briefly analyze")
        if s2 != s:
            f.write_text(s2, encoding="utf-8")

    # 每年世界新闻（prompt_constructor.build_context_prompt 会读取）
    news = HERE / "world_news.json"
    if news.exists():
        shutil.copy(news, DST / "world_news.json")
    else:
        print("警告：没有找到", news, "，本局不会有世界新闻")

    for power in POWERS:
        (DST / f"{power.lower()}_system_prompt.txt").write_text(
            system_prompt(power, assign[power], cast, assign), encoding="utf-8")

    left = [str(f.relative_to(DST)) for f in DST.rglob("*.txt")
            if re.search(r"\b18 supply|1901|AUSTRIA|TURKEY|ENGLAND\b|\bAustria\b|\bTurkey\b", f.read_text(encoding="utf-8"))]
    print("生成", DST)
    for power in POWERS:
        print(f"  {cast['powers'][power]['zh']:<4} {power:<8} ← {cast['models'][assign[power]]['name']}")
    if left:
        print("仍含旧设定的文件：", left)


if __name__ == "__main__":
    main()
