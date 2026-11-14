# 列出两个网关上每把 key 能用的模型名（读 ~/ai-diplomacy-1939/.env）
import json, urllib.request
from pathlib import Path

env = {}
for line in (Path.home() / "ai-diplomacy-1939/.env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k] = v.strip()

targets = [(n, env["XZZ_BASE"], env[f"XZZ_{n}"]) for n in ("GROK", "GEMINI", "GPT", "CLAUDE")]
targets.append(("CN", env["CN_BASE"], env["CN_KEY"]))
for name, base, key in targets:
    req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {key}", "User-Agent": "curl/8"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=30))
        print(f"== {name}:", sorted(m["id"] for m in d.get("data", [])))
    except Exception as e:
        print(f"== {name}: 失败", e)
