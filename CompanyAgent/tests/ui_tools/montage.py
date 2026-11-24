#!/usr/bin/env python3
"""把几张截图拼成一张总览：python3 montage.py out.png 列数 缩放 a.png b.png ...（缩放如 0.5）"""
import sys
from PIL import Image, ImageDraw
out, cols, scale = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
ims = [Image.open(p).convert('RGB') for p in sys.argv[4:]]
ims = [im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS) for im in ims]
w = max(i.width for i in ims); h = max(i.height for i in ims)
rows = (len(ims) + cols - 1) // cols
pad = 8
canvas = Image.new('RGB', (cols * (w + pad) + pad, rows * (h + pad + 16) + pad), (128, 128, 128))
d = ImageDraw.Draw(canvas)
for k, (im, p) in enumerate(zip(ims, sys.argv[4:])):
    x = pad + (k % cols) * (w + pad); y = pad + (k // cols) * (h + pad + 16)
    d.text((x, y), p.split('/')[-1], fill=(255, 255, 255))
    canvas.paste(im, (x, y + 14))
canvas.save(out)
print(out, canvas.size)
