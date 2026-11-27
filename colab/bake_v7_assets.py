# -*- coding: utf-8 -*-
"""
95GB Colab RTX PRO 6000 旗舰级 2D 美术工坊批量渲染脚本 (Animagine-XL 4.0 + BiRefNet GPU)
产出两套顶级 2D 直播资产，一次烘焙永久存本地零显卡复用：
1. 5 张大师级 1080x1920 宫崎骏/新海诚/蔚蓝档案风 2D 游戏专属实景背景（茶话会 / 樱花棋局 / 复古侦探推理室 / 仲夏营地白天 / 仲夏营地星光萤火虫夜景）
2. 8 位极度幼态 2 头身超软萌坐姿 2D 宝宝精绘透明立绘（参考 blue-fish-archive 软糯手绘坐姿小豆丁画风，统一画师质感 + BiRefNet GPU 发丝级透明抠图！）
"""

import gc
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/content/diffusers_compat")

import numpy as np
import torch
from diffusers import EulerAncestralDiscreteScheduler, StableDiffusionXLPipeline
from PIL import Image

OUT_DIR = Path("/content/ai_live_cloud/baked_v7")
SCENE_DIR = OUT_DIR / "scenes"
CHIBI_DIR = OUT_DIR / "chibi_v7"
SCENE_DIR.mkdir(parents=True, exist_ok=True)
CHIBI_DIR.mkdir(parents=True, exist_ok=True)

NEG_SCENE = (
    "people, person, girl, boy, character, human, chibi, crowd, text, watermark, signature, logo, "
    "lowres, bad quality, worst quality, blurry, ugly, 3d render, photorealistic, horror, dark scary, blood"
)

SCENES = {
    "scene_life": (
        "masterpiece, high score, great score, absurdres, scenery, no humans, "
        "cozy pastoral anime garden wooden cottage terrace, lush blooming sakura cherry blossom tree, "
        "sunflowers and pastel daisy flowerbed, warm golden afternoon sunlight, volumetric light shafts, "
        "floating cherry blossom petals, soft fluffy clouds in clear blue sky, gentle windmill on distant green hills, "
        "studio ghibli and blue archive background art style, warm healing cozy atmosphere, vibrant painterly anime illustration, "
        "clean open lower foreground space for wooden tea table"
    ),
    "scene_chess": (
        "masterpiece, high score, great score, absurdres, scenery, no humans, "
        "serene anime garden pavilion under giant blooming cherry blossom tree, falling pink sakura petals, "
        "warm dappled sunlight, bamboo fence, stone lantern, lush green meadow and flower bushes, distant blue sky and clouds, "
        "makoto shinkai and studio ghibli painterly anime background art, crisp vivid colors, warm joyful atmosphere"
    ),
    "scene_undercover": (
        "masterpiece, high score, great score, absurdres, scenery, no humans, "
        "cozy fairytale detective tea room interior, warm wooden bookshelves filled with colorful storybooks, "
        "soft glowing vintage desk lamp, cute corkboard with colorful notes and red strings, window overlooking sunny pastoral garden, "
        "warm amber afternoon light rays, floating dust motes, anime visual novel background illustration, cozy playful mystery atmosphere"
    ),
    "scene_werewolf_day": (
        "masterpiece, high score, great score, absurdres, scenery, no humans, "
        "sunny midsummer forest meadow campsite, cute pastel canvas glamping tent with colorful triangle bunting flags, "
        "tall pine trees, wildflowers, warm golden morning sunbeams filtering through leaves, wooden picnic clearing, "
        "laid-back camp yuru camp and studio ghibli anime background art, cheerful healing atmosphere"
    ),
    "scene_werewolf_night": (
        "masterpiece, high score, great score, absurdres, scenery, no humans, "
        "magical starry night midsummer forest meadow campsite, deep indigo sky with bright crescent moon and milky way stars, "
        "dozens of glowing warm golden fireflies floating everywhere, cozy cute glowing paper lanterns hanging on wooden posts, "
        "warm fairy lights on pastel glamping tent, whimsical healing fairytale night anime background art, no scary elements"
    ),
}

NEG_CHIBI = (
    "tall, adult, mature, long legs, realistic, 3d, nsfw, text, watermark, logo, signature, "
    "multiple girls, 2girls, extra limbs, extra hands, bad hands, blurry, dark background, complex background, scenery, border, frame"
)

CHIBI_BASE_STYLE = (
    "masterpiece, high score, great score, absurdres, 1girl, solo, chibi, super deformed, 2-head-tall tiny cute toddler-like anime baby girl, "
    "sitting happily facing viewer, looking at viewer, big sparkling expressive anime eyes, soft pastel watercolor anime illustration, "
    "fluffy soft hair, rosy cheeks, adorable tiny proportions, full body centered with generous margin, pure solid white background, simple background"
)

CHIBIS = {
    "gpt": (
        "tiny white-haired dragon baby girl with two cute small white curved dragon horns, tiny fluffy white dragon wings, "
        "fluffy white dragon tail curled beside her, pastel white lace princess dress with pale blue ribbons, "
        "hugging a tiny cute smiling fluffy sky-blue cloud plushie, violet-blue sparkling eyes, gentle proud cute smile"
    ),
    "claude": (
        "tiny warm orange-haired baby girl with a bright yellow sunflower hair ornament, soft warm amber eyes, "
        "cream and caramel peter-pan collar dress with bow, sitting neatly hugging a cute brown leather storybook with flower bookmark, "
        "gentle polite shy sweet smile, rosy blushing cheeks"
    ),
    "deepseek": (
        "tiny ocean-blue-haired baby girl with cute whale-fin side ears, ahoge hair curl, white frilly maid headband, "
        "dark navy and white frilly lolita maid dress with whale emblem apron, big cute blue whale tail behind her, "
        "hugging a round smiling pastel blue baby whale plushie, bright sparkling sapphire blue eyes, cheerful open-mouth cute smile"
    ),
    "gemini": (
        "tiny lavender-and-sky-blue gradient haired baby girl with fluffy purple-blue cat ears and fluffy cat tail, "
        "heterochromia sparkling eyes (one pink eye, one golden yellow eye), pastel starry lavender dress, "
        "hugging a glowing cute golden four-pointed star cushion, curious bright cheerful expression"
    ),
    "zhipu": (
        "tiny silky black-haired baby girl with black-and-white lace headband, neat black-and-white classic academy dress, "
        "little white socks and boots, sitting politely hugging a tiny neat dark-blue notebook with gold trim, "
        "clear dark glassy eyes, earnest cute little class-monitor expression"
    ),
    "grok": (
        "tiny golden-blonde-haired baby girl with big fluffy twin-tails and cute tiny black bat hair clips, "
        "ruby red sparkling eyes, mischievous cute smile showing one tiny cute fang, gothic black and crimson frilly dress, "
        "holding a cute oversized plush chibi black-and-red toy battle axe playfully"
    ),
    "kimi": (
        "tiny silver-lavender wavy-haired baby girl with a golden crescent moon hair clip, starry indigo musical-note dress, "
        "sparkling violet eyes, holding a small translucent glowing rainbow glass prism gently in her lap, "
        "dreamy gentle fairytale princess smile"
    ),
    "qwen": (
        "tiny indigo-purple-haired baby girl with two cute braided pigtails, small blue-and-white porcelain style chinese ribbon mini hat, "
        "pastel blue-and-white modern hanfu lolita dress, holding a cute painted blue-and-white folding paper fan and little embroidered pouch, "
        "bright gentle playful smile"
    ),
}


def load_birefnet(device: str):
    import torchvision.transforms as T
    from transformers import AutoModelForImageSegmentation

    print("[BiRefNet] loading ZhengPeng7/BiRefNet on", device, flush=True)
    model = AutoModelForImageSegmentation.from_pretrained(
        "ZhengPeng7/BiRefNet", trust_remote_code=True
    ).to(device).eval().half()
    tf = T.Compose([
        T.Resize((1024, 1024)),
        T.ToTensor(),
        T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])
    return model, tf


def mat_image(img: Image.Image, model, tf, device: str) -> Image.Image:
    orig_size = img.size
    inp = tf(img.convert("RGB")).unsqueeze(0).to(device).half()
    with torch.no_grad():
        preds = model(inp)[-1].sigmoid().float().cpu()
    mask = preds[0].squeeze().numpy()
    mask_img = Image.fromarray((mask * 255).clip(0, 255).astype(np.uint8)).resize(orig_size, Image.BILINEAR)
    rgba = img.convert("RGBA")
    rgba.putalpha(mask_img)
    # 裁剪透明边缘并留温和呼吸边距
    bbox = rgba.getbbox()
    if bbox:
        pad = 24
        x0 = max(0, bbox[0] - pad)
        y0 = max(0, bbox[1] - pad)
        x1 = min(rgba.width, bbox[2] + pad)
        y1 = min(rgba.height, bbox[3] + pad)
        rgba = rgba.crop((x0, y0, x1, y1))
    return rgba


def main():
    t0 = time.time()
    device = "cuda"
    print("[Animagine-XL 4.0] Loading pipeline to 95GB Blackwell GPU...", flush=True)
    pipe = StableDiffusionXLPipeline.from_pretrained(
        "cagliostrolab/animagine-xl-4.0",
        torch_dtype=torch.float16,
        use_safetensors=True,
    )
    pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
    pipe.to(device)
    print(f"[Animagine-XL 4.0] Loaded in {time.time() - t0:.1f}s!", flush=True)

    # 1. 渲染 5 套大师级 9:16 竖屏动漫实景背景（832x1472 -> 高质量 Lanczos 放大到 1080x1920）
    for idx, (name, prompt) in enumerate(SCENES.items()):
        out_jpg = SCENE_DIR / f"{name}.jpg"
        if out_jpg.exists() and out_jpg.stat().st_size > 40000:
            print(f"  [SKIP SCENE] {name}", flush=True)
            continue
        ts = time.time()
        gen = torch.Generator(device=device).manual_seed(202607 + idx * 101)
        img = pipe(
            prompt=prompt,
            negative_prompt=NEG_SCENE,
            width=832,
            height=1472,
            guidance_scale=5.5,
            num_inference_steps=26,
            generator=gen,
        ).images[0]
        img = img.resize((1080, 1920), Image.Resampling.LANCZOS)
        img.save(out_jpg, format="JPEG", quality=93)
        print(f"  [SCENE OK] {name}.jpg ({out_jpg.stat().st_size // 1024} KB, {time.time() - ts:.1f}s)", flush=True)

    # 2. 渲染 8 位极幼态 2 头身坐姿超萌宝宝原画
    raw_chibis = {}
    for idx, (pid, desc) in enumerate(CHIBIS.items()):
        out_png = CHIBI_DIR / f"{pid}.png"
        if out_png.exists() and out_png.stat().st_size > 30000:
            print(f"  [SKIP CHIBI] {pid}", flush=True)
            continue
        ts = time.time()
        gen = torch.Generator(device=device).manual_seed(884800 + idx * 77)
        prompt = f"{CHIBI_BASE_STYLE}, {desc}"
        img = pipe(
            prompt=prompt,
            negative_prompt=NEG_CHIBI,
            width=1024,
            height=1024,
            guidance_scale=5.8,
            num_inference_steps=28,
            generator=gen,
        ).images[0]
        raw_chibis[pid] = img
        print(f"  [CHIBI GEN OK] {pid} ({time.time() - ts:.1f}s)", flush=True)

    # 释放 SDXL 显存后用 BiRefNet GPU 做发丝级无白边透明抠图
    del pipe
    gc.collect()
    torch.cuda.empty_cache()

    if raw_chibis:
        biref_model, biref_tf = load_birefnet(device)
        for pid, img in raw_chibis.items():
            ts = time.time()
            rgba = mat_image(img, biref_model, biref_tf, device)
            out_png = CHIBI_DIR / f"{pid}.png"
            rgba.save(out_png, format="PNG")
            print(f"  [CHIBI MAT OK] {pid}.png {rgba.size} ({out_png.stat().st_size // 1024} KB, {time.time() - ts:.1f}s)", flush=True)
        del biref_model
        gc.collect()
        torch.cuda.empty_cache()

    print(f"=== BAKE_V7_COMPLETE in {time.time() - t0:.1f}s ===", flush=True)


if __name__ == "__main__":
    main()
