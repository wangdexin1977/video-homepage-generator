#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频首页背景图合成脚本
- 背景：整张高清图 cover 铺满目标分辨率（或 fit-blur 完整呈现整图）
- 主题文字：一半红一半黄（默认前半黄 #FFD700 / 后半红 #E60012）
- 超大加粗、铺满屏幕、不叠加、无其他文字
用法:
  python3 make_homepage_bg.py --theme "今日全球军事速览" --bg bg.jpg --out out.png \
      --split-text "今日全球军事"
  python3 make_homepage_bg.py --theme "AI 前沿播报" --bg bg.jpg --out out.png --lines 2
"""
import argparse
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

DEFAULT_COLOR1 = "#FFD700"  # 黄
DEFAULT_COLOR2 = "#E60012"  # 红
TARGET_RATIO = 0.92          # 单行总宽目标 = 屏宽 * 0.92
MIN_RATIO = 0.85             # 单行总宽下限（低于说明主题较短，属正常）
MAX_RATIO = 0.96
FONT_CANDIDATES = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 1, "Hiragino Sans GB W6"),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, "Hiragino Sans GB W3"),
    ("/System/Library/Fonts/PingFang.ttc", None, "PingFang"),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", None, "Songti"),
    ("/System/Library/Fonts/STHeiti Medium.ttc", None, "STHeiti"),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", None, "Arial Bold"),
]


def log(msg):
    print(msg, file=sys.stderr)


def find_font():
    for path, index, name in FONT_CANDIDATES:
        if os.path.exists(path):
            return path, index, name
    return None, None, None


def load_font(path, size, index):
    try:
        if path and str(path).lower().endswith((".ttc", ".otc")) and index is not None:
            return ImageFont.truetype(path, size, index=index)
        if path:
            return ImageFont.truetype(path, size)
    except Exception as e:
        log(f"[warn] 字体加载失败 {path} index={index}: {e}")
    return ImageFont.load_default()


def find_split(theme, split_text, split_n):
    """返回分割索引：theme[:idx] 用 color1，theme[idx:] 用 color2"""
    if split_text:
        idx = theme.find(split_text)
        if idx == -1:
            raise SystemExit(f"[error] --split-text '{split_text}' 不在主题 '{theme}' 中")
        idx += len(split_text)
        if not (0 < idx < len(theme)):
            raise SystemExit("[error] --split-text 分割后两段不能为空")
        return idx
    n = len(theme)
    if split_n is not None:
        if not (0 < split_n < n):
            raise SystemExit("[error] --split-n 需在 1..len-1 之间")
        return split_n
    # 自动：优先自然断点（空格/标点之后），其次正中
    seps = " 　,，。、·/-—|"
    candidates = [i for i in range(1, n) if theme[i - 1] in seps]
    if not candidates:
        candidates = [(n + 1) // 2]
    return min(candidates, key=lambda i: abs(i - n / 2))


def line_width(draw, text, font, spacing):
    w = sum(draw.textlength(ch, font=font) for ch in text)
    return w + spacing * max(0, len(text) - 1)


def pick_font_size(draw, lines, width, height, font_path, font_index, spacing_fn):
    """二分找最大字号：每行宽 ≤ 屏宽*MAX_RATIO 且总高 ≤ 屏高*0.94"""
    lo, hi = 10, int(height * 0.95)
    best = 10
    while lo <= hi:
        mid = (lo + hi) // 2
        font = load_font(font_path, mid, font_index)
        sp = spacing_fn(mid)
        wmax = max(line_width(draw, t, font, sp) for t, _ in lines)
        h = (mid * 1.4) * len(lines)  # 行高近似
        if wmax <= width * MAX_RATIO and h <= height * 0.94:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def draw_line(draw, text, font, fill, spacing, x_start, baseline, outline, stroke):
    x = x_start
    for ch in text:
        if stroke > 0:
            draw.text((x, baseline), ch, font=font, fill=fill, anchor="ls",
                      stroke_width=stroke, stroke_fill=(0, 0, 0))
        if outline > 0:
            draw.text((x, baseline), ch, font=font, fill=fill, anchor="ls",
                      stroke_width=outline, stroke_fill=fill)
        else:
            draw.text((x, baseline), ch, font=font, fill=fill, anchor="ls")
        x += draw.textlength(ch, font=font) + spacing


def wrap_text(text, font, max_w, draw):
    """按像素宽度折行（中文逐字）"""
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def draw_subtitle(canvas, subtitle, font_path, font_index, width, height):
    """副标题：下部约 1/3 处，白色小号字 + 黑描边 + 半透明黑圆角底条，最多 3 行"""
    if not subtitle:
        return
    draw = ImageDraw.Draw(canvas)
    # 字号自适应：先 52px，超 3 行则缩小，最小 28px
    size = 52
    lines = None
    while size >= 28:
        font = load_font(font_path, size, font_index)
        lines = wrap_text(subtitle, font, int(width * 0.9), draw)
        if len(lines) <= 3:
            break
        size -= 4
    font = load_font(font_path, size, font_index)
    asc, desc = font.getmetrics()
    line_h = asc + desc
    pad_x, pad_y = 28, 16
    w_max = max(draw.textlength(t, font=font) for t in lines)
    bw = w_max + pad_x * 2
    bh = len(lines) * line_h + pad_y * 2
    cx, cy = width // 2, int(height * 0.70)
    x0, y0 = cx - bw // 2, cy - bh // 2
    # 半透明黑圆角底条（提升可读性）
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=20, fill=(0, 0, 0, 110))
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(canvas)
    # 白字 + 黑描边 + 阴影
    y = y0 + pad_y
    for t in lines:
        x = cx - draw.textlength(t, font=font) / 2
        draw.text((x + 2, y + asc + 2), t, font=font, fill=(0, 0, 0, 180),
                  stroke_width=5, stroke_fill=(0, 0, 0, 255))
        draw.text((x, y + asc), t, font=font, fill=(255, 255, 255, 255),
                  stroke_width=5, stroke_fill=(0, 0, 0, 255))
        y += line_h
    log(f"[info] 副标题: {size}px, {len(lines)} 行, {len(subtitle)} 字")
    log(f"[subtitle-region] {y0} {y0 + bh}")  # 副标题底条占位区域（供字幕避让定位）


def main():
    ap = argparse.ArgumentParser(description="视频首页背景图合成")
    ap.add_argument("--theme", default="", help="主题文字（可留空：图片本身已带文字则不加任何文字）")
    ap.add_argument("--bg", required=True, help="背景图路径（高清原图）")
    ap.add_argument("--out", required=True, help="输出路径")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--color1", default=DEFAULT_COLOR1, help="前半颜色（默认黄）")
    ap.add_argument("--color2", default=DEFAULT_COLOR2, help="后半颜色（默认红）")
    ap.add_argument("--split-text", default="", help="前半段文字（推荐显式指定）")
    ap.add_argument("--split-n", type=int, default=None, help="按字符数分割")
    ap.add_argument("--spacing", type=int, default=None, help="字间距 px（默认字号/12）")
    ap.add_argument("--outline", type=int, default=0, help="同色描边加粗 px（可选）")
    ap.add_argument("--stroke", type=int, default=0, help="黑色描边 px（提升对比度，推荐亮背景用 6~10）")
    ap.add_argument("--shadow", action="store_true", help="深色阴影提升可读性")
    ap.add_argument("--dim", type=float, default=0.0, help="背景压暗 0~1")
    ap.add_argument("--mode", default="cover", choices=["cover", "fit-blur"],
                    help="cover=铺满(比例相符时零裁剪)；fit-blur=模糊延展铺底+整图完整居中")
    ap.add_argument("--lines", type=int, default=1, choices=[1, 2],
                    help="1=单行；2=前半上/后半下两行")
    ap.add_argument("--subtitle", default="", help="副标题（下部1/3处小字介绍，≤100字，可选）")
    ap.add_argument("--jpeg", action="store_true", help="输出 JPG(质量95)")
    args = ap.parse_args()

    theme = args.theme.strip()
    # 主题可为空（用户要求）：图片本身已带文字时，不添加任何主题文字

    # 1. 背景：整图铺满目标分辨率
    # 按 EXIF 方向先转正，避免手机竖图被当作横图处理。
    bg = ImageOps.exif_transpose(Image.open(args.bg)).convert("RGB")
    if bg.width < args.width or bg.height < args.height:
        log(f"[warn] 背景 {bg.width}x{bg.height} 小于输出 {args.width}x{args.height}，"
            f"将放大（画质受损，建议换更清晰的图）")
    if args.mode == "fit-blur":
        base = bg.resize((args.width, args.height), Image.LANCZOS)
        base = base.filter(ImageFilter.GaussianBlur(60))
        scale_fit = min(args.width / bg.width, args.height / bg.height)
        fg = bg.resize((math.ceil(bg.width * scale_fit), math.ceil(bg.height * scale_fit)),
                       Image.LANCZOS)
        canvas = base
        canvas.paste(fg, ((args.width - fg.width) // 2, (args.height - fg.height) // 2))
    else:
        scale = max(args.width / bg.width, args.height / bg.height)
        bg = bg.resize((math.ceil(bg.width * scale), math.ceil(bg.height * scale)), Image.LANCZOS)
        x0 = (bg.width - args.width) // 2
        y0 = (bg.height - args.height) // 2
        canvas = bg.crop((x0, y0, x0 + args.width, y0 + args.height))
    if args.dim > 0:
        overlay = Image.new("RGB", canvas.size, (0, 0, 0))
        canvas = Image.blend(canvas, overlay, min(1.0, max(0.0, args.dim)))
    draw = ImageDraw.Draw(canvas)

    # 2. 主题文字（可空：为空则图片上不加任何文字，直接使用原图）
    if theme:
        # 分割
        idx = find_split(theme, args.split_text, args.split_n)
        part1, part2 = theme[:idx], theme[idx:]
        lines = [(part1, args.color1), (part2, args.color2)]

        # 字号自适应（单行模式按两段合并总宽约束，两行模式按每行约束）
        font_path, font_index, _ = find_font()
        if not font_path:
            log("[warn] 未找到系统粗体，使用默认字体")
        spacing_fn = lambda size: args.spacing if args.spacing is not None else max(2, size // 12)
        width_lines = lines if args.lines == 2 else [(part1 + part2, args.color1)]
        size = pick_font_size(draw, width_lines, args.width, args.height, font_path, font_index, spacing_fn)
        font = load_font(font_path, size, font_index)
        spacing = spacing_fn(size)
        log(f"[info] 字号 {size}px, 字间距 {spacing}px, 分割: 「{part1}」+「{part2}」")

        # 绘制（垂直居中）
        asc, desc = font.getmetrics()
        line_h = asc + desc
        total_h = line_h * len(lines)
        y_first_baseline = args.height * 0.44 - total_h / 2 + asc
        for i, (text, color) in enumerate(lines):
            w = line_width(draw, text, font, spacing)
            x_start = (args.width - w) / 2
            baseline = y_first_baseline + i * line_h
            if args.shadow:
                off = max(2, size // 40)
                draw_line(draw, text, font, (0, 0, 0), spacing, x_start + off, baseline + off, 0, 0)
            draw_line(draw, text, font, color, spacing, x_start, baseline, args.outline, args.stroke)
    else:
        log("[info] 主题为空：不加任何主题文字，直接使用原图（图片自带文字）")
        font_path, font_index, _ = find_font()

    # 3. 副标题（可选，下部 1/3 处）
    if args.subtitle:
        draw_subtitle(canvas, args.subtitle.strip(), font_path, font_index,
                      args.width, args.height)

    # 4. 校验 + 输出
    if theme:
        if args.lines == 1:
            final_w = line_width(draw, part1 + part2, font, spacing)
        else:
            final_w = max(line_width(draw, t, font, spacing) for t, _ in lines)
        ratio = final_w / args.width
        if ratio < MIN_RATIO:
            log(f"[warn] 文字总宽仅占屏宽 {ratio:.0%}（主题较短属正常）")
        if ratio > MAX_RATIO:
            log(f"[error] 文字超出屏宽 {ratio:.0%}，请检查")
            sys.exit(1)

    if args.jpeg:
        canvas.save(args.out, "JPEG", quality=95)
    else:
        canvas.save(args.out, "PNG")
    im = Image.open(args.out)
    log(f"[ok] 输出 {args.out} {im.size[0]}x{im.size[1]}")


if __name__ == "__main__":
    main()
