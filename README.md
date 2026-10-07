# KernelHub 插件仓库

[KernelHub Studio](https://github.com/sigewinner/kernelhub) 的**内核插件**集中托管仓库。

应用本体（2.0.0 起）只发一个**不含任何内核的壳**，插件按需从这里下载安装——
所以只想转个图片的用户不必下载 200 MB 的 Office/PDF 依赖。

---

## 这个仓库里有什么

```
kernelhub-plugins/
├── catalog.json                 ← 插件目录清单（应用读它渲染「插件」页）
├── docs/
│   ├── PROTOCOL.md              ← CKP 协议规范（插件作者必读）
│   └── schemas/*.json           ← 协议 JSON Schema
└── plugins/
    ├── pillow-image/
    │   ├── kernel.json          ← 内核清单（能力、参数、引擎声明）
    │   ├── adapter.py           ← 适配器入口
    │   └── vendor/              ← 该插件专属的 Python 依赖（可选）
    ├── imagemagick/
    │   ├── kernel.json
    │   └── adapter.py           ← 无 vendor：调用用户自己装的 magick.exe
    └── ...
```

**每个插件都是自包含的**：`plugins/<id>/vendor/` 只装这个插件需要的 Python 包。
宿主的 CKP 运行器会把该目录注入 `PYTHONPATH`，所以插件之间互不干扰，
也不会因为别的插件没装而无法工作。

> `kernelhub` 这个 Python 包（`kernelhub.sdk` / `kernelhub.cli_bridge`）**不在这里**——
> 它是 CKP 的适配器运行时 SDK，由应用本体随包提供。

---

## 下载量一览

应用会按需只拉你勾选的那几个插件：

| 插件 | 版本 | 下载量 | 依赖来源 |
|---|---|---|---|
| `ffmpeg-media` | 1.1.0 | 83.62 MB | 自带 ffmpeg（imageio-ffmpeg wheel） |
| `svg-vector` | 1.0.0 | 76.13 MB | svglib + reportlab + lxml + PyMuPDF + Pillow |
| `pymupdf-pdf` | 1.1.0 | 62.13 MB | PyMuPDF + Pillow |
| `pillow-image` | 1.2.0 | 41.58 MB | Pillow + pillow-heif |
| `office-text` | 1.0.0 | 27.29 MB | python-docx / openpyxl / python-pptx（含 lxml、Pillow） |
| `data-table` | 1.0.0 | 0.89 MB | openpyxl |
| `text-markup` | 1.0.0 | 0.43 MB | markdown + html2text |
| `stdlib-image` | 1.1.0 | 0 | 纯 Python 标准库 |
| `windows-wic` | 1.0.0 | 0 | Windows 自带 PowerShell + WIC |
| `imagemagick` / `graphicsmagick` / `libvips-image` / `inkscape-vector` / `ghostscript-pdf` / `poppler-pdf` / `qpdf-pdf` / `sevenzip-archive` / `pandoc-document` / `libreoffice-office` | 1.0.0 | ~0 | 只需几 KB 代码，引擎是**你自己装的外部程序** |

「依赖来源」是外部程序的那 10 个插件，装完还需要系统 `PATH` 里有对应工具
（应用会在「内核」页逐个探测并告诉你缺什么）。

---

## 安装方式

### 推荐：在应用里装

打开 KernelHub Studio →「**插件**」页 → 勾选要装的 → 安装。
应用会自动选择下载方式（见下）。

### 手动：git sparse checkout

只想拿某一个插件的文件、不启动应用时：

```bat
git clone --filter=blob:none --sparse --depth 1 https://github.com/sigewinner/kernelhub-plugins.git
cd kernelhub-plugins
git sparse-checkout set plugins/pillow-image
```

`--filter=blob:none --sparse` 是关键：它只会下载你 `set` 进去的那个目录，
不会把另外 18 个插件的 290 MB 一起拖下来。

然后把这个目录整个拷到应用的插件目录即可：

* 安装版：`%APPDATA%\kernelhub-studio\hub\plugins\`
* 或任意目录 + 环境变量 `CKP_PLUGIN_PATH` 指向它

---

## catalog.json

应用通过它知道「有哪些插件、多大、装的是什么版本」：

```json
{
  "schema": 1,
  "ckp": "1.0",
  "app": ">=2.0.0",
  "updated": "2026-10-07",
  "pluginCount": 19,
  "totalSize": 306468000,
  "plugins": [
    {
      "id": "pillow-image",
      "name": "Pillow 图像内核",
      "version": "1.2.0",
      "ckp": "1.0",
      "kind": "image",
      "runtime": "python",
      "requires": ["PIL"],
      "external": [],
      "capabilities": 4,
      "ops": ["convert", "optimize", "inspect", "transform"],
      "path": "plugins/pillow-image",
      "size": 43623456,
      "files": 152,
      "vendorSize": 43597824,
      "sha256": "…"
    }
  ]
}
```

* `size` / `files` —— 整个插件目录，用来在界面上显示「要下载多少」
* `vendorSize` —— 其中有多少是 Python 依赖
* `sha256` —— **内容树哈希**：把文件按相对路径排序后，对 `路径\0文件哈希\n` 逐行做 SHA-256。
  与遍历顺序、时间戳无关，应用装完可以直接校验
* `external` —— 需要用户自己准备的外部程序

重新生成（改完插件后必做）：

```bat
node tools/make-catalog.js
```

---

## 怎么写一个新插件

最小插件只有两个文件：

```
plugins/my-kernel/
├── kernel.json     ← 声明能力：op、from/to 格式、参数、探测方式、引擎
└── adapter.py      ← 从 --ckp-job 读任务，往 stdout 吐 NDJSON 事件
```

如果引擎是现成的命令行程序，**连 adapter.py 都不用写**——
`kernel.json` 里的 `x-cli` 段就能把它桥接进来，参考 `plugins/imagemagick/`。

需要用 Python 库时：

```bat
python -m pip install --target plugins/my-kernel/vendor 你要的包
node tools/make-catalog.js
```

规范细节见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)，以及应用里的 `tools/new_kernel.py` 脚手架。

---

## 来源与许可

本仓库的插件代码与 `vendor/` 内容拆分自
[kernel-hub](https://github.com/sigewinner/kernelhub) 的单体 `vendor/` 目录，
插件源码**一个字节都没有改动**——只把每个插件真正用到的 Python 发行版
搬到了它自己的 `vendor/` 下（依赖关系由各发行版的 `METADATA`/`Requires-Dist`
与插件源码的实际引用共同确定）。

各插件清单里的 `license` / `homepage` 字段标注了它封装的上游项目。
`vendor/` 内的第三方包版权归各自作者所有，随包保留了对应的 `*.dist-info` 元数据。
