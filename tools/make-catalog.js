'use strict';
/**
 * 生成 catalog.json —— 插件仓库的目录清单，应用靠它渲染「插件」页并决定下载什么。
 *
 * 每个插件的 sha256 是**内容树哈希**：对所有文件按相对路径排序后，
 * 对 `相对路径\0文件sha256\n` 逐行做 SHA-256。这样应用装完可以直接校验，
 * 且与文件系统的遍历顺序、时间戳无关。
 *
 * 用法: node tools/make-catalog.js [仓库根目录]
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const PLUGINS_DIR = path.join(ROOT, 'plugins');

function walk(dir, base = dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, base, out);
    else out.push(path.relative(base, p).replace(/\\/g, '/'));
  }
  return out;
}

function sha256File(p) {
  return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
}

/** 内容树哈希：与遍历顺序无关 */
function treeHash(dir) {
  const files = walk(dir).sort();
  const h = crypto.createHash('sha256');
  let bytes = 0;
  for (const rel of files) {
    const abs = path.join(dir, rel);
    const st = fs.statSync(abs);
    bytes += st.size;
    h.update(rel);
    h.update('\0');
    h.update(sha256File(abs));
    h.update('\n');
  }
  return { hash: h.digest('hex'), files: files.length, bytes };
}

/** 这个插件需要用户自己准备什么外部程序 */
function externalTools(manifest) {
  const out = new Set();
  const probe = manifest.probe || {};
  const exes = ((manifest['x-cli'] || manifest.xCli || {}).executables) || {};
  for (const [name, spec] of Object.entries(exes)) {
    const candidates = (spec && typeof spec === 'object' && spec.candidates) || [name];
    const label = Array.isArray(candidates) ? candidates[0] : String(candidates);
    out.add(label);
  }
  if (probe.type === 'command' && probe.target) out.add(String(probe.target));
  return [...out].sort();
}

const plugins = [];
for (const id of fs.readdirSync(PLUGINS_DIR).sort()) {
  const dir = path.join(PLUGINS_DIR, id);
  if (!fs.statSync(dir).isDirectory()) continue;
  const mf = path.join(dir, 'kernel.json');
  if (!fs.existsSync(mf)) {
    console.warn(`  ! 跳过 ${id}：没有 kernel.json`);
    continue;
  }
  const manifest = JSON.parse(fs.readFileSync(mf, 'utf8').replace(/^\uFEFF/, ''));
  const t = treeHash(dir);
  const vendorDir = path.join(dir, 'vendor');
  const vendor = fs.existsSync(vendorDir) ? treeHash(vendorDir) : { hash: '', files: 0, bytes: 0 };

  plugins.push({
    id: manifest.id || id,
    name: manifest.name || id,
    version: manifest.version || '0.0.0',
    ckp: manifest.ckp || '1.0',
    kind: manifest.kind || 'other',
    description: manifest.description || '',
    homepage: manifest.homepage || '',
    license: manifest.license || '',
    priority: typeof manifest.priority === 'number' ? manifest.priority : 0,
    tags: manifest.tags || [],
    runtime: (manifest.runtime || {}).type || 'python',
    requires: ((manifest.runtime || {}).requires) || [],
    probe: probeSummary(manifest),
    external: externalTools(manifest),
    capabilities: (manifest.capabilities || []).length,
    ops: [...new Set((manifest.capabilities || []).map((c) => c.op))].sort(),
    /**
     * 支持的格式清单（from = 能读，to = 能写）。
     * 应用要用它算界面显示名（「图片 PNG / JPG」这种：类型 + 最典型的两个扩展名），
     * 所以必须在目录里带上 —— 否则「可安装」列表还没装就知道不了插件支持什么格式。
     */
    formats: formatLists(manifest),
    path: `plugins/${id}`,
    size: t.bytes,
    files: t.files,
    vendorSize: vendor.bytes,
    sha256: t.hash,
  });
}

/** 汇总所有能力里出现过的输入/输出格式（去重、去通配符、排序） */
function formatLists(manifest) {
  const from = new Set();
  const to = new Set();
  for (const c of manifest.capabilities || []) {
    for (const f of c.from || []) if (f && f !== '*') from.add(String(f).toLowerCase());
    for (const f of c.to || []) if (f && f !== '*') to.add(String(f).toLowerCase());
  }
  return { from: [...from].sort(), to: [...to].sort() };
}

function probeSummary(manifest) {
  const p = manifest.probe || {};
  return { type: p.type || 'none', target: p.target || '' };
}

const catalog = {
  schema: 1,
  // 应用用这两个字段判断「这份目录我能不能用」
  ckp: '1.0',
  app: '>=2.0.0',
  repository: 'https://github.com/sigewinner/kernelhub-plugins',
  updated: new Date().toISOString().slice(0, 10),
  pluginCount: plugins.length,
  totalSize: plugins.reduce((n, p) => n + p.size, 0),
  plugins,
};

const out = path.join(ROOT, 'catalog.json');
fs.writeFileSync(out, JSON.stringify(catalog, null, 2) + '\n', 'utf8');

console.log(`已写入 ${out}`);
console.log(`  插件数: ${plugins.length}`);
console.log(`  总体积: ${(catalog.totalSize / 1024 / 1024).toFixed(2)} MB`);
for (const p of plugins) {
  console.log(
    `  ${p.id.padEnd(20)} v${p.version.padEnd(7)} ${(p.size / 1024 / 1024).toFixed(2).padStart(7)} MB  ` +
      `vendor=${(p.vendorSize / 1024 / 1024).toFixed(2).padStart(7)} MB  ${p.capabilities} 能力` +
      (p.external.length ? `  外部依赖: ${p.external.join('/')}` : '')
  );
}
