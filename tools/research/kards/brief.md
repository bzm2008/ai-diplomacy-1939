任务：调研卡牌游戏 Kards（The WWII Card Game，Kards 官方 kards.com 与 kards.fandom.com wiki）的真实卡牌，写入 ./kards-cards.md。

背景：我们在做 AI 玩《强权外交》1939 的视频，八个国家：英国、法国、德国、意大利、波兰、苏联、西班牙、美国。每国要配 3 张"牌"，想优先选 Kards 里真实存在的卡牌来蹭热度，并在视频里还原卡牌样式。

要求：
1. 先查 Kards 里这些国家（德、英、美、苏、法、意；波兰、西班牙若没有就写"没有"）各有哪些有名的 Order（令牌）、Countermeasure（反制）、Building（建筑）卡，每国至少 6 张，要真实存在，每张写：英文原名、类型、费用(kredits)、效果原文、所属扩展包、来源 URL。
2. 重点核实这些是否存在、是否同名：Alpine Fortress（阿尔卑斯要塞）、Maginot Line、Atlantic Wall、Lend Lease、Great Patriotic War、Blitzkrieg、Operation Barbarossa、Battle of Britain 之类。查不到的明确写"未找到"，不要编。
3. 找每张卡牌原图（卡面）的直接图片链接，记录 URL，并在能下载时用 curl 下载到 ./img/（文件名用英文卡名）。同时记录图片版权状态（Kards 卡图版权属开发商 Kards/1939 Games，注明"需确认素材许可"，不要假设可商用）。
4. 最后写一个表：卡名、国家、类型、效果摘要、有无原图、来源。
只写你亲眼从网页确认的内容，不要凭记忆补。
