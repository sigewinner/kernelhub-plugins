#!/usr/bin/env python3
"""ImageMagick 图像内核 适配器 · CKP 1.0。

本文件由 ``tools/new_kernel.py`` 生成，是**通用命令行桥接器**的入口：
引擎怎么调用全部写在同目录 ``kernel.json`` 的 ``x-cli`` 段里，
所以给这个内核加格式、加参数、换引擎，都只需要改 JSON。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_ROOT, "vendor"), _ROOT):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from kernelhub.cli_bridge import CliBridge  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(CliBridge(__file__).main())
