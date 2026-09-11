#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频首页合成：一张或多张图片 + 配音 → MP4（可选同步字幕）一键合成

同步原理（v3）：
  1. ffmpeg silencedetect 从音频波形提取所有停顿段
  2. 自适应对齐：取时长最长的 (句数-1) 个停顿作为句边界 → 每句绝对时间戳 [t0,t1]
  3. PIL 把每句字幕预渲染为透明 PNG（黄字+黑描边+阴影，任意样式）
  4. 单命令 ffmpeg：背景图 + 逐句字幕 PNG 链式 overlay enable='between(t,t0,t1)' + 音频
     → 字幕按帧渲染（自动 10fps，误差 ≤ 1 帧 / 100ms），零累积误差

字幕排版规则（v4）：
  - --no-subtitle：完全不加字幕（只合成背景图+音频）
  - 有副标题时（--subtitle-bottom）：字幕放在副标题底条下方 14px，字号自动缩小保证不溢出
  - 无副标题时：字幕贴底部（--margin-bottom）
  - 字幕字号默认 30px（小于副标题 52px，符合"字幕 < 副标题"排版要求）

用法:
  python3 compose_video.py --bg home.png --bg image2.png --audio speech.wav --script lines.txt --out out.mp4
  python3 compose_video.py --bg bg.png --audio speech.wav --out out.mp4 --no-subtitle
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
DETECT_NOISE = "-38dB"
DETECT_GAP = 0.15
MIN_FONT = 28


def _find_bin(name):
    """探测 ffmpeg/ffprobe 绝对路径，不依赖进程 PATH（GUI/.app 启动时常缺 homebrew）。
    优先常见 homebrew/macports 路径，回退 shutil.which，最后兜底用原名。"""
    candidates = [
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/opt/local/bin/{name}",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    p = shutil.which(name)
    return p or name


FFMPEG = _find_bin("ffmpeg")
FFPROBE = _find_bin("ffprobe")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def audio_duration(path):
    r = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(r.stdout.strip())


def detect_silence(audio, noise, gap):
    r = run([FFMPEG, "-hide_banner", "-i", audio, "-af",
             f"silencedetect=noise={noise}:d={gap}", "-f", "null", "-"])
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(starts, ends))


def align_sentences(total, segs, n):
    """自适应对齐：取时长最长的 n-1 个静音段作为句间停顿，切出 n 个语句区间。
    兼容开头/结尾静音；停顿不足时返回 None。"""
    segs = [(s, e) for s, e in segs if e - s > 0.02]
    head = None
    if segs and segs[0][0] < 0.05:
        head = segs[0]
        segs = segs[1:]
    tail = None
    if segs and total - segs[-1][1] < 0.05:
        tail = segs[-1]
        segs = segs[:-1]
    if len(segs) < n - 1:
        return None
    cuts = sorted(sorted(segs, key=lambda x: -(x[1] - x[0]))[: n - 1])
    cur = head[1] if head else 0.0
    bounds = []
    for s, e in cuts:
        if s > cur + 0.01:
            bounds.append((cur, s))
        cur = max(cur, e)
    end = tail[0] if tail else total
    if end > cur + 0.01:
        bounds.append((cur, end))
    return bounds if len(bounds) == n else None


def frame_align(t, fps):
    return round(t * fps) / fps


def build_image_sequence(backgrounds, total, out_path):
    """生成 ffconcat 图片时间线。

    单图覆盖整段音频；多图按上传顺序播放，并自动追加第 1 张作为收尾首页。
    多图时开头首页和结尾首页各自最多 3 秒，其余内容图均分剩余时长。
    当音频较短、均分时长本来不足 3 秒时，所有播放段继续均分。
    末尾重复一次最后文件是 ffconcat 让最后一个 duration 生效的要求，不会额外增加播放段。
    """
    sequence = list(backgrounds)
    if len(sequence) == 1:
        durations = [total]
    else:
        sequence.append(sequence[0])
        homepage_duration = min(3.0, total / len(sequence))
        content_duration = (total - homepage_duration * 2) / (len(sequence) - 2)
        durations = [homepage_duration] + [content_duration] * (len(sequence) - 2) + [homepage_duration]

    def quote(path):
        return os.path.abspath(path).replace("\\", "\\\\").replace("'", "\\'")

    lines = ["ffconcat version 1.0"]
    for path, duration in zip(sequence, durations):
        lines.append(f"file '{quote(path)}'")
        lines.append(f"duration {duration:.9f}")
    lines.append(f"file '{quote(sequence[-1])}'")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return sequence, durations


def render_subtitle_png(text, font_size, rgb, canvas_w, margin_x, out_path):
    """PIL 渲染单句字幕为透明 PNG（黄字+黑描边+阴影，超长自动折行，水平居中）
    返回 (路径, 图片高度)"""
    from PIL import Image, ImageDraw, ImageFont
    max_chars = max(8, int((canvas_w - 2 * margin_x) / font_size))
    lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] \
        if len(text) > max_chars else [text]
    font = ImageFont.truetype(FONT, font_size, index=0)
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    line_h = int(font_size * 1.35)
    widths = []
    w_max = 0
    for ln in lines:
        bbox = d.textbbox((0, 0), ln, font=font)
        w = bbox[2] - bbox[0]
        widths.append((bbox, w))
        w_max = max(w_max, w)
    img = Image.new("RGBA", (w_max + 60, len(lines) * line_h + 24), (0, 0, 0, 0))
    dd = ImageDraw.Draw(img)
    y = 12
    for ln, (bbox, w) in zip(lines, widths):
        x = (img.width - w) // 2 - bbox[0]
        # 阴影（2px 半透明黑，先画）
        dd.text((x + 2, y - bbox[1] + 2), ln, font=font, fill=(0, 0, 0, 170),
                stroke_width=6, stroke_fill=(0, 0, 0, 255))
        # 主体（黄字 + 黑描边 6px）
        dd.text((x, y - bbox[1]), ln, font=font, fill=(rgb[0], rgb[1], rgb[2], 255),
                stroke_width=6, stroke_fill=(0, 0, 0, 255))
        y += line_h
    img.save(out_path)
    return out_path, img.height


def main():
    ap = argparse.ArgumentParser(description="首页图+配音→MP4（可选同步字幕）")
    ap.add_argument("--bg", action="append", required=True,
                    help="背景图；可重复传入多次，按参数顺序播放")
    ap.add_argument("--audio", required=True, help="配音音频")
    ap.add_argument("--script", default="", help="文字稿，每行一句（--no-subtitle 时忽略）")
    ap.add_argument("--out", required=True, help="输出 MP4")
    ap.add_argument("--no-subtitle", action="store_true", help="不使用字幕（只合成背景图+音频）")
    ap.add_argument("--subtitle-bottom", type=int, default=None,
                    help="副标题底条底部 y（有副标题时字幕放在其下方 14px，防重叠）")
    ap.add_argument("--font-size", type=int, default=30, help="字幕字号（默认30，小于副标题52）")
    ap.add_argument("--font-color", default="0xFFD700")
    ap.add_argument("--fps", type=int, default=0,
                    help="输出帧率；0=自动（单图无字幕1fps、多图无字幕5fps、有字幕10fps）")
    ap.add_argument("--noise", default=None)
    ap.add_argument("--gap", type=float, default=None)
    ap.add_argument("--margin-x", type=int, default=120)
    ap.add_argument("--margin-bottom", type=int, default=110)
    ap.add_argument("--canvas-width", type=int, default=1920, help="输出画布宽（用于字幕折行估算，默认1920）")
    ap.add_argument("--canvas-height", type=int, default=1080, help="输出画布高（用于字幕定位，默认1080）")
    args = ap.parse_args()

    for bg in args.bg:
        if not os.path.exists(bg):
            sys.exit(f"[error] 背景图不存在: {bg}")
    if not os.path.exists(args.audio):
        sys.exit(f"[error] 音频不存在: {args.audio}")
    total = audio_duration(args.audio)

    # ---------- 字幕（可选） ----------
    subs, bounds, lines = [], [], []
    if args.no_subtitle:
        print("[info] 字幕未启用（--no-subtitle），仅合成背景图+音频")
    else:
        if not os.path.exists(args.script):
            sys.exit(f"[error] 文字稿不存在: {args.script}")
        lines = [l.strip() for l in open(args.script, encoding="utf-8") if l.strip()]
        if not lines:
            sys.exit("[error] 文字稿为空")

        # 1. 静音检测 + 自适应句边界（最长 n-1 个停顿切分，与文字句数严格对齐）
        segs = detect_silence(args.audio, args.noise or DETECT_NOISE, args.gap or DETECT_GAP)
        bounds = align_sentences(total, segs, len(lines))
        print(f"[info] 音频 {total:.2f}s，静音段 {len(segs)}，文字句数 {len(lines)}")
        if bounds is None:
            print(f"[error] 句数无法对齐：音频静音段不足 {len(lines) - 1} 个（连读过多）或边界异常。")
            print("  建议：检查文字稿是否逐句对应音频（口播时每句间应有明显停顿）；")
            print("  或调整 --noise/--gap 重新检测停顿。")
            sys.exit(2)
        print(f"[ok] 已按最长停顿自适应对齐为 {len(bounds)} 句")

        # 2. 字幕 y 定位 + 字号自适应（防与副标题/屏底重叠）
        workdir = os.path.join(os.path.dirname(os.path.abspath(args.out)), "subs_tmp")
        os.makedirs(workdir, exist_ok=True)
        fc = args.font_color
        rgb = (int(fc[2:4], 16), int(fc[4:6], 16), int(fc[6:8], 16)) if fc.startswith("0x") and len(fc) == 8 \
            else (255, 215, 0)
        h = args.canvas_height
        y_base = None  # 顶部对齐基准（副标题下方模式）；None = 贴底对齐
        if args.subtitle_bottom is not None:
            y_base = args.subtitle_bottom + 56  # 副标题下方 56px 呼吸空间，防重叠
            # 字号自适应：y_base + 最高字幕 ≤ H-20，最低 28px
            font_size = args.font_size
            paths, heights = [], []
            while True:
                paths, heights = [], []
                for i, ((t0, t1), text) in enumerate(zip(bounds, lines)):
                    p, hh = render_subtitle_png(text, font_size, rgb, args.canvas_width,
                                                args.margin_x, os.path.join(workdir, f"l{i:02d}.png"))
                    paths.append(p)
                    heights.append(hh)
                if y_base + max(heights) <= h - 20 or font_size <= MIN_FONT:
                    break
                font_size -= 4
            if font_size != args.font_size:
                print(f"[info] 字幕区空间不足，字号 {args.font_size}→{font_size}px")
            if y_base + max(heights) > h - 20:
                y_base = h - 20 - max(heights)
                print(f"[warn] 副标题+字幕超高，字幕下移至 y={y_base}（可能贴近副标题）")
        else:
            font_size = args.font_size
            paths, heights = [], []
            for i, ((t0, t1), text) in enumerate(zip(bounds, lines)):
                p, hh = render_subtitle_png(text, font_size, rgb, args.canvas_width,
                                            args.margin_x, os.path.join(workdir, f"l{i:02d}.png"))
                paths.append(p)
                heights.append(hh)
        subs = list(zip(paths, heights))
        print(f"[ok] 字幕渲染完成：字号 {font_size}px，{len(subs)} 句")

    # ---------- 3. 图片时间线 + 单命令合成 ----------
    sequence_file = os.path.join(os.path.dirname(os.path.abspath(args.out)), "image_sequence.ffconcat")
    sequence, durations = build_image_sequence(args.bg, total, sequence_file)
    if args.fps > 0:
        output_fps = args.fps
    elif not args.no_subtitle:
        output_fps = 10
    elif len(args.bg) == 1:
        output_fps = 1
    else:
        # 多图至少 5fps；短播放段自动提高帧率，尽量保证每张图片都能出现。
        shortest_segment = min(durations)
        output_fps = min(25, max(5, int(1 / shortest_segment) + 1))
    if len(args.bg) == 1:
        print(f"[info] 单图模式：首页覆盖整段音频 {durations[0]:.2f}s")
    else:
        print(f"[info] 图片 {len(args.bg)} 张，播放段 {len(sequence)} 段；"
              f"首页首段 {durations[0]:.2f}s，内容图每张 {durations[1]:.2f}s，"
              f"结尾首页 {durations[-1]:.2f}s")
    if len(args.bg) > 1:
        print("[info] 播放顺序：" + " → ".join(str(i + 1) for i in range(len(args.bg))) + " → 1（回到首页）")
    print(f"[info] 性能模式：{output_fps}fps（静态图片自适应帧率）")
    if len(args.bg) == 1:
        # 图片只解码一次，再由滤镜循环供帧；避免长音频反复解码同一张 PNG/JPEG。
        inputs = ["-i", args.bg[0]]
    else:
        inputs = ["-f", "concat", "-safe", "0", "-i", sequence_file]
    for p, _ in subs:
        inputs += ["-i", p]
    audio_idx = len(subs) + 1
    fc_parts = []
    if len(args.bg) == 1:
        fc_parts.append(
            f"[0:v]loop=loop=-1:size=1:start=0,trim=duration={total:.6f},"
            f"setpts=N/({output_fps}*TB)[base]"
        )
        prev = "base"
    else:
        prev = "0:v"
    if not subs:
        fc_parts.append(f"[{prev}]format=yuv420p[v]")
    else:
        for i, ((t0, t1), (p, hh)) in enumerate(zip(bounds, subs)):
            a, b = frame_align(t0, output_fps), frame_align(t1, output_fps)
            out = "v" if i == len(subs) - 1 else f"s{i}"
            if y_base is not None:
                y_expr = str(y_base)  # 顶部对齐（副标题下方）
            else:
                y_expr = f"main_h-overlay_h-{args.margin_bottom}"  # 贴底对齐
            fc_parts.append(
                f"[{prev}][{i + 1}:v]overlay=x=(main_w-overlay_w)/2:y={y_expr}"
                f":enable='between(t,{a:.3f},{b:.3f})'[{out}]"
            )
            prev = out
        fc_parts.append(f"[{prev}]format=yuv420p[v]")
    fc = ";".join(fc_parts)

    common = [FFMPEG, "-y"] + inputs + ["-i", args.audio,
              "-filter_complex", fc,
              "-map", "[v]", "-map", f"{audio_idx}:a"]
    tail = ["-r", str(output_fps), "-shortest", "-t", f"{total:.6f}",
            "-movflags", "+faststart", args.out]
    if sys.platform == "darwin":
        video_codec = ["-c:v", "h264_videotoolbox", "-profile:v", "high", "-q:v", "65",
                       "-realtime", "true", "-prio_speed", "1"]
        audio_codec = ["-c:a", "aac_at", "-b:a", "192k", "-aac_at_quality", "0",
                       "-ar", "44100", "-ac", "2"]
        encoder_name = "Apple VideoToolbox + AudioToolbox"
    else:
        video_codec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "21"]
        audio_codec = ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
        encoder_name = "libx264 + AAC"
    cmd = common + video_codec + audio_codec + tail
    print(f"[info] 加速编码：{encoder_name}")
    print("[info] 单命令合成中...")
    r = run(cmd)
    if r.returncode != 0 and sys.platform == "darwin":
        print("[warn] Apple 编码器当前不可用，自动回退到通用快速编码器")
        fallback_video = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "21"]
        fallback_audio = ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
        r = run(common + fallback_video + fallback_audio + tail)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        sys.exit(1)

    print(f"[ok] 输出 {args.out}")
    if subs:
        print("[同步表] 句号 起始~结束(帧对齐) 文字")
        for i, ((t0, t1), text) in enumerate(zip(bounds, lines)):
            a, b = frame_align(t0, output_fps), frame_align(t1, output_fps)
            print(f"  {i + 1:02d} {a:7.2f}s~{b:7.2f}s  {text[:32]}")
    else:
        print("[同步表] 字幕未启用")


if __name__ == "__main__":
    main()
