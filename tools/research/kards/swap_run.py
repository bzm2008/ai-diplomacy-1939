import subprocess, sys
from concurrent.futures import ThreadPoolExecutor
K = "/Users/mac/ai-diplomacy-1939-tools/research/kards"
C = "/Users/mac/ai-diplomacy-1939-tools/art/cast"
CH = {"grok": f"{C}/GERMANY_grok.png", "kimi": f"{C}/FRANCE_kimi.png", "doubao": f"{C}/ITALY_doubao.png",
      "glm": f"{C}/SOVIET_glm.png", "deepseek": f"{C}/POLAND_deepseek.png", "claude": f"{C}/BRITAIN_claude.png",
      "gpt": f"{C}/USA_gpt.png", "gemini": f"{C}/SPAIN_gemini.png"}
JOBS = [("de_guderian","grok"),("fr_maginot","kimi"),("fr_saar","kimi"),("fr_vive","kimi"),("it_marina","doubao"),
        ("pl_hel","glm"),("pl_planwest","glm"),("pl_stubborn","glm"),("su_order227","deepseek"),("su_organ","deepseek"),
        ("uk_homeguard","claude"),("uk_ultra","claude"),("us_island","gpt")]
P = ("第一张图是一张卡牌，第二张图是角色立绘。把第一张卡牌插画里最显眼的主要人物，替换成第二张图里的这个动漫角色，"
     "保留她的发型发色、五官、服饰和随身物件，让她以原人物的姿势、视角、位置和大小出现在原场景里，画风与卡牌插画统一。"
     "除这个人物外，卡牌的其他部分保持不变：背景、其他物体、边框、左上角费用数字、右上角徽记、牌名文字、底部纸面都不要改动，底部不要添加任何文字。")
def run(j):
    slug, who = j
    import glob
    src = glob.glob(f"{K}/src/blank/{slug}/*.png")[0]
    r = subprocess.run(["bai","edit","--image",src,CH[who],"--size","1024x1536","--prompt",P,"-o",f"{K}/swap/{slug}"],
                       capture_output=True, text=True)
    open(f"{K}/swap/log/{slug}.log","w").write(r.stdout+r.stderr)
    return slug, r.returncode
with ThreadPoolExecutor(4) as ex:
    for s, rc in ex.map(run, JOBS): print(s, rc, flush=True)
