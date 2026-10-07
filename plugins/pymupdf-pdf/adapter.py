#!/usr/bin/env python3
"""PyMuPDF (MuPDF) PDF 内核 · CKP 1.0 适配器。

能力：render（PDF→位图）/ extract（抽文本、抽内嵌图）/ convert（图片→PDF）/
merge（多 PDF 合并）/ split（PDF 拆分）/ optimize（瘦身）/ inspect（元信息）

引擎：PyMuPDF —— MuPDF 的 Python 绑定，速度快、无外部依赖。
"""

from __future__ import annotations

import json
import os
import sys

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

KERNEL_ID = "pymupdf-pdf"
VERSION = "1.1.0"

RASTER_FORMATS = {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "ppm", "pgm", "pnm", "psd"}
IMAGE_INPUTS = {"png", "jpg", "jpeg", "bmp", "gif", "tif", "tiff", "webp", "avif", "jfif", "pnm", "ppm"}


def _load_engine():
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 旧版本兼容
        except ImportError as exc:  # pragma: no cover
            raise CkpError(
                "DEPENDENCY_MISSING",
                "未安装 PyMuPDF，请运行：python tools/pip_bootstrap.py install "
                "--target vendor PyMuPDF", str(exc)) from exc
    version = getattr(pymupdf, "version", None)
    if isinstance(version, (tuple, list)) and version:
        note = f"PyMuPDF {version[0]}"
    else:
        note = "PyMuPDF"
    return pymupdf, note


def _open_pdf(pymupdf, path):
    if not os.path.isfile(path):
        raise CkpError("INPUT_NOT_FOUND", f"输入文件不存在: {path}")
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001
        raise CkpError("INPUT_UNREADABLE", f"无法打开 PDF: {exc}", str(exc)) from exc
    if doc.needs_pass:
        raise CkpError("INPUT_UNREADABLE", "PDF 已加密，需要密码")
    return doc


def _page_range(doc, params):
    total = doc.page_count
    first = int(params.get("first_page") or 1)
    last = int(params.get("last_page") or total)
    first = max(1, min(first, total))
    last = max(first, min(last, total))
    return first, last


def _render(job, app, pymupdf, note):
    params = dict(job.get("params") or {})
    src = job["inputs"][0]["path"]
    dst = job["outputs"][0]["path"]
    out_fmt = canonical_format(job["outputs"][0].get("format") or format_of_path(dst))
    if out_fmt not in RASTER_FORMATS:
        raise CkpError("UNSUPPORTED_FORMAT", f"PyMuPDF 渲染不支持输出 {out_fmt}")

    dpi = int(params.get("dpi") or 150)
    quality = int(params.get("quality") or 85)
    doc = _open_pdf(pymupdf, src)
    first, last = _page_range(doc, params)
    count = last - first + 1

    out_dir = os.path.dirname(dst) or "."
    stem = os.path.splitext(os.path.basename(dst))[0]
    ensure_parent(dst)

    produces_all = count > 1
    results: list[dict] = []
    for idx, page_no in enumerate(range(first, last + 1)):
        page = doc.load_page(page_no - 1)
        pix = page.get_pixmap(dpi=dpi, alpha=(out_fmt == "png"))
        target = dst if not produces_all else os.path.join(
            out_dir, f"{stem}_{page_no:03d}.{out_fmt}")
        ensure_parent(target)

        if out_fmt == "png":
            pix.save(target)
        else:
            # PyMuPDF 只能直接写 png/ppm 等；其它格式经 Pillow 转一道
            try:
                from io import BytesIO

                from PIL import Image

                img = Image.open(BytesIO(pix.tobytes("png")))
                if out_fmt in ("jpg", "jpeg") and img.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    rgba = img.convert("RGBA")
                    bg.paste(rgba, mask=rgba.split()[-1])
                    img = bg
                save_kw = {"quality": quality} if out_fmt in ("jpg", "jpeg", "webp") else {}
                img.save(target, **save_kw)
            except ImportError as exc:
                raise CkpError("DEPENDENCY_MISSING",
                               f"输出 {out_fmt} 需要 Pillow 做二次编码", str(exc)) from exc
            except OSError as exc:
                raise CkpError("INTERNAL", f"写出 {out_fmt} 失败: {exc}", str(exc)) from exc

        meta = {"width": pix.width, "height": pix.height, "page": page_no, "dpi": dpi,
                "engine": note}
        results.append(emit_artifact(target, out_fmt, primary=(idx == 0), meta=meta))
        app.progress((idx + 1) / count, f"第 {page_no} 页")

    app.log(f"共渲染 {count} 页 @ {dpi}dpi")
    doc.close()
    return results


def _extract_text(job, app, pymupdf, note):
    params = dict(job.get("params") or {})
    src = job["inputs"][0]["path"]
    dst = job["outputs"][0]["path"]
    out_fmt = canonical_format(job["outputs"][0].get("format") or format_of_path(dst))

    doc = _open_pdf(pymupdf, src)
    first, last = _page_range(doc, params)
    mode = {"text": "text", "txt": "text", "md": "text", "markdown": "text",
            "html": "html", "htm": "html", "json": "json",
            "xml": "xml"}.get(out_fmt, "text")

    chunks: list[str] = []
    for page_no in range(first, last + 1):
        page = doc.load_page(page_no - 1)
        if mode == "html":
            chunks.append(page.get_text("html"))
        elif mode == "json":
            chunks.append(json.dumps(page.get_text("dict"),
                                     ensure_ascii=False, default=str))
        elif mode == "xml":
            chunks.append(page.get_text("xml"))
        else:
            text = page.get_text("text")
            if out_fmt in ("md", "markdown"):
                text = f"## 第 {page_no} 页\n\n{text.strip()}\n"
            chunks.append(text)

    if mode == "json":
        payload = "[\n" + ",\n".join(chunks) + "\n]\n"
    elif mode == "text":
        payload = "\n\n".join(chunks)
    else:
        payload = "\n".join(chunks)

    ensure_parent(dst)
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        fh.write(payload)

    meta = {"pages": last - first + 1, "chars": len(payload), "engine": note}
    app.log(f"抽取 {meta['pages']} 页，{meta['chars']} 字符")
    doc.close()
    return [emit_artifact(dst, out_fmt, primary=True, meta=meta)]


def _extract_images(job, app, pymupdf, note):
    """抽取 PDF 中内嵌的图片。"""
    src = job["inputs"][0]["path"]
    dst = job["outputs"][0]["path"]
    doc = _open_pdf(pymupdf, src)
    out_dir = os.path.dirname(dst) or "."
    stem = os.path.splitext(os.path.basename(dst))[0]
    ensure_parent(dst)

    results: list[dict] = []
    seen: set[int] = set()
    for page_no in range(doc.page_count):
        page = doc.load_page(page_no)
        for img_index, info in enumerate(page.get_images(full=True)):
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                data = doc.extract_image(xref)
            except Exception:  # noqa: BLE001
                continue
            ext = str(data.get("ext") or "png").lower()
            target = os.path.join(out_dir, f"{stem}_p{page_no + 1}_{img_index + 1}.{ext}")
            ensure_parent(target)
            with open(target, "wb") as fh:
                fh.write(data["image"])
            results.append(emit_artifact(target, ext, primary=not results,
                                         meta={"width": data.get("width"),
                                               "height": data.get("height"),
                                               "page": page_no + 1, "engine": note}))
    doc.close()
    if not results:
        raise CkpError("INPUT_UNREADABLE", "该 PDF 中没有可抽取的内嵌图片")
    app.log(f"抽出 {len(results)} 张内嵌图片")
    return results


def _images_to_pdf(job, app, pymupdf, note):
    sources = [i["path"] for i in job["inputs"]]
    dst = job["outputs"][0]["path"]
    params = dict(job.get("params") or {})
    ensure_parent(dst)

    from io import BytesIO

    from PIL import Image

    pages = []
    for path in sources:
        try:
            with Image.open(path) as img:
                if img.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    rgba = img.convert("RGBA")
                    bg.paste(rgba, mask=rgba.split()[-1])
                    img = bg
                else:
                    img = img.convert("RGB")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=int(params.get("quality") or 90))
                pages.append((buf.getvalue(), img.width, img.height))
        except Exception as exc:  # noqa: BLE001
            raise CkpError("INPUT_UNREADABLE", f"无法读取图片 {path}: {exc}", str(exc)) from exc

    out = pymupdf.open()
    for data, width, height in pages:
        page = out.new_page(width=width, height=height)
        page.insert_image(pymupdf.Rect(0, 0, width, height), stream=data)
    out.save(dst, deflate=True, garbage=3)
    out.close()
    app.log(f"{len(pages)} 张图片合成 PDF")
    return [emit_artifact(dst, "pdf", primary=True,
                          meta={"pages": len(pages), "engine": note})]


def _merge(job, app, pymupdf, note):
    sources = [i["path"] for i in job["inputs"]]
    dst = job["outputs"][0]["path"]
    ensure_parent(dst)
    out = pymupdf.open()
    total = 0
    for path in sources:
        doc = _open_pdf(pymupdf, path)
        out.insert_pdf(doc)
        total += doc.page_count
        doc.close()
    out.save(dst, deflate=True, garbage=3)
    out.close()
    app.log(f"合并 {len(sources)} 个 PDF，共 {total} 页")
    return [emit_artifact(dst, "pdf", primary=True,
                          meta={"pages": total, "sources": len(sources), "engine": note})]


def _split(job, app, pymupdf, note):
    src = job["inputs"][0]["path"]
    dst = job["outputs"][0]["path"]
    params = dict(job.get("params") or {})
    per_file = max(1, int(params.get("pages_per_file") or 1))
    doc = _open_pdf(pymupdf, src)
    out_dir = os.path.dirname(dst) or "."
    stem = os.path.splitext(os.path.basename(dst))[0]
    ensure_parent(dst)

    results: list[dict] = []
    total = doc.page_count
    index = 1
    for start in range(0, total, per_file):
        part = pymupdf.open()
        part.insert_pdf(doc, from_page=start, to_page=min(start + per_file, total) - 1)
        target = os.path.join(out_dir, f"{stem}_{index:03d}.pdf")
        part.save(target, deflate=True, garbage=3)
        part.close()
        results.append(emit_artifact(target, "pdf", primary=(index == 1),
                                     meta={"pages": min(per_file, total - start),
                                           "engine": note}))
        app.progress(index * per_file / total, f"第 {index} 部分")
        index += 1
    doc.close()
    app.log(f"拆分为 {len(results)} 个文件")
    return results


def _optimize(job, app, pymupdf, note):
    src = job["inputs"][0]["path"]
    dst = job["outputs"][0]["path"]
    ensure_parent(dst)
    doc = _open_pdf(pymupdf, src)
    before = os.path.getsize(src)
    doc.save(dst, garbage=4, deflate=True, deflate_images=True,
             deflate_fonts=True, clean=True)
    doc.close()
    after = os.path.getsize(dst)
    meta = {"bytes_before": before, "bytes_after": after,
            "saved_percent": round((1 - after / max(1, before)) * 100, 1),
            "engine": note}
    app.log(f"{before:,}B -> {after:,}B（省 {meta['saved_percent']}%）")
    return [emit_artifact(dst, "pdf", primary=True, meta=meta)]


def _inspect(job, app, pymupdf, note):
    src = job["inputs"][0]["path"]
    doc = _open_pdf(pymupdf, src)
    meta = {
        "path": src,
        "pages": doc.page_count,
        "encrypted": bool(doc.needs_pass),
        "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
        "engine": note,
    }
    try:
        page = doc.load_page(0)
        rect = page.rect
        meta["page_size_pt"] = [round(rect.width, 2), round(rect.height, 2)]
        meta["rotation"] = page.rotation
    except Exception:  # noqa: BLE001
        pass
    try:
        meta["toc"] = len(doc.get_toc() or [])
    except Exception:  # noqa: BLE001
        pass
    doc.close()

    app.log(json.dumps(meta, ensure_ascii=False))
    app.extra_metrics.update(meta)
    outputs = job.get("outputs") or []
    if outputs and canonical_format(outputs[0].get("format")) == "json":
        dst = outputs[0]["path"]
        ensure_parent(dst)
        with open(dst, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        return [emit_artifact(dst, "json", primary=True)]
    return []


def main(argv=None) -> int:
    pymupdf, note = _load_engine()
    app = KernelApp(KERNEL_ID, VERSION, engine=note)

    def handler(job, _app):
        op = str(job.get("op", "convert"))
        inputs = job.get("inputs") or []
        src_fmt = canonical_format(inputs[0].get("format")
                                   or format_of_path(inputs[0].get("path", ""))) if inputs else ""

        if op == "render":
            return _render(job, _app, pymupdf, note)
        if op == "extract":
            dst_fmt = canonical_format((job.get("outputs") or [{}])[0].get("format")
                                       or format_of_path((job.get("outputs") or [{}])[0].get("path", "")))
            if dst_fmt in RASTER_FORMATS:
                return _extract_images(job, _app, pymupdf, note)
            return _extract_text(job, _app, pymupdf, note)
        if op == "merge":
            return _merge(job, _app, pymupdf, note)
        if op == "split":
            return _split(job, _app, pymupdf, note)
        if op == "optimize":
            return _optimize(job, _app, pymupdf, note)
        if op == "inspect":
            return _inspect(job, _app, pymupdf, note)
        if op == "convert":
            if src_fmt in IMAGE_INPUTS:
                return _images_to_pdf(job, _app, pymupdf, note)
            if src_fmt == "pdf":
                return _optimize(job, _app, pymupdf, note)
        raise CkpError("UNSUPPORTED_FORMAT",
                       f"PyMuPDF 内核不支持 {src_fmt} 上的操作 {op}")

    return app.run(handler, argv)


if __name__ == "__main__":
    raise SystemExit(main())
