#!/usr/bin/env python3
"""SVG 矢量内核 · CKP 1.0 适配器。

SVG → PDF / PNG / JPG，纯 Python 实现（svglib + reportlab，PNG 由 PyMuPDF 兜底渲染）。

注意：svglib 只支持 SVG 1.1 的核心子集（不支持 CSS 样式表、滤镜、动画）。
若机器上装了 Inkscape / libvips，KernelHub 会优先选用它们（优先级更高）。
"""

from __future__ import annotations

import io
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

KERNEL_ID = "svg-vector"
VERSION = "1.0.0"


def _load_engine():
    try:
        from svglib.svglib import svg2rlg
    except ImportError as exc:
        raise CkpError(
            "DEPENDENCY_MISSING",
            "未安装 svglib：python tools/pip_bootstrap.py install --target vendor svglib reportlab",
            str(exc)) from exc
    notes = ["svglib"]
    try:
        import reportlab

        notes.append(f"reportlab {getattr(reportlab, 'Version', '?')}")
    except ImportError:
        pass
    return svg2rlg, " + ".join(notes)


def _read_svg(path: str) -> bytes:
    if not os.path.isfile(path):
        raise CkpError("INPUT_NOT_FOUND", f"输入文件不存在: {path}")
    with open(path, "rb") as fh:
        return fh.read()


def _svg_to_pdf_bytes(svg2rlg, svg_bytes: bytes, params: dict) -> bytes:
    from reportlab.graphics import renderPDF

    drawing = _drawing_from_bytes(svg2rlg, svg_bytes, params)
    buf = io.BytesIO()
    renderPDF.drawToFile(drawing, buf, autoSize=1)
    return buf.getvalue()


def _drawing_from_bytes(svg2rlg, svg_bytes: bytes, params: dict):
    scale = float(params.get("scale") or 1.0)
    stream = io.BytesIO(svg_bytes)
    try:
        drawing = svg2rlg(stream)
    except Exception as exc:  # noqa: BLE001
        raise CkpError("INPUT_UNREADABLE", f"SVG 解析失败: {exc}", str(exc)) from exc
    if drawing is None:
        raise CkpError("INPUT_UNREADABLE", "SVG 解析结果为空（文件可能不是合法 SVG）")
    if scale != 1.0:
        drawing.scale(scale, scale)
        drawing.width *= scale
        drawing.height *= scale
    return drawing


def _drawing_to_png(svg2rlg, svg_bytes: bytes, params: dict) -> tuple[bytes, int, int]:
    """优先用 svglib 自己的 renderPM；不行就退回到 reportlab PDF + PyMuPDF 渲染。"""
    drawing = _drawing_from_bytes(svg2rlg, svg_bytes, params)
    dpi = int(params.get("dpi") or 96)

    # 路线 A：rlPyCairo / renderPM
    try:
        from reportlab.graphics import renderPM

        buf = io.BytesIO()
        renderPM.drawToFile(drawing, buf, fmt="PNG", dpi=dpi)
        data = buf.getvalue()
        if data:
            return data, drawing.width, drawing.height
    except Exception:  # noqa: BLE001
        pass

    # 路线 B：PDF 中转 + PyMuPDF 光栅化（两个库都随项目安装）
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise CkpError(
                "DEPENDENCY_MISSING",
                "SVG 转位图需要 reportlab 的 renderPM 或 PyMuPDF 之一；"
                "请安装 PyMuPDF：python tools/pip_bootstrap.py install --target vendor PyMuPDF",
                str(exc)) from exc

    pdf_bytes = _svg_to_pdf_bytes(svg2rlg, svg_bytes, params)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc.load_page(0)
    pix = page.get_pixmap(dpi=dpi, alpha=bool(params.get("transparent", True)))
    data = pix.tobytes("png")
    dims = (pix.width, pix.height)
    doc.close()
    return data, dims[0], dims[1]


def main(argv=None) -> int:
    svg2rlg, engine = _load_engine()
    app = KernelApp(KERNEL_ID, VERSION, engine=engine)

    def handler(job, _app):
        inputs = job.get("inputs") or []
        outputs = job.get("outputs") or []
        if not inputs or not outputs:
            raise CkpError("BAD_JOB", "需要一个输入与一个输出")

        src = inputs[0]["path"]
        dst = outputs[0]["path"]
        src_fmt = canonical_format(inputs[0].get("format") or format_of_path(src))
        dst_fmt = canonical_format(outputs[0].get("format") or format_of_path(dst))
        params = dict(job.get("params") or {})

        if src_fmt not in ("svg", "svgz", "xml"):
            raise CkpError("UNSUPPORTED_FORMAT", f"SVG 内核只接受 svg/svgz，收到 {src_fmt}")

        _app.progress(0.25, "读取 SVG")
        svg_bytes = _read_svg(src)

        ensure_parent(dst)
        _app.progress(0.6, f"渲染为 {dst_fmt}")

        if dst_fmt == "pdf":
            payload = _svg_to_pdf_bytes(svg2rlg, svg_bytes, params)
            with open(dst, "wb") as fh:
                fh.write(payload)
            meta = {"engine": engine, "bytes": len(payload), "format": "pdf"}
        elif dst_fmt in ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"):
            data, width, height = _drawing_to_png(svg2rlg, svg_bytes, params)
            if dst_fmt == "png":
                with open(dst, "wb") as fh:
                    fh.write(data)
            else:
                try:
                    from PIL import Image
                except ImportError as exc:
                    raise CkpError("DEPENDENCY_MISSING",
                                   f"输出 {dst_fmt} 需要 Pillow", str(exc)) from exc
                img = Image.open(io.BytesIO(data))
                if dst_fmt in ("jpg", "jpeg") and img.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    rgba = img.convert("RGBA")
                    bg.paste(rgba, mask=rgba.split()[-1])
                    img = bg
                img.save(dst, quality=int(params.get("quality") or 90))
            meta = {"engine": engine, "width": width, "height": height,
                    "dpi": int(params.get("dpi") or 96)}
        elif dst_fmt == "svg":
            with open(dst, "wb") as fh:
                fh.write(svg_bytes)
            meta = {"engine": engine, "note": "原样复制"}
        else:
            raise CkpError("UNSUPPORTED_FORMAT",
                           f"SVG 内核无法输出 {dst_fmt}（支持 pdf/png/jpg/webp/bmp/tiff/svg）")

        return [emit_artifact(dst, dst_fmt, primary=True, meta=meta)]

    return app.run(handler, argv)


if __name__ == "__main__":
    raise SystemExit(main())
