# 擦掉卡面效果文字，只留牌名。用法：python3 -I erase_text.py 输入.png 输出.png [y0 y1]
# 思路：效果文字在牌名下面的纸面区，笔画比纸面暗。取该区域逐行中位数当纸色，把偏暗像素膨胀后覆盖。
import sys
from PIL import Image, ImageFilter
import numpy as np
src, dst = sys.argv[1], sys.argv[2]
y0 = int(sys.argv[3]) if len(sys.argv) > 3 else 560
y1 = int(sys.argv[4]) if len(sys.argv) > 4 else 668
im = Image.open(src).convert("RGB")
a = np.asarray(im).astype(np.float32)
H, W, _ = a.shape
x0, x1 = 14, W - 14
reg = a[y0:y1, x0:x1]
lum = reg.mean(axis=2)
paper_row = np.median(lum, axis=1, keepdims=True)
mask = (lum < paper_row - 22) | (lum > paper_row + 40)
m = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))
mask = np.asarray(m) > 0
bg = np.median(reg, axis=1, keepdims=True).repeat(reg.shape[1], axis=1)
rng = np.random.default_rng(1)
noise = rng.normal(0, 3.0, reg.shape[:2])[..., None]
fill = bg + noise
out = reg.copy()
out[mask] = fill[mask]
a[y0:y1, x0:x1] = out
Image.fromarray(a.clip(0, 255).astype(np.uint8)).save(dst)
print("擦除像素占比", round(float(mask.mean()), 3))
