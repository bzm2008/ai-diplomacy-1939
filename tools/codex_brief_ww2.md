# 任务：给 1939 外交对局引擎加两条二战机制

工作目录：`~/ai-diplomacy-1939`（AI_Diplomacy 的改版，地图 `diplomacy/maps/europe1939.map`，八国 BRITAIN FRANCE GERMANY ITALY POLAND SOVIET SPAIN USA）。
用 `.venv/bin/python` 运行，需要 `PYTHONPATH=.`。仓库有大量未提交改动，不要 git checkout / reset / stash，不要改与本任务无关的代码。

## 机制一：美国孤立主义

规则：在年份 < `US_ISOLATION_UNTIL`（默认 1941，可用环境变量 `DIP_US_ISOLATION_UNTIL` 覆盖）期间，USA 的单位不能对“欧洲国家的地盘”采取进攻行动。具体：

- 一条 USA 指令如果是移动（`A X - Y`、`F X - Y`，含 `VIA`）或支援移动（`... S A X - Y`），并且目标省份 Y 满足以下任一条件，就禁止：
  1. Y 当前有非 USA 国家的单位；
  2. Y 是补给中心，且当前归属某个非 USA 国家。
- 目标省份比较时去掉海岸后缀（`SPA/SC` 视为 `SPA`），大小写统一。
- 护航（`F X C A Y - Z`）同样按 Z 判断。支援驻守（`S A X`、`S F X`，没有 `-`）、驻守 `H`、撤退阶段和调整阶段（建造/解散）不受限。
- 提前解除：如果任意一个 USA 的本土补给中心（BOS NYC WAS）当前被别国占有，或者 USA 本土中心里有别国单位，孤立立即解除（相当于被打到家门口）。
- 年份从阶段名取：`S1940M` → 1940。

实现要求：

1. 在 `ai_diplomacy/utils.py` 新增函数 `us_isolation_active(game) -> bool` 和 `filter_isolation_orders(game, power_name, orders: list[str]) -> tuple[list[str], list[str]]`（返回允许的、被禁止的）。power_name 可能是 `USA` 也可能是代码形式，非 USA 直接全部放行。
2. `gather_possible_orders` 对 USA 在孤立期内过滤掉被禁止的可选指令（每个位置的列表都过滤，不要让列表变空：驻守 `H` 一定保留）。这样提示词里美国看到的可选指令就已经不含进攻欧洲的选项。
3. `get_valid_orders` 在引擎校验之后，对 USA 再过一遍 `filter_isolation_orders`：被禁止的指令移进 `invalid`，并在日志（logger.info）里写明 `[USA] 孤立主义禁止：<指令>`。被禁止的单位不需要补指令（引擎默认驻守）。
4. 不要改引擎 `diplomacy/` 目录下的代码。

## 机制二：每年世界新闻

- 新闻文本在 `~/ai-diplomacy-1939-tools/world_news.json`，键是年份字符串，`_说明` 键忽略。
- 修改 `~/ai-diplomacy-1939-tools/make_prompts.py`：生成提示词时把这个 json 复制到 `prompts_1939/world_news.json`（放在生成流程末尾，复制前检查文件存在）。
- 修改 `ai_diplomacy/prompt_constructor.py` 的 `build_context_prompt`：从 prompts_dir（为空时用默认目录，参考 `load_prompt` 如何找目录）读 `world_news.json`，取当前阶段年份对应的文本；如果有，就在生成的 context 字符串**最前面**加上：

```
# 世界新闻（{year}）
{文本}
```

  后面空一行。文件不存在或该年份没有文本时什么都不加，不报错。
- 美国孤立期内，再在 USA 的 context 里（紧跟世界新闻之后）加一段：

```
# 美国孤立主义
在 {US_ISOLATION_UNTIL} 年之前，你的部队不能进攻欧洲国家的单位或补给中心，只能移动到空的海域、空地和中立补给中心（如 ICE、AZO），可以驻守、支援驻守，也可以自由谈判。可选指令列表已按此过滤。若有国家打到你的本土，限制立即解除。
```

## 自测（必须做，把结果写进最后的汇报）

写一个脚本 `~/ai-diplomacy-1939-tools/test_ww2_rules.py`，不调用任何大模型，只用引擎：

1. 新开 `Game(map_name="europe1939")`，打印 USA 在 S1939M 过滤前后的可选指令数量。
2. 构造一个美国舰队贴近欧洲的局面：用随机合法指令推进（固定种子），每个阶段对 USA 调 `filter_isolation_orders`，统计 1939、1940 年被禁止的指令数（应 > 0 或至少可选列表被过滤），1941 年应为 0。
3. 断言：1941 年之前，USA 从 `gather_possible_orders` 拿到的指令里没有一条的目标是“有外国单位或外国补给中心”的省份。
4. 用 `build_context_prompt`（或更简单：直接调你写的读取新闻的辅助函数）确认 S1939M 时 context 开头是 `# 世界新闻（1939）`，USA 的 context 含 `# 美国孤立主义`，1941 年 USA 不含。
5. 跑一遍 `PYTHONPATH=. .venv/bin/python ~/ai-diplomacy-1939-tools/random_game.py 1 10`，确认引擎没被弄坏。
6. `python3 -I ~/ai-diplomacy-1939-tools/make_prompts.py --cast ~/ai-diplomacy-1939-tools/cast_manual.json`，确认 `prompts_1939/world_news.json` 已生成。

## 汇报

用中文，列出：改了哪些文件的哪些函数（附简短 diff 摘要），自测每一步的输出数字，以及你不确定或觉得有风险的地方。不要声称没跑过的测试已经通过。
