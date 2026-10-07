'use strict';
/**
 * vendor 切分分析 v3（权威版）。
 *
 * 修正 v2 的两个 bug：
 *   - dist 键把版本号也带进去了（et_xmlfile_2_0_0），而 Requires-Dist 是裸名，
 *     导致依赖图一条都没连上。现在按 dist-info 目录名拆出 name/version。
 *   - 只扫 `import X`，漏掉 adapter 里 `_require("docx", "python-docx")` 这种
 *     带引号的动态导入。现在额外匹配 _require / import_module / __import__。
 *
 * kernelhub 不计入插件依赖：它是 CKP 适配器 SDK，由壳提供。
 */
const fs = require('fs');
const path = require('path');

const VENDOR = process.argv[2];
const PLUGINS = process.argv[3];

const normName = (n) => n.trim().toLowerCase().replace(/[-.]+/g, '_');

const STDLIB = new Set(`abc argparse array ast asyncio base64 bdb binascii bisect builtins bz2 calendar cmath cmd code codecs
collections colorsys compileall concurrent configparser contextlib copy copyreg cProfile csv ctypes dataclasses datetime
dbm decimal difflib dis doctest email encodings enum errno faulthandler fcntl filecmp fileinput fnmatch fractions
ftplib functools gc getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html http idlelib imaplib imghdr
importlib inspect io ipaddress itertools json keyword linecache locale logging lzma mailbox marshal math mimetypes mmap
multiprocessing netrc numbers operator optparse os pathlib pdb pickle pickletools pkgutil platform plistlib poplib posixpath
pprint profile pstats pty pwd py_compile pyclbr pydoc queue quopri random re readline reprlib resource rlcompleter runpy
sched secrets select selectors shelve shlex shutil signal site smtplib socket socketserver sqlite3 ssl stat statistics string
stringprep struct subprocess symtable sys sysconfig syslog tabnanny tarfile tempfile termios textwrap threading time timeit
tkinter token tokenize tomllib trace traceback tracemalloc tty turtle types typing unicodedata unittest urllib uuid venv
warnings wave weakref webbrowser winreg winsound wsgiref xml xmlrpc zipapp zipfile zipimport zlib __future__`.split(/\s+/).filter(Boolean));

function walk(dir, base = dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, base, out);
    else out.push(path.relative(base, p).replace(/\\/g, '/'));
  }
  return out;
}

/* ---------- 发行版元数据 ---------- */
const dists = new Map();
for (const d of fs.readdirSync(VENDOR)) {
  if (!d.endsWith('.dist-info')) continue;
  const full = d.replace(/\.dist-info$/, '');
  const m = full.match(/^(.*?)-(\d[^-]*)$/);
  const name = m ? m[1] : full;
  const version = m ? m[2] : '';
  const key = normName(name);

  const rec = { key, name, version, fullName: full, requires: [], files: [] };
  const meta = path.join(VENDOR, d, 'METADATA');
  if (fs.existsSync(meta)) {
    for (const line of fs.readFileSync(meta, 'utf8').split(/\r?\n/)) {
      const mm = line.match(/^Requires-Dist:\s*(.+)$/i);
      if (!mm) continue;
      const semi = mm[1].indexOf(';');
      const marker = semi >= 0 ? mm[1].slice(semi + 1) : '';
      if (/extra\s*==/i.test(marker)) continue;
      const raw = mm[1].slice(0, semi >= 0 ? semi : mm[1].length);
      const depName = raw.split(/[\s(<>=!~;[]/)[0].trim();
      if (depName) rec.requires.push(normName(depName));
    }
  }
  const record = path.join(VENDOR, d, 'RECORD');
  if (fs.existsSync(record)) {
    for (const line of fs.readFileSync(record, 'utf8').split(/\r?\n/)) {
      if (!line.trim()) continue;
      const mm = line.match(/^"([^"]+)"|^([^,]+)/);
      const rel = (mm[1] || mm[2] || '').trim().replace(/\\/g, '/');
      if (rel && !rel.startsWith('..')) rec.files.push(rel);
    }
  }
  dists.set(key, rec);
}

/* ---------- 模块 -> 发行版 ---------- */
const moduleToDist = new Map();
for (const [key, rec] of dists) {
  for (const f of rec.files) {
    const top = f.split('/')[0];
    if (!top || top.endsWith('.dist-info')) continue;
    if (top.endsWith('.py')) {
      const mod = top.replace(/\.py$/, '');
      if (!moduleToDist.has(mod)) moduleToDist.set(mod, key);
    } else if (!top.includes('.')) {
      if (!moduleToDist.has(top)) moduleToDist.set(top, key);
    }
  }
}
const ALIASES = { PIL: 'pillow', fitz: 'pymupdf', pymupdf: 'pymupdf', docx: 'python_docx', pptx: 'python_pptx' };
for (const [mod, key] of Object.entries(ALIASES)) if (dists.has(key)) moduleToDist.set(mod, key);

/* ---------- 直接依赖 ---------- */
const IMPORT_RE = /^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*))/gm;
const QUOTED_RE = /(?:_require|import_module|__import__)\s*\(\s*["']([A-Za-z_][\w.]*)["']/g;

function scanPlugin(dir) {
  const direct = new Map(); // module -> where
  const local = new Set();
  const files = walk(dir);
  for (const f of files) if (f.endsWith('.py')) local.add(path.basename(f, '.py'));

  const note = (mod, where) => { if (mod && !direct.has(mod)) direct.set(mod, where); };

  for (const f of files) {
    const abs = path.join(dir, f);
    if (f.endsWith('.py')) {
      const src = fs.readFileSync(abs, 'utf8');
      IMPORT_RE.lastIndex = 0;
      let m;
      while ((m = IMPORT_RE.exec(src))) {
        const part = m[1] || m[2] || '';
        for (const chunk of part.split(',')) {
          const name = chunk.trim().split(/\s+as\s+/)[0].trim().split('.')[0];
          note(name, `${f}: import`);
        }
      }
      QUOTED_RE.lastIndex = 0;
      while ((m = QUOTED_RE.exec(src))) note(m[1].split('.')[0], `${f}: 动态导入 "${m[1]}"`);
    } else if (f.endsWith('.json')) {
      let data;
      try { data = JSON.parse(fs.readFileSync(abs, 'utf8').replace(/^\uFEFF/, '')); } catch { continue; }
      for (const r of (data.runtime && data.runtime.requires) || []) note(String(r), `${f}: runtime.requires`);
      const exes = (data['x-cli'] || data.xCli || {}).executables || {};
      for (const [nm, spec] of Object.entries(exes)) {
        if (spec && typeof spec === 'object') {
          if (spec.python) note(String(spec.python).split(':')[0], `${f}: x-cli.executables.${nm}.python`);
          for (const b of spec.bundled || []) {
            const mm = String(b).match(/vendor\/([A-Za-z_]\w*)/);
            if (mm) note(mm[1], `${f}: x-cli.executables.${nm}.bundled`);
          }
        }
      }
      if (data.probe && data.probe.type === 'python-import' && data.probe.target) {
        note(String(data.probe.target), `${f}: probe.python-import`);
      }
    }
  }
  return { direct, local, files };
}

function distClosure(seeds) {
  const seen = new Set();
  const parent = new Map();
  const stack = [...seeds];
  while (stack.length) {
    const k = stack.pop();
    if (seen.has(k) || !dists.has(k)) continue;
    seen.add(k);
    for (const dep of dists.get(k).requires) {
      if (!seen.has(dep)) { if (!parent.has(dep)) parent.set(dep, k); stack.push(dep); }
    }
  }
  return { seen, parent };
}

const report = { plugins: [], unusedDists: [], rootFilesOwner: [], unresolved: [] };

for (const id of fs.readdirSync(PLUGINS).sort()) {
  const dir = path.join(PLUGINS, id);
  if (!fs.statSync(dir).isDirectory()) continue;
  const { direct, local, files } = scanPlugin(dir);

  const seedDists = new Set();
  const unresolved = [];
  for (const [mod, where] of direct) {
    if (mod === 'kernelhub') continue;                 // 壳提供的适配器 SDK
    if (local.has(mod)) continue;                      // 插件自带的同级模块
    if (STDLIB.has(mod)) continue;
    const k = moduleToDist.get(mod);
    if (k) seedDists.add(k);
    else unresolved.push({ module: mod, where });
  }

  const { seen: allDists, parent } = distClosure(seedDists);

  let bytes = 0, nfiles = 0;
  const fileList = [];
  for (const k of allDists) {
    for (const f of dists.get(k).files) {
      try {
        const st = fs.statSync(path.join(VENDOR, f));
        if (st.isFile()) { bytes += st.size; nfiles += 1; fileList.push(f); }
      } catch { /* RECORD 里可能有没装上的条目 */ }
    }
  }

  report.plugins.push({
    id,
    directDists: [...seedDists].sort().map((k) => dists.get(k).fullName),
    allDists: [...allDists].sort().map((k) => dists.get(k).fullName),
    transitive: [...allDists].filter((k) => !seedDists.has(k)).map((k) => `${dists.get(k).fullName} <- ${parent.get(k) ? dists.get(parent.get(k)).fullName : '?'}`).sort(),
    files: nfiles,
    mb: +(bytes / 1024 / 1024).toFixed(2),
  });
  if (unresolved.length) report.unresolved.push({ id, unresolved });
}

const used = new Set();
for (const p of report.plugins) for (const d of p.allDists) used.add(d);
for (const [k, rec] of dists) if (!used.has(rec.fullName)) report.unusedDists.push(rec.fullName);

const rootFiles = fs.readdirSync(VENDOR).filter((n) => fs.statSync(path.join(VENDOR, n)).isFile());
for (const f of rootFiles) {
  let owner = '';
  for (const [, rec] of dists) if (rec.files.includes(f)) { owner = rec.fullName; break; }
  report.rootFilesOwner.push({ file: f, owner: owner || '**NO OWNER**' });
}

console.log(JSON.stringify(report, null, 1));
