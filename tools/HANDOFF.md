# 《强权外交》1939 交接文档

给 DeepSeek harness 的操作说明。目标：把八个大模型的 1939 对局完整跑完，并留下足够的日志供复盘。
本文档里没有任何密钥。所有密钥都在 `~/ai-diplomacy-1939/.env`（权限 600，已被 git 忽略），启动脚本会自己读取。

## 1. 这是什么

八个大模型各扮演 1939 年春欧洲的一个国家，在《强权外交》棋盘上对局，最后做成视频。
地图是改过的 `europe1939`：英法德意苏 + 波兰、西班牙、美国（美国在大西洋对岸，开局三支舰队）。
全图 42 个补给中心，控制 22 个即获胜。不做亚洲和太平洋战场。

国家分配（已定，不要改）：

| 顺序 | 国家 | 模型 | 网关 |
|---|---|---|---|
| 1 | 英国 BRITAIN | claude-opus-5-5 | xin-zhi-zhu.com |
| 2 | 法国 FRANCE | kimi-k3.1-pro | ai.max66.xyz |
| 3 | 德国 GERMANY | grok-4.7 | xin-zhi-zhu.com |
| 4 | 意大利 ITALY | doubao-seed-2.1-lite | 火山方舟 ark.cn-beijing.volces.com（Anthropic 协议） |
| 5 | 波兰 POLAND | glm-5.3 | xin-zhi-zhu.com |
| 6 | 苏联 SOVIET | deepseek-v4.1-flash | ai.max66.xyz |
| 7 | 西班牙 SPAIN | gemini-4-argon | xin-zhi-zhu.com |
| 8 | 美国 USA | gpt-6-astra | xin-zhi-zhu.com |

顺序必须和上表一致：引擎按这个顺序把模型分配给国家。

## 2. 目录

- `~/ai-diplomacy-1939`：游戏仓库（GoodStartLabs/AI_Diplomacy 的副本，非商业许可）。用里面的 `.venv`（Python 3.13，已装好依赖）。
- `~/ai-diplomacy-1939/ai_diplomacy/prompts_1939/`：本局提示词，已生成好，含八个国家的人设。不要重新生成，除非 cast 改了。
- `~/ai-diplomacy-1939/diplomacy/maps/europe1939.map`：地图。
- `~/ai-diplomacy-1939/.env`：密钥。
- `~/ai-diplomacy-1939-tools/`：工具脚本。
  - `run_game.py`：启动器（试跑 / 正式 / 续跑）。
  - `check_run.py`：检查一次运行的进度和错误。
  - `dump_phase.py`：导出某一阶段的全部提示词和原始回复。
  - `make_prompts.py`、`cast.json`、`cast_manual.json`：提示词生成，正常情况不用动。
- 结果输出到 `~/ai-diplomacy-1939/results/1939-test`（试跑）和 `results/1939-full`（正式局）。

## 3. 怎么跑

一律在仓库目录下用虚拟环境的 Python：

```bash
cd ~/ai-diplomacy-1939
# 试跑：打完 1939 全年（春移动、秋移动、冬季调整，以及可能的撤退阶段），停在 1940 春之前，每阶段谈判 1 轮
.venv/bin/python ~/ai-diplomacy-1939-tools/run_game.py test
# 正式局：打到 1945 年或有国家达到 22 个中心，每阶段谈判 3 轮
.venv/bin/python ~/ai-diplomacy-1939-tools/run_game.py full
# 中断后续跑：自动找到已有存档接着打（优先续正式局）
.venv/bin/python ~/ai-diplomacy-1939-tools/run_game.py resume
```

游戏用 `caffeinate -i` 包住启动命令，防止电脑休眠把进程杀掉：

```bash
caffeinate -i .venv/bin/python ~/ai-diplomacy-1939-tools/run_game.py full
```

预计耗时：试跑 20 到 60 分钟；正式局（打满到 1945）数小时到十几个小时，取决于模型响应速度。
xin-zhi-zhu 网关偶尔抽风（超时、403、502），引擎对每次调用自动重试 5 次（指数退避），一般能自己恢复。

## 4. 先跑试跑，确认通过再开正式局

重跑试跑前，确认 `results/1939-test` 不存在，否则脚本会把旧存档当成续跑。第一次试跑的结果已改名保存为 `results/1939-test-v1`，不要删。

试跑结束后运行：

```bash
python3 -I ~/ai-diplomacy-1939-tools/check_run.py ~/ai-diplomacy-1939/results/1939-test
```

通过标准（全部满足才算通过）：

1. 存档停在 `S1940M`（`end_at_phase` 是到达该阶段就停，不会打这个阶段）。
2. 八个国家在 `S1939M` 和 `F1939M` 都有 `order_generation` 类型的 `Success` 记录。
3. 错误条数可以有（重试产生的 WARNING 正常），但没有“某个国家整轮没有 orders 记录”的情况。
4. 八国中心数加总在 25 到 42 之间（开局本土 25 个，另有 17 个中立中心，春季不结算，秋季后才会增加）。

不通过就不要开正式局，把 `check_run.py` 的输出保留下来。

## 二战机制（2026-11-07 新增）

1. **美国孤立主义**：1941 年之前，美国的部队不能进攻欧洲国家的单位或补给中心，只能去空海域、空地和中立中心（ICE、AZO）。美国的可选指令已经提前过滤掉这类进攻，模型就算硬写，引擎也会作废，并在日志里记一条 `[USA] 孤立主义禁止：…`。如果有国家打进美国本土，限制立即解除。截止年份可以用环境变量 `DIP_US_ISOLATION_UNTIL` 改，默认 1941，不要随便动。
2. **每年世界新闻**：每个国家的提示词开头会插入当年的“世界新闻”，内容来自 `~/ai-diplomacy-1939-tools/world_news.json`，生成提示词时会复制到 `prompts_1939/`。新闻只写棋盘外的大事，不改规则。
3. 自测：`cd ~/ai-diplomacy-1939 && PYTHONPATH=. .venv/bin/python ~/ai-diplomacy-1939-tools/test_ww2_rules.py`，最后一行应为“全部通过”。开局前先跑一次。
4. 复盘时可以在 `general_game.log` 里搜 `孤立主义禁止`，看美国有没有试图提前参战，这本身就是看点。

## 5. 日志与复盘材料

每次运行的目录里有：

| 文件 | 内容 | 用途 |
|---|---|---|
| `lmvsgame.json` | 每阶段结束后的完整存档（棋盘、命令、谈判、日记、目标） | 续跑靠它；中断不丢进度 |
| `general_game.log` | 引擎日志，含每次失败和重试 | 查错 |
| `llm_responses.csv` | 每一次模型调用的时间、模型、国家、阶段、类型、**完整输入提示词**、**原始回复**、成功标记 | 复盘的主要材料 |
| `overview.jsonl` | 只在整局正常结束时生成：错误统计、国家到模型的映射、参数 | 判断局是否正常结束 |

`llm_responses.csv` 会很大（几百 MB 级），这是预期的，不要删。

导出某一阶段的全部材料（每个国家的提示词和回复各一个文件）：

```bash
python3 -I ~/ai-diplomacy-1939-tools/dump_phase.py ~/ai-diplomacy-1939/results/1939-full S1939M
```

输出到 `<run_dir>/review/S1939M/`。

## 6. 中断与续跑

- 直接 Ctrl-C 或进程被杀都安全：每阶段结束才写 `lmvsgame.json`，最多丢掉正在进行的那一个阶段。
- 续跑用 `run_game.py resume`。**续跑必须用和上次完全相同的模型和参数**，脚本已保证这一点，不要手动改参数后续。
- 注意：如果结果目录存在但里面没有合法存档，引擎会打印 `Could not resume game` 然后**从头开一局新的**。续跑前先确认目录里有 `lmvsgame.json`。
- 想从某个具体阶段重打（会清除该阶段及之后的结果）：给 `lm_game.py` 加 `--resume_from_phase S1940M`。不要轻易用。

## 7. 已知问题

1. **豆包额度**：火山方舟的每周额度在 2026-11-07 已用尽，返回 HTTP 429，重置时间 **2026-11-09 00:00（北京时间）**。在此之前不要开任何包含意大利的局，否则意大利每一轮都会失败并退回默认命令。
2. **Grok 网关不稳**：`grok-4.7` 在 xin-zhi-zhu 上间歇性超时 / 403 / 502。重试通常能过。如果日志里 Germany 连续多个阶段报 `API Error after 5 attempts`，停下来换个时间再 `resume`。
3. **地图两条警告无害**：加载时会报 `POWER NAME CAN BE CONFUSED WITH LOCATION ALIAS: USA` 和 `DUPLICATE MAP LOCATION OR POWER: SPAIN`。这两条原版地图就有，已用三个随机种子各跑 40 年验证过不影响对局，忽略即可。
4. **胜负判定日志文字是旧的**：日志里“a power has ≥18 centres”这句话是旧文本，实际阈值按地图走，是 22，不影响判定。
5. **不要开 `--use_unformatted_prompts`**：那个模式每轮额外调用一次格式化模型，费 token 也更易错。提示词本身已要求 JSON 输出。
6. **某国某轮命令解析失败**时，引擎自动用该位置的默认合法命令兜底（`fallback_orders`），对局不会因此中断，但该国那一轮等于在随机行动。`check_run.py` 里 `orders` 行的 `success` 不是 `success` 时就是这种情况，正式局里如果某个国家反复出现，记下来。

8. **Claude 拒答（已修复）**：原版下单提示词里写着“comprehensive reasoning”和“Reasoning:”标题，Claude Opus 5.5 会当成在索取内部思考而直接拒答（`stop_reason=refusal`，回复为空），英国每轮都退回默认命令。已在 `make_prompts.py` 里改成“简要分析 Analysis:”，并重新生成了提示词。如果以后重新生成提示词，这个修正会自动带上。日志里若再看到 `[claude-opus-5-5] LLM returned an empty or invalid response`，就是又触发了拒答，记下当时的阶段。
9. **Grok 断线（已修复）**：xin-zhi-zhu 网关约 60 秒内没有输出就断开连接，Grok 默认的深度推理处理长提示词时会超过这个时间，于是每次都报 `APIConnectionError: Connection error`，一次调用重试下来要 15 分钟。`run_game.py` 已设置 `DIP_REASONING_EFFORT=grok-4.7=low`，实测 20 到 40 秒返回。不要去掉这个设置。
10. **单次调用超时**：OpenAI 兼容客户端的超时已从默认 600 秒改为 180 秒，SDK 自带重试关掉，统一交给引擎的 5 次重试。

## 9. 本次接手与额度恢复后试跑记录（2026-11-09）

- 原有结果目录均保留：`results/1939-test-v1`、`results/1939-test`；没有覆盖有效存档。
- `1939-test-v3`：额度恢复后首次尝试。因进程环境继承 `NO_PROXY` 中的 `[::1]`，所有客户端初始化报 `Invalid port: ':1]'`，没有有效存档；目录只保留诊断日志。
- `1939-test-v4`：使用进程级代理修正后完成，退出码 0，最终阶段 `S1940M`，生成 `lmvsgame.json`、`overview.jsonl`、`check.txt`。实际存档 schema 为 `phases[-1].state`；8 国中心总数 33。
- v4 订单覆盖：S1939M 与 F1939M 均有 8 国 `order_generation` 记录。英国有 1 次非法 `A YOR - HOL` 后由引擎校验兜底；苏联两次提出非法 `F STP/SC - BAR` 后由引擎校验兜底；其余主要订单调用成功。
- v4 意大利：豆包额度已不再返回 429，但 Anthropic 兼容响应含 `ThinkingBlock`，旧解析器假设 `response.content[0].text`，导致意大利初始化、外交、订单和阶段日记失败。`clients.py` 已改为兼容单个 block / block 列表并提取 text block，但该修复是在 v4 运行期间写入，v4 进程未加载新代码；下次新进程才验证。
- v4 还暴露计划阶段旧调用签名问题：`planning.py` 向 `BaseModelClient.get_plan` 传入多余 positional 参数并重复传 `log_file_path`。已按方法签名修正，下一次新进程验证。
- `narrative.py` 的可选摘要失败降级已修正：不再访问不存在的 `ALLOW_NARATION_FAILURE` 配置字段；新版 Python 3.13 AST 与 ThinkingBlock 跳过测试通过。
- 新备用 Grok/Claude key 已保存为 `.env` 中的 `XZZ_GROK_ALT`、`XZZ_CLAUDE_ALT`，权限 600；最小探测直连和代理均为 `403 error code 1010`，尚未切换主配置。

**正式局安全门槛：** v4 证明额度恢复后豆包端点能返回响应，但解析修复尚未经过新进程验证；计划阶段修复也尚未经过新进程验证；仍有连接断开、非法订单和空响应风险。因此当前结论仍是“引擎可推进，模型可靠性不通过”，不得启动 `full`。下一步应在独立新目录再跑一次 test，确认意大利 ThinkingBlock 与 planning 签名修复后，8 国两阶段订单均成功，再考虑正式局。


正式局目录 `results/1939-full` 整体保留，不要清理。确认三件事：

1. `overview.jsonl` 存在（说明正常结束，不是中途断的）。
2. `check_run.py` 输出存一份：`python3 -I ~/ai-diplomacy-1939-tools/check_run.py results/1939-full > results/1939-full/check.txt`。
3. 把最终各国家的中心数、胜者和结束阶段记到 `results/1939-full/README.txt`。

复盘和视频由 Claude 负责，DeepSeek 不需要做。
