import subprocess, glob
from concurrent.futures import ThreadPoolExecutor
K="/Users/mac/ai-diplomacy-1939-tools/research/kards"; C="/Users/mac/ai-diplomacy-1939-tools/art/cast"
UP=lambda who,name,cost,desc:(f"第一张图是一张卡牌模板，第二张图是角色立绘。做一张新卡牌：保持第一张的边框、左上角费用位、右上角徽记和纸面质感，插画区换成第二张里的这个角色，{desc}，画风与原卡统一。牌名改为「{name}」，只写牌名，卡牌底部不要任何效果小字，左上角费用数字改成 {cost}。")
SW=lambda: "她站在中央，左右各有一个一模一样的半透明分身，三个她同时低头书写指令，纸页和光点在空中飞舞"
J=[("us","blank/us_island","gpt","USA_gpt","升格·GPT 6 Astra",3,UP(0,"升格·GPT 6 Astra",3,"她身后升起一个更庞大的银白光影，象征升格成更强的自己")),
   ("it","blank/it_marina","doubao","ITALY_doubao","升格·Seed 2.1 Pro",3,UP(0,"升格·Seed 2.1 Pro",3,"她身后升起一个更庞大的金色光影，象征升格成更强的自己")),
   ("es","spain/es_watch.png","gemini","SPAIN_gemini","升格·更强 Gemini",3,UP(0,"升格·更强 Gemini",3,"她身后升起一个更庞大的紫色光影，象征升格成更强的自己")),
   ("fr","blank/fr_maginot","kimi","FRANCE_kimi","集群·三重分身",3,UP(0,"集群·三重分身",3,SW())),
   ("de","blank/de_wolfpack","grok","GERMANY_grok","集群·三重分身",3,UP(0,"集群·三重分身",3,SW())),
   ("pl","blank/pl_hel","glm","SOVIET_glm","集群·三重分身",3,UP(0,"集群·三重分身",3,SW())),
   ("su","blank/su_organ","deepseek","POLAND_deepseek","集群·三重分身",3,UP(0,"集群·三重分身",3,SW()))]
def run(j):
    k,tp,_,ch,_,_,p=j
    src=tp if tp.endswith(".png") else glob.glob(f"{K}/src/{tp}/*.png")[0]
    if tp.endswith(".png"): src=f"{K}/src/{tp}"
    r=subprocess.run(["bai","edit","--image",src,f"{C}/{ch}.png","--size","1024x1536","--prompt",p,"-o",f"{K}/model/{k}_model"],capture_output=True,text=True)
    open(f"{K}/model/log/{k}.log","w").write(r.stdout+r.stderr); return k,r.returncode
with ThreadPoolExecutor(4) as ex:
    for k,rc in ex.map(run,J): print(k,rc,flush=True)
