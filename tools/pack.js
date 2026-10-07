'use strict';
/**
 * 把每个插件打成 zip，供**没有 git 的用户**走 HTTPS 回退下载。
 *
 * 落盘布局与仓库一致：包内条目是 `plugins/<id>/...`，
 * 应用侧 src/engine/pluginStore.js 解压后直接取 `plugins/<id>`。
 *
 * 为什么自己写 zip 而不是调 Compress-Archive / zip 命令：
 *   - 项目坚持零运行时依赖，且要在 Windows / Linux / macOS 上行为一致
 *   - 需要精确控制条目名（必须是正斜杠、必须带 plugins/<id> 前缀），
 *     PowerShell 的 Compress-Archive 在不同版本下前缀行为不一样
 *   - 只有 deflate + CRC32 两件事，Node 的 zlib 已经提供了前者
 *
 * 用法:
 *   node tools/pack.js              打包全部插件到 dist/
 *   node tools/pack.js pillow-image 只打某一个
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const ROOT = path.resolve(__dirname, '..');
const PLUGINS_DIR = path.join(ROOT, 'plugins');
const DIST = path.join(ROOT, 'dist');

/* ---------------------------------------------------------------- CRC32 */

const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buf) {
  let c = 0 ^ -1;
  for (let i = 0; i < buf.length; i += 1) c = (c >>> 8) ^ CRC_TABLE[(c ^ buf[i]) & 0xff];
  return (c ^ -1) >>> 0;
}

/* ------------------------------------------------------------ 最小 zip 写 */

/**
 * 写一个 zip。
 * @param {Array<{name:string, data:Buffer}>} entries 条目名必须已用 '/' 分隔
 * @returns {Buffer}
 */
function writeZip(entries) {
  const locals = [];
  const centrals = [];
  let offset = 0;

  for (const e of entries) {
    const nameBuf = Buffer.from(e.name, 'utf8');
    const crc = crc32(e.data);
    const deflated = zlib.deflateRawSync(e.data, { level: 9 });
    // 压缩反而更大时（已压缩过的二进制）就用 stored
    const useDeflate = deflated.length < e.data.length;
    const payload = useDeflate ? deflated : e.data;
    const method = useDeflate ? 8 : 0;

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt16LE(0x0800, 6); // flag: 文件名为 UTF-8
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(0, 10); // mod time
    local.writeUInt16LE(0x21, 12); // mod date（1980-01-01，固定以保证可复现）
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(payload.length, 18);
    local.writeUInt32LE(e.data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    locals.push(local, nameBuf, payload);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4); // version made by
    central.writeUInt16LE(20, 6); // version needed
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(method, 10);
    central.writeUInt16LE(0, 12);
    central.writeUInt16LE(0x21, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(payload.length, 20);
    central.writeUInt32LE(e.data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt16LE(0, 30); // extra
    central.writeUInt16LE(0, 32); // comment
    central.writeUInt16LE(0, 34); // disk
    central.writeUInt16LE(0, 36); // internal attrs
    central.writeUInt32LE(0, 38); // external attrs
    central.writeUInt32LE(offset, 42);
    centrals.push(central, nameBuf);

    offset += local.length + nameBuf.length + payload.length;
  }

  const centralBuf = Buffer.concat(centrals);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralBuf.length, 12);
  eocd.writeUInt32LE(offset, 16);
  eocd.writeUInt16LE(0, 20);

  return Buffer.concat([...locals, centralBuf, eocd]);
}

/* ------------------------------------------------------------------ 打包 */

function walk(dir, base = dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, base, out);
    else out.push(path.relative(base, p).replace(/\\/g, '/'));
  }
  return out;
}

function packOne(id) {
  const dir = path.join(PLUGINS_DIR, id);
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'kernel.json'), 'utf8').replace(/^\uFEFF/, ''));
  const version = String(manifest.version || '0.0.0');
  const files = walk(dir).sort();
  const entries = files.map((rel) => ({
    name: `plugins/${id}/${rel}`,
    data: fs.readFileSync(path.join(dir, rel)),
  }));

  fs.mkdirSync(DIST, { recursive: true });
  const outName = `${id}-${version}.zip`;
  const outPath = path.join(DIST, outName);
  const buf = writeZip(entries);
  fs.writeFileSync(outPath, buf);

  const raw = entries.reduce((n, e) => n + e.data.length, 0);
  return { id, version, outName, outPath, files: files.length, raw, zip: buf.length };
}

const only = process.argv[2];
const ids = only ? [only] : fs.readdirSync(PLUGINS_DIR).filter((n) => fs.statSync(path.join(PLUGINS_DIR, n)).isDirectory()).sort();

console.log(`打包 ${ids.length} 个插件 → ${DIST}`);
let totalRaw = 0;
let totalZip = 0;
const rows = [];
for (const id of ids) {
  const r = packOne(id);
  rows.push(r);
  totalRaw += r.raw;
  totalZip += r.zip;
}
for (const r of rows) {
  console.log(
    `  ${r.outName.padEnd(34)} ${String(r.files).padStart(5)} 文件  ` +
      `${(r.raw / 1024 / 1024).toFixed(2).padStart(8)} MB → ${(r.zip / 1024 / 1024).toFixed(2).padStart(7)} MB` +
      `  (压缩率 ${((1 - r.zip / Math.max(r.raw, 1)) * 100).toFixed(0)}%)`
  );
}
console.log(`合计 ${(totalRaw / 1024 / 1024).toFixed(2)} MB → ${(totalZip / 1024 / 1024).toFixed(2)} MB`);

module.exports = { writeZip, crc32 };
