#!/usr/bin/env python3
"""Office 文档抽取内核 · CKP 1.0 适配器。

DOCX / XLSX / PPTX → 文本 / Markdown / HTML / CSV / JSON。
用 python-docx、openpyxl、python-pptx 读取，**不需要安装 Office 或 LibreOffice**。
"""

from __future__ import annotations

import csv
import html as htmllib
import io
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

KERNEL_ID = "office-text"
VERSION = "1.0.0"

DOC_FORMATS = {"docx", "docm"}
SHEET_FORMATS = {"xlsx", "xlsm"}
SLIDE_FORMATS = {"pptx", "pptm"}


def _require(module: str, package: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise CkpError(
            "DEPENDENCY_MISSING",
            f"未安装 {package}：python tools/pip_bootstrap.py install "
            f"--target vendor {package}", str(exc)) from exc


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #


def _docx_blocks(path: str):
    docx = _require("docx", "python-docx")
    try:
        document = docx.Document(path)
    except Exception as exc:  # noqa: BLE001
        raise CkpError("INPUT_UNREADABLE", f"无法打开 DOCX: {exc}", str(exc)) from exc

    blocks: list[tuple[str, str]] = []  # (kind, text)
    for para in document.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = (getattr(para.style, "name", "") or "").lower()
        if style.startswith("heading") or style.startswith("标题"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            blocks.append(("h" + (level if level in "123456" else "1"), text))
        elif style.startswith("list"):
            blocks.append(("li", text))
        else:
            blocks.append(("p", text))

    for index, table in enumerate(document.tables):
        rows = [[(cell.text or "").strip() for cell in row.cells] for row in table.rows]
        rows = [r for r in rows if any(r)]
        if rows:
            blocks.append(("table", json.dumps(rows, ensure_ascii=False)))
    return blocks


def _docx_to_markdown(blocks) -> str:
    out: list[str] = []
    for kind, text in blocks:
        if kind.startswith("h"):
            level = int(kind[1])
            out.append("#" * level + " " + text)
        elif kind == "li":
            out.append("- " + text)
        elif kind == "table":
            rows = json.loads(text)
            if rows:
                out.append("| " + " | ".join(str(c) for c in rows[0]) + " |")
                out.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                for row in rows[1:]:
                    out.append("| " + " | ".join(str(c) for c in row) + " |")
        else:
            out.append(text)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _docx_to_html(blocks, title: str) -> str:
    parts: list[str] = []
    for kind, text in blocks:
        escaped = htmllib.escape(text)
        if kind.startswith("h"):
            level = kind[1]
            parts.append(f"<h{level}>{escaped}</h{level}>")
        elif kind == "li":
            parts.append(f"<li>{escaped}</li>")
        elif kind == "table":
            rows = json.loads(text)
            body = "".join(
                "<tr>" + "".join(f"<td>{htmllib.escape(str(c))}</td>" for c in row) + "</tr>"
                for row in rows)
            parts.append(f"<table>{body}</table>")
        else:
            parts.append(f"<p>{escaped}</p>")
    return ("<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{htmllib.escape(title)}</title></head><body>\n"
            + "\n".join(parts) + "\n</body></html>\n")


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #


def _sheet_rows(path: str, sheet: str = ""):
    openpyxl = _require("openpyxl", "openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    rows = [[("" if c is None else str(c)) for c in row]
            for row in ws.iter_rows(values_only=True)]
    wb.close()
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    return ws.title, rows


# --------------------------------------------------------------------------- #
# PPTX
# --------------------------------------------------------------------------- #


def _pptx_slides(path: str):
    pptx = _require("pptx", "python-pptx")
    try:
        prs = pptx.Presentation(path)
    except Exception as exc:  # noqa: BLE001
        raise CkpError("INPUT_UNREADABLE", f"无法打开 PPTX: {exc}", str(exc)) from exc
    slides = []
    for index, slide in enumerate(prs.slides, 1):
        title = ""
        lines: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            text = (shape.text_frame.text or "").strip()
            if not text:
                continue
            if shape == slide.shapes.title:
                title = text
            else:
                lines.extend(ln for ln in text.splitlines() if ln.strip())
        notes = ""
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:  # noqa: BLE001
            pass
        slides.append({"index": index, "title": title, "lines": lines, "notes": notes})
    return slides


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    notes = []
    for module, package in (("docx", "python-docx"), ("openpyxl", "openpyxl"),
                            ("pptx", "python-pptx")):
        try:
            __import__(module)
            notes.append(package)
        except ImportError:
            pass
    app = KernelApp(KERNEL_ID, VERSION, engine=" + ".join(notes) or "（无引擎）")

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
        ensure_parent(dst)

        if not os.path.isfile(src):
            raise CkpError("INPUT_NOT_FOUND", f"输入文件不存在: {src}")

        title = str(params.get("title") or os.path.splitext(os.path.basename(src))[0])
        _app.progress(0.2, "解析文档")

        if src_fmt in DOC_FORMATS:
            blocks = _docx_blocks(src)
            _app.log(f"{len(blocks)} 个段落/表格")
            if dst_fmt in ("md", "markdown"):
                payload = _docx_to_markdown(blocks)
            elif dst_fmt in ("html", "htm"):
                payload = _docx_to_html(blocks, title)
            elif dst_fmt in ("json",):
                payload = json.dumps(
                    [{"kind": k, "text": t} for k, t in blocks],
                    ensure_ascii=False, indent=2) + "\n"
            else:  # text / txt
                payload = "\n".join(t for k, t in blocks if k != "table") + "\n"
                for k, t in blocks:
                    if k == "table":
                        payload += "\n" + "\n".join(
                            "\t".join(r) for r in json.loads(t)) + "\n"

        elif src_fmt in SHEET_FORMATS:
            sheet_name, rows = _sheet_rows(src, str(params.get("sheet") or ""))
            _app.log(f"工作表「{sheet_name}」{len(rows)} 行")
            if dst_fmt == "csv":
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\n").writerows(rows)
                payload = buf.getvalue()
            elif dst_fmt == "json":
                header = rows[0] if rows else []
                payload = json.dumps(
                    [dict(zip(header, row)) for row in rows[1:]] if header else rows,
                    ensure_ascii=False, indent=2) + "\n"
            elif dst_fmt in ("md", "markdown"):
                header = rows[0] if rows else []
                lines = ["| " + " | ".join(header) + " |",
                         "| " + " | ".join("---" for _ in header) + " |"]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                payload = "\n".join(lines) + "\n"
            elif dst_fmt in ("html", "htm"):
                body = "".join("<tr>" + "".join(
                    f"<td>{htmllib.escape(c)}</td>" for c in row) + "</tr>" for row in rows)
                payload = ("<!doctype html>\n<html lang=\"zh-CN\"><head>"
                           "<meta charset=\"utf-8\">"
                           f"<title>{htmllib.escape(title)}</title></head><body>"
                           f"<table>{body}</table></body></html>\n")
            else:
                payload = "\n".join("\t".join(r) for r in rows) + "\n"

        elif src_fmt in SLIDE_FORMATS:
            slides = _pptx_slides(src)
            _app.log(f"{len(slides)} 页幻灯片")
            if dst_fmt in ("md", "markdown"):
                parts = []
                for slide in slides:
                    heading = slide["title"] or f"第 {slide['index']} 页"
                    parts.append(f"## {heading}")
                    parts.extend(slide["lines"])
                    if slide.get("notes"):
                        parts.append(f"> 备注：{slide['notes']}")
                    parts.append("")
                payload = "\n".join(parts)
            elif dst_fmt in ("html", "htm"):
                parts = []
                for slide in slides:
                    parts.append(f"<h2>{htmllib.escape(slide['title'] or str(slide['index']))}</h2><ul>")
                    parts.extend(f"<li>{htmllib.escape(x)}</li>" for x in slide["lines"])
                    parts.append("</ul>")
                payload = ("<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                           f"<title>{htmllib.escape(title)}</title></head><body>\n"
                           + "\n".join(parts) + "\n</body></html>\n")
            elif dst_fmt == "json":
                payload = json.dumps(slides, ensure_ascii=False, indent=2) + "\n"
            else:
                parts = []
                for slide in slides:
                    parts.append(f"--- 第 {slide['index']} 页 ---")
                    if slide["title"]:
                        parts.append(slide["title"])
                    parts.extend(slide["lines"])
                    parts.append("")
                payload = "\n".join(parts)
        else:
            raise CkpError("UNSUPPORTED_FORMAT",
                           f"Office 抽取内核不支持读取 {src_fmt}"
                           "（支持 docx / xlsx / pptx）")

        _app.progress(0.85, "写出")
        with open(dst, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        meta = {"from": src_fmt, "to": dst_fmt, "chars": len(payload)}
        return [emit_artifact(dst, dst_fmt, primary=True, meta=meta)]

    return app.run(handler, argv)


if __name__ == "__main__":
    raise SystemExit(main())
