#!/usr/bin/env python3
"""Pillow 图像内核 · CKP 1.0 适配器。

能力：convert / transform / optimize / inspect
引擎：Python Pillow（可选 pillow-heif 扩展 HEIC/HEIF 读写）
"""

from __future__ import annotations

import json
import os
import sys

# --- 让适配器可以脱离宿主独立运行 ----------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_ROOT, "vendor"), _ROOT):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from kernelhub.sdk import (  # noqa: E402
    CkpError,
    KernelApp,
    canonical_format,
    emit_artifact,
    ensure_parent,
    format_of_path,
)

KERNEL_ID = "pillow-image"
VERSION = "1.2.0"

#: 不支持透明通道的输出格式 —— 需要先铺底色
NO_ALPHA = {"jpg", "jpeg", "jfif", "bmp", "dib", "ppm", "pgm", "pbm", "pnm",
            "pdf", "pcx", "dds", "jp2", "j2k"}

#: 支持多帧保存的输出格式
ANIMATED_OUT = {"gif", "webp", "tif", "tiff", "apng", "png"}

#: Pillow 保存时使用的格式名（默认取大写扩展名）
SAVE_FORMAT = {
    "jpg": "JPEG", "jpeg": "JPEG", "jfif": "JPEG",
    "tif": "TIFF", "tiff": "TIFF",
    "heic": "HEIF", "heif": "HEIF",
    "jp2": "JPEG2000", "j2k": "JPEG2000",
    "ico": "ICO",
    "pdf": "PDF",
    "png": "PNG",
}


def _load_engine():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise CkpError("DEPENDENCY_MISSING",
                       "未安装 Pillow，请运行：python tools/pip_bootstrap.py install "
                       "--target vendor Pillow", str(exc)) from exc

    heif_note = ""
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        heif_note = f"pillow-heif {getattr(pillow_heif, '__version__', '?')}"
    except Exception:  # noqa: BLE001
        heif_note = ""

    try:
        from PIL import Image as _I

        version = f"Pillow {getattr(_I, '__version__', '?')}"
    except Exception:  # noqa: BLE001
        version = "Pillow"
    return Image, ImageOps, (version + (f" + {heif_note}" if heif_note else ""))


def _parse_color(value, Image):
    text = str(value or "").strip()
    if not text:
        return (255, 255, 255)
    if text.startswith("#"):
        hexpart = text[1:]
        if len(hexpart) == 3:
            hexpart = "".join(c * 2 for c in hexpart)
        if len(hexpart) >= 6:
            try:
                return (int(hexpart[0:2], 16), int(hexpart[2:4], 16), int(hexpart[4:6], 16))
            except ValueError:
                return (255, 255, 255)
    if "," in text:
        try:
            parts = [int(float(x)) for x in text.split(",")[:3]]
            while len(parts) < 3:
                parts.append(255)
            return tuple(max(0, min(255, p)) for p in parts)
        except ValueError:
            return (255, 255, 255)
    try:
        from PIL import ImageColor

        return ImageColor.getrgb(text)[:3]
    except Exception:  # noqa: BLE001
        return (255, 255, 255)


def _resample_filter(name, Image):
    table = {
        "nearest": Image.Resampling.NEAREST,
        "box": Image.Resampling.BOX,
        "bilinear": Image.Resampling.BILINEAR,
        "bicubic": Image.Resampling.BICUBIC,
        "lanczos": Image.Resampling.LANCZOS,
    }
    return table.get(str(name or "lanczos").lower(), Image.Resampling.LANCZOS)


def _apply_transforms(img, params, Image, ImageOps):
    changed = False

    if bool(params.get("grayscale")):
        img = img.convert("L")
        changed = True

    crop = str(params.get("crop") or "").strip()
    if crop:
        try:
            parts = [int(float(x)) for x in crop.replace(";", ",").split(",")]
            if len(parts) == 4:
                left, top, right, bottom = parts
                if right <= left:
                    right = left + max(1, right)
                if bottom <= top:
                    bottom = top + max(1, bottom)
                img = img.crop((left, top, right, bottom))
                changed = True
        except (ValueError, TypeError):
            pass

    rotate = float(params.get("rotate") or 0)
    if rotate:
        img = img.rotate(rotate, expand=True, resample=Image.Resampling.BICUBIC)
        changed = True

    flip = str(params.get("flip") or "none").lower()
    if flip == "horizontal":
        img = ImageOps.mirror(img)
        changed = True
    elif flip == "vertical":
        img = ImageOps.flip(img)
        changed = True
    elif flip == "both":
        img = ImageOps.mirror(ImageOps.flip(img))
        changed = True

    tw = int(params.get("resize_w") or 0)
    th = int(params.get("resize_h") or 0)
    if tw > 0 or th > 0:
        keep = bool(params.get("keep_aspect", True))
        cur_w, cur_h = img.size
        if keep:
            if tw > 0 and th > 0:
                ratio = min(tw / cur_w, th / cur_h)
            elif tw > 0:
                ratio = tw / cur_w
            else:
                ratio = th / cur_h
            target = (max(1, round(cur_w * ratio)), max(1, round(cur_h * ratio)))
        else:
            target = (tw if tw > 0 else cur_w, th if th > 0 else cur_h)
        if target != img.size:
            img = img.resize(target, _resample_filter(params.get("resample"), Image))
            changed = True

    return img, changed


def _prepare_mode(img, out_fmt, background, Image):
    """把图像调整为目标格式能接受的色彩模式。"""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info)

    if out_fmt in NO_ALPHA:
        if has_alpha:
            rgba = img.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, background)
            canvas.paste(rgba, mask=rgba.split()[-1])
            return canvas
        if img.mode in ("CMYK", "YCbCr", "I;16", "I", "F"):
            return img.convert("RGB")
        if img.mode == "P":
            return img.convert("RGB")
        return img

    if out_fmt in ("png", "webp", "avif", "tif", "tiff", "gif", "ico", "jp2"):
        if img.mode == "P":
            return img.convert("RGBA" if "transparency" in img.info else "RGB")
        if img.mode in ("CMYK", "I;16", "I", "F", "YCbCr"):
            return img.convert("RGB")
        return img

    if img.mode == "P":
        return img.convert("RGB")
    return img


def _save_kwargs(out_fmt, params, img, save_format):
    kw = {}
    quality = int(params.get("quality") or 88)
    optimize = bool(params.get("optimize", True))
    lossless = bool(params.get("lossless", False))

    if save_format == "JPEG":
        kw.update(quality=max(1, min(100, quality)), optimize=optimize,
                  progressive=bool(params.get("progressive", False)))
    elif save_format == "PNG":
        kw.update(compress_level=max(0, min(9, int(params.get("png_compress_level") or 6))),
                  optimize=optimize)
    elif save_format == "WEBP":
        if lossless:
            kw.update(lossless=True)
        else:
            kw.update(quality=max(1, min(100, quality)), method=4)
    elif save_format == "AVIF":
        kw.update(quality=max(1, min(100, quality)), speed=6)
    elif save_format == "JPEG2000":
        kw.update(quality_mode="rates", quality_layers=[max(1, 100 - quality)])
    elif save_format == "TIFF":
        kw.update(compression="tiff_deflate" if optimize else "raw")
    elif save_format == "HEIF":
        kw.update(quality=max(1, min(100, quality)))
    elif save_format == "PDF":
        kw.update(resolution=float(params.get("pdf_dpi") or 150))
    elif save_format == "ICO":
        sizes = []
        for part in str(params.get("ico_sizes") or "16,32,48,256").split(","):
            try:
                sizes.append(int(float(part)))
            except ValueError:
                continue
        if sizes:
            kw.update(sizes=[(s, s) for s in sorted(set(sizes))])

    dpi = int(params.get("dpi") or 0)
    if dpi > 0 and save_format in ("JPEG", "PNG", "TIFF", "WEBP", "PDF"):
        kw["dpi"] = (dpi, dpi)

    return kw


def _handle_convert(job, app, Image, ImageOps, engine_note):
    params = dict(job.get("params") or {})
    inputs = [i["path"] for i in job["inputs"]]
    outputs = job.get("outputs") or []
    if not outputs:
        raise CkpError("BAD_JOB", "convert 操作需要至少一个 outputs")

    src = inputs[0]
    if not os.path.isfile(src):
        raise CkpError("INPUT_NOT_FOUND", f"输入文件不存在: {src}")
    dst = outputs[0]["path"]
    out_fmt = canonical_format(outputs[0].get("format") or format_of_path(dst))
    ensure_parent(dst)

    app.progress(0.1, "打开图像")
    try:
        img = Image.open(src)
        img.load()
    except FileNotFoundError as exc:
        raise CkpError("INPUT_NOT_FOUND", f"输入文件不存在: {src}", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise CkpError("INPUT_UNREADABLE",
                       f"无法解码图像（{format_of_path(src) or '未知格式'}）: {exc}",
                       str(exc)) from exc

    app.log(f"源: {img.format or '?'} {img.mode} {img.size[0]}x{img.size[1]} "
            f"帧数={getattr(img, 'n_frames', 1)}")

    # 多帧信息必须在任何可能"压平"帧的操作之前取。
    # 注意 ImageOps.exif_transpose() 会返回单帧新图，直接毁掉 GIF/WEBP 动画，
    # 所以只对静态图做自动摆正。
    frames = getattr(img, "n_frames", 1)
    default_duration = img.info.get("duration", 100)
    default_loop = img.info.get("loop", 0)

    if bool(params.get("autoorient", True)) and frames <= 1:
        try:
            img = ImageOps.exif_transpose(img) or img
        except Exception:  # noqa: BLE001
            pass

    frame_sel = int(params.get("frame", 0))
    keep_all = frame_sel < 0 and frames > 1 and out_fmt in ANIMATED_OUT

    background = _parse_color(params.get("background"), Image)
    save_format = SAVE_FORMAT.get(out_fmt, out_fmt.upper() if out_fmt else "")
    if not save_format:
        raise CkpError("UNSUPPORTED_FORMAT", f"无法确定输出格式: {out_fmt!r}")

    app.progress(0.3, "应用变换")
    if keep_all:
        out_frames = []
        for idx in range(frames):
            img.seek(idx)
            frame = img.convert("RGBA") if img.mode == "P" else img.copy()
            frame, _ = _apply_transforms(frame, params, Image, ImageOps)
            out_frames.append(_prepare_mode(frame, out_fmt, background, Image))
        save_img = out_frames[0]
        extra_kw = {"save_all": True, "append_images": out_frames[1:],
                    "duration": default_duration, "loop": default_loop}
    else:
        if frames > 1:
            img.seek(max(0, min(frame_sel, frames - 1)))
        working = img.copy()
        working, _ = _apply_transforms(working, params, Image, ImageOps)
        save_img = _prepare_mode(working, out_fmt, background, Image)
        extra_kw = {}

    if bool(params.get("strip_metadata", False)):
        save_img.info.clear()

    kw = _save_kwargs(out_fmt, params, save_img, save_format)
    kw.update(extra_kw)

    app.progress(0.6, f"编码为 {save_format}")
    try:
        save_img.save(dst, format=save_format, **kw)
    except KeyError as exc:
        raise CkpError("UNSUPPORTED_FORMAT",
                       f"当前 Pillow 构建不支持写出 {out_fmt}（缺少编码器）: {exc}",
                       str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise CkpError("INTERNAL", f"保存失败: {exc}", str(exc)) from exc

    meta = {"width": save_img.size[0], "height": save_img.size[1],
            "mode": save_img.mode, "engine": engine_note, "frames": len(out_frames) if keep_all else 1}
    info = emit_artifact(dst, out_fmt, primary=True, meta=meta)
    app.progress(1.0, "完成")
    return [info]


def _handle_inspect(job, app, Image, _ImageOps, engine_note):
    src = job["inputs"][0]["path"]
    try:
        img = Image.open(src)
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise CkpError("INPUT_UNREADABLE", f"无法读取图像: {exc}", str(exc)) from exc

    meta = {
        "path": src,
        "format": img.format,
        "mode": img.mode,
        "width": img.size[0],
        "height": img.size[1],
        "frames": getattr(img, "n_frames", 1),
        "is_animated": bool(getattr(img, "is_animated", False)),
        "has_alpha": img.mode in ("RGBA", "LA", "PA", "P") and (
            img.mode != "P" or "transparency" in img.info),
        "engine": engine_note,
    }
    try:
        exif = img.getexif()
        if exif:
            meta["exif"] = {str(k): str(v)[:200] for k, v in list(exif.items())[:40]}
    except Exception:  # noqa: BLE001
        pass
    try:
        meta["info_keys"] = sorted(str(k) for k in img.info.keys())[:30]
    except Exception:  # noqa: BLE001
        pass

    app.log(json.dumps(meta, ensure_ascii=False))
    app.extra_metrics.update(meta)

    outputs = job.get("outputs") or []
    if outputs and str(outputs[0].get("format", "")).lower() == "json":
        dst = outputs[0]["path"]
        ensure_parent(dst)
        with open(dst, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        return [emit_artifact(dst, "json", primary=True)]
    return []


def main(argv=None) -> int:
    Image, ImageOps, engine_note = _load_engine()
    app = KernelApp(KERNEL_ID, VERSION, engine=engine_note)

    def handler(job, _app):
        op = str(job.get("op", "convert"))
        if op in ("convert", "transform", "optimize"):
            params = dict(job.get("params") or {})
            if op == "optimize":
                params.setdefault("strip_metadata", True)
            job["params"] = params
            return _handle_convert(job, _app, Image, ImageOps, engine_note)
        if op == "inspect":
            return _handle_inspect(job, _app, Image, ImageOps, engine_note)
        raise CkpError("UNSUPPORTED_FORMAT", f"Pillow 内核不支持操作: {op}")

    return app.run(handler, argv)


if __name__ == "__main__":
    raise SystemExit(main())
