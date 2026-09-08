#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频首页 MP4 生成器 WebUI
上传 音频 + 口播文稿(MD) + 参考图片 → 一键生成带同步字幕的 MP4

流程（与 video-homepage-image + video-homepage-compose skill 一致）：
  口播文稿解析出主题与逐句文字稿
  → 参考图合成首页背景图（高清整图 fit-blur + 红黄主题大字）
  → 音频静音检测切句（绝对时间戳帧对齐）
  → 单命令 ffmpeg 合成 MP4（drawtext 逐句同步字幕）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(BASE, "jobs")
SCRIPTS = os.path.join(BASE, "scripts")
BG_SCRIPT = os.path.join(SCRIPTS, "make_homepage_bg.py")
COMPOSE_SCRIPT = os.path.join(SCRIPTS, "compose_video.py")
os.makedirs(JOBS, exist_ok=True)

ALLOWED_AUDIO = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")
ALLOWED_IMAGE = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# 桌面输出目录：每个任务完成后自动把 MP4 复制到这里，方便快速找到成品
OUT_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "视频首页生成器输出")
os.makedirs(OUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 800 * 1024 * 1024  # 800MB


# ---------- 工具 ----------
def log(job_id, msg):
    # 过滤控制字符（ffmpeg 进度 \r 等），避免污染日志与 JSON
    msg = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", msg)
    p = os.path.join(JOBS, job_id, "log.txt")
    with open(p, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def set_status(job_id, st):
    with open(os.path.join(JOBS, job_id, "status.txt"), "w", encoding="utf-8") as f:
        f.write(st)


def save_result(job_id, data):
    with open(os.path.join(JOBS, job_id, "result.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run_pipe(cmd, job_id):
    """执行命令并把 stdout+stderr 实时写入 job 日志"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        if line.strip():
            log(job_id, line.rstrip("\n"))
    proc.wait()
    return proc.returncode


def _find_bin(name):
    import shutil
    for c in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/opt/local/bin/{name}"):
        if os.path.exists(c):
            return c
    return shutil.which(name) or name


FFPROBE = _find_bin("ffprobe")


def ffprobe_duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path],
                       capture_output=True, text=True)
    try:
        return round(float(r.stdout.strip()), 2)
    except Exception:
        return 0.0


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "video"


def image_filename_stem(filename):
    """保留第一张图片原名，仅去掉扩展名并替换文件系统禁止字符。"""
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(".") or "video"


def available_export_path(directory, stem):
    """返回不覆盖旧文件的导出路径：名称.mp4、名称_2.mp4、名称_3.mp4……"""
    path = os.path.join(directory, stem + ".mp4")
    index = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{stem}_{index}.mp4")
        index += 1
    return path


# ---------- 口播文稿解析 ----------
def parse_script(md_text):
    """返回 (theme, sentences)。
    theme 恒为空字符串：主题文字完全由界面输入决定，
    不再默认取文案首个标题（用户明确要求取消该行为）。"""
    lines = [l.strip() for l in md_text.splitlines()]
    theme = ""
    body = []
    for l in lines:
        if not l:
            continue
        if l.startswith("#"):
            continue  # 标题不再作为主题（用户要求）
        body.append(re.sub(r"^[-*•\d.)\s]+", "", l).strip())
    text = " ".join(x for x in body if x)
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?])", text) if s.strip()]
    if len(sentences) <= 1 and len(body) > 1:
        sentences = [x for x in body if x]
    return theme, sentences


# ---------- 异步任务 ----------
def process_job(job_id, audio_path, img_paths, script_path, form, output_stem="video"):
    wd = os.path.join(JOBS, job_id)
    try:
        set_status(job_id, "running")
        log(job_id, "=== 视频首页 MP4 生成 ===")

        # 1. 解析口播文稿
        log(job_id, "[1/4] 解析口播文稿...")
        md_text = ""
        if script_path and os.path.exists(script_path):
            with open(script_path, "r", encoding="utf-8-sig", errors="replace") as f:
                md_text = f.read()
        theme, sentences = parse_script(md_text)
        theme = form.get("theme", "").strip()  # 主题由界面输入决定；留空 = 不加主题文字（图片自带）
        no_sub = form.get("no_subtitle") in ("1", "true", "on", "yes") or not sentences
        lines_path = os.path.join(wd, "lines.txt")
        with open(lines_path, "w", encoding="utf-8") as f:
            f.write("\n".join(sentences))
        if not sentences:
            log(job_id, "未提供口播文案，自动关闭字幕，仅合成画面 + 音频")
        log(job_id, f"主题：{theme or '(无，图片自带文字)'} ｜ 文字句数：{len(sentences)}")

        # 2. 统一处理图片序列：第 1 张是首页，后续图片不重复叠加首页标题
        log(job_id, f"[2/4] 处理图片序列（共 {len(img_paths)} 张；第 1 张为首页）...")
        size_mode = form.get("size_mode", "manual")
        with Image.open(img_paths[0]) as first_image:
            first_image = ImageOps.exif_transpose(first_image)
            source_w, source_h = first_image.size
        if size_mode == "auto":
            # libx264 + yuv420p 要求宽高为偶数；奇数尺寸只补 1 像素。
            w = source_w + source_w % 2
            h = source_h + source_h % 2
            orientation = "横屏" if w > h else "竖屏" if h > w else "方形"
            log(job_id, f"自动尺寸：首图 {source_w}x{source_h} → 输出 {w}x{h}（{orientation}）")
            if (w, h) != (source_w, source_h):
                log(job_id, "[warn] 首图存在奇数边长，为兼容 H.264 编码已将对应边长补 1 像素")
        else:
            w = int(form.get("width", 1920))
            h = int(form.get("height", 1080))
            orientation = "横屏" if w > h else "竖屏" if h > w else "方形"
            log(job_id, f"手动尺寸：输出 {w}x{h}（{orientation}）")
        mode = form.get("bg_mode", "fit-blur")  # fit-blur: 整图完整呈现，模糊延展铺底
        split = form.get("split_text", "").strip()
        subtitle = form.get("subtitle", "").strip()
        frame_paths = []
        for index, img_path in enumerate(img_paths):
            is_homepage = index == 0
            frame_out = os.path.join(wd, "homepage.png" if is_homepage else f"frame_{index + 1:02d}.png")
            bg_cmd = [sys.executable, BG_SCRIPT, "--bg", img_path,
                      "--out", frame_out, "--width", str(w), "--height", str(h),
                      "--mode", mode]
            if is_homepage:
                bg_cmd += ["--shadow", "--dim", "0.3", "--stroke", "8"]
                if theme:
                    bg_cmd += ["--theme", theme]
                if split:
                    bg_cmd += ["--split-text", split]
                if subtitle:
                    bg_cmd += ["--subtitle", subtitle]
            log(job_id, f"  图片 {index + 1}/{len(img_paths)}：{'首页' if is_homepage else '内容图'}")
            rc = run_pipe(bg_cmd, job_id)
            if rc != 0 or not os.path.exists(frame_out):
                raise RuntimeError(f"第 {index + 1} 张图片处理失败（见上方日志）")
            frame_paths.append(frame_out)
        # 解析副标题底条区域（供字幕避让定位，防与副标题重叠）
        sub_region = None
        with open(os.path.join(wd, "log.txt"), "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r"\[subtitle-region\] (\d+) (\d+)", line)
                if m:
                    sub_region = (int(m.group(1)), int(m.group(2)))
        # 3. 字幕对齐 + 合成 MP4
        log(job_id, "[3/4] 音频切句对齐 + 单命令合成 MP4（绝对时间戳同步字幕）...")
        mp4_out = os.path.join(wd, "out.mp4")
        comp_cmd = [sys.executable, COMPOSE_SCRIPT, "--audio", audio_path,
                    "--out", mp4_out, "--canvas-width", str(w),
                    "--canvas-height", str(h)]
        for frame_path in frame_paths:
            comp_cmd += ["--bg", frame_path]
        if no_sub:
            comp_cmd += ["--no-subtitle"]
        else:
            comp_cmd += ["--script", lines_path,
                        "--font-size", str(int(form.get("font_size", 30))),
                        "--font-color", form.get("font_color", "0xFFD700")]
            if sub_region:
                comp_cmd += ["--subtitle-bottom", str(sub_region[1])]
            if form.get("margin_bottom"):
                comp_cmd += ["--margin-bottom", str(int(form["margin_bottom"]))]
        rc = run_pipe(comp_cmd, job_id)
        if rc != 0 or not os.path.exists(mp4_out):
            raise RuntimeError("MP4 合成失败（见上方日志；若报错含 ffprobe/ffmpeg 未找到，多为服务通过桌面 .app 启动导致 PATH 不含 homebrew，已修复为绝对路径，重启服务即可）")

        # 4. 汇总结果
        log(job_id, "[4/4] 校验并汇总...")
        sync = []
        in_sync = False
        with open(os.path.join(wd, "log.txt"), "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("[同步表]"):
                    in_sync = True
                    continue
                if in_sync and line.strip():
                    m = re.match(r"\s*(\d+)\s+([\d.]+)s~\s*([\d.]+)s\s+(.+)", line)
                    if m:
                        sync.append({"no": int(m.group(1)), "t0": float(m.group(2)),
                                     "t1": float(m.group(3)), "text": m.group(4).strip()})
        result = {
            "theme": theme,
            "sentences": len(sentences),
            "sync": sync,
            "audio_duration": ffprobe_duration(audio_path),
            "video_duration": ffprobe_duration(mp4_out),
            "out_name": output_stem + ".mp4",
            "source_image_name": output_stem,
            "subtitle_enabled": not no_sub,
            "image_count": len(img_paths),
            "sequence_count": len(img_paths) + (1 if len(img_paths) > 1 else 0),
            "source_width": source_w,
            "source_height": source_h,
            "output_width": w,
            "output_height": h,
            "orientation": orientation,
        }

        # 自动导出 MP4 到桌面「视频首页生成器输出」文件夹
        try:
            export_path = available_export_path(OUT_DIR, output_stem)
            export_name = os.path.basename(export_path)
            import shutil
            shutil.copy2(mp4_out, export_path)
            result["export_path"] = export_path
            result["export_name"] = export_name
            log(job_id, f"[ok] 已导出到桌面：{export_path}")
        except Exception as e:
            log(job_id, f"[warn] 导出到桌面失败：{e}")

        save_result(job_id, result)
        log(job_id, f"[ok] 完成！MP4 时长 {result['video_duration']}s，图片 {len(img_paths)} 张，字幕 {len(sync)} 句")
        set_status(job_id, "done")
    except Exception as e:
        log(job_id, f"[error] {e}")
        set_status(job_id, "error")


# ---------- 路由 ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/generate", methods=["POST"])
def api_generate():
    audio = request.files.get("audio")
    script = request.files.get("script")
    images = [f for f in request.files.getlist("images") if f and f.filename]
    if not images:
        legacy_image = request.files.get("image")
        if legacy_image and legacy_image.filename:
            images = [legacy_image]
    if not audio or not images:
        return jsonify({"error": "请同时上传音频和至少 1 张图片"}), 400
    output_stem = image_filename_stem(images[0].filename)
    aext = os.path.splitext(audio.filename)[1].lower()
    if aext not in ALLOWED_AUDIO:
        return jsonify({"error": f"音频格式不支持：{aext}（支持 wav/mp3/m4a/aac/flac/ogg）"}), 400
    for image in images:
        iext = os.path.splitext(image.filename)[1].lower()
        if iext not in ALLOWED_IMAGE:
            return jsonify({"error": f"图片格式不支持：{iext}（支持 png/jpg/jpeg/webp/bmp）"}), 400

    job_id = uuid.uuid4().hex[:10]
    wd = os.path.join(JOBS, job_id)
    os.makedirs(wd)
    audio_path = os.path.join(wd, "audio" + aext)
    script_path = None
    audio.save(audio_path)
    img_paths = []
    for index, image in enumerate(images):
        iext = os.path.splitext(image.filename)[1].lower()
        img_path = os.path.join(wd, f"image_{index + 1:02d}" + iext)
        image.save(img_path)
        img_paths.append(img_path)
    if script and script.filename:
        script_path = os.path.join(wd, "script.md")
        script.save(script_path)
    set_status(job_id, "pending")
    log(job_id, "任务已创建，开始处理...")

    t = threading.Thread(target=process_job,
                         args=(job_id, audio_path, img_paths, script_path, request.form, output_stem),
                         daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>/status")
def api_status(job_id):
    wd = os.path.join(JOBS, job_id)
    if not os.path.isdir(wd):
        abort(404)
    try:
        with open(os.path.join(wd, "status.txt"), "r", encoding="utf-8") as f:
            status = f.read().strip()
    except FileNotFoundError:
        status = "pending"
    log_text = ""
    try:
        with open(os.path.join(wd, "log.txt"), "r", encoding="utf-8") as f:
            log_text = f.read()
    except FileNotFoundError:
        pass
    return jsonify({"job_id": job_id, "status": status, "log": log_text})


@app.route("/api/job/<job_id>/result")
def api_result(job_id):
    p = os.path.join(JOBS, job_id, "result.json")
    if not os.path.exists(p):
        abort(404)
    with open(p, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/job/<job_id>/download")
def api_download(job_id):
    wd = os.path.join(JOBS, job_id)
    mp4 = os.path.join(wd, "out.mp4")
    if not os.path.exists(mp4):
        abort(404)
    rp = os.path.join(wd, "result.json")
    name = "video.mp4"
    if os.path.exists(rp):
        with open(rp, "r", encoding="utf-8") as f:
            name = json.load(f).get("out_name", "video.mp4")
    return send_file(mp4, as_attachment=True, download_name=name,
                     mimetype="video/mp4")


@app.route("/api/job/<job_id>/video")
def api_video(job_id):
    """MP4 快速浏览入口：内嵌播放器直接播放（支持 Range 拖动）"""
    wd = os.path.join(JOBS, job_id)
    mp4 = os.path.join(wd, "out.mp4")
    if not os.path.exists(mp4):
        abort(404)
    return send_file(mp4, mimetype="video/mp4", conditional=True)


@app.route("/api/jobs")
def api_jobs():
    """历史任务列表：页面刷新后仍可找到所有已生成的 MP4"""
    items = []
    for d in sorted(os.listdir(JOBS), reverse=True):
        wd = os.path.join(JOBS, d)
        if not os.path.isdir(wd):
            continue
        info = {"job_id": d, "status": "pending", "created": os.path.getmtime(wd)}
        try:
            with open(os.path.join(wd, "status.txt"), "r", encoding="utf-8") as f:
                info["status"] = f.read().strip()
        except FileNotFoundError:
            pass
        rp = os.path.join(wd, "result.json")
        if os.path.exists(rp):
            try:
                with open(rp, "r", encoding="utf-8") as f:
                    r = json.load(f)
                for k in ("theme", "video_duration", "audio_duration", "sentences",
                          "subtitle_enabled", "image_count", "sequence_count",
                          "source_width", "source_height", "output_width", "output_height", "orientation",
                          "out_name", "source_image_name", "export_path", "export_name"):
                    if k in r:
                        info[k] = r[k]
            except Exception:
                pass
        mp4 = os.path.join(wd, "out.mp4")
        info["mp4_exists"] = os.path.exists(mp4)
        if info["mp4_exists"]:
            info["mp4_size"] = os.path.getsize(mp4)
            info["mp4_path"] = mp4
        items.append(info)
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    return jsonify({"jobs": items})


@app.route("/api/job/<job_id>/reveal", methods=["POST"])
def api_reveal(job_id):
    """在 Finder 中定位 MP4 文件"""
    wd = os.path.join(JOBS, job_id)
    mp4 = os.path.join(wd, "out.mp4")
    if not os.path.exists(mp4):
        abort(404)
    subprocess.Popen(["open", "-R", mp4])
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/open_dir", methods=["POST"])
def api_open_dir(job_id):
    """打开任务所在文件夹"""
    wd = os.path.join(JOBS, job_id)
    if not os.path.isdir(wd):
        abort(404)
    subprocess.Popen(["open", wd])
    return jsonify({"ok": True})


@app.route("/api/open_outdir", methods=["POST"])
def api_open_outdir():
    """打开桌面输出文件夹（视频首页生成器输出）"""
    subprocess.Popen(["open", OUT_DIR])
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/bg")
def api_bg(job_id):
    p = os.path.join(JOBS, job_id, "homepage.png")
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="image/png")


def cleanup_old_jobs(max_age_days=7):
    now = time.time()
    for d in os.listdir(JOBS):
        p = os.path.join(JOBS, d)
        if os.path.isdir(p) and now - os.path.getmtime(p) > max_age_days * 86400:
            try:
                import shutil
                shutil.rmtree(p)
            except Exception:
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="视频首页 MP4 生成器 WebUI")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    cleanup_old_jobs()
    print(f"视频首页 MP4 生成器：http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
