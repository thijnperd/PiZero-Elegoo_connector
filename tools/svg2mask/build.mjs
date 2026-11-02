// Rasterises Tabler (MIT) SVGs into 1-bit icon masks for the TFT firmware.
//   node build.mjs
// Inputs : tools/svg2mask/svg/<name>.svg   (24x24 grid, 2px stroke, stroke=currentColor)
// Outputs: assets/icons/generated/icons.h  (row-packed bitmaps, MSB first)
//          assets/icons/PROOF.png          (labelled sheet drawn from those bitmaps)
import fs from 'node:fs';
import path from 'node:path';
import pkg from 'pngjs';
const { PNG } = pkg;
import { Resvg } from '@resvg/resvg-js';

const ROOT = path.resolve(process.cwd(), '..', '..');
const SVG_DIR = path.join(process.cwd(), 'svg');
const OUT_DIR = path.join(ROOT, 'assets', 'icons', 'generated');
const HEADER = path.join(OUT_DIR, 'icons.h');
const PROOF = path.join(ROOT, 'assets', 'icons', 'PROOF.png');

// Tabler draws a 24-unit grid with a 2px stroke. Rendering each icon 1:1 at its
// final size with that native stroke and thresholding at 50% coverage is what
// survives best: at 24px the stroke lands exactly on 2px, and at 16px the
// 1.33px stroke resolves to a clean 1px hairline. Supersampling (ss=4) was
// tried and produced broken, dotted strokes, so sampling stays at 1.
const STROKE = { 16: 2, 24: 2, 32: 2 };
const SS = 1;          // sample 1:1, threshold at 50% coverage

// The gear's teeth straddle pixel boundaries and threshold into specks. Half a
// grid unit of offset put them back on the grid (8 ragged pixels -> 2). Every
// other glyph is cleanest with no offset, so this stays per-icon.
// Values are in Tabler grid units, which equal pixels at the 24px render.
const NUDGE = { settings: [0.5, 0] };

const KEEP_CLEAR = 1;  // outer ring in px that must stay empty (bezel clips it)

const ICONS = [
  { name: 'clock',    size: 16 },
  { name: 'link',     size: 16 },
  { name: 'firmware', size: 16 },
  { name: 'offline',  size: 16 },
  { name: 'battery',  size: 16 },
  { name: 'sd',       size: 16 },
  { name: 'usb',      size: 16 },
  { name: 'lock',     size: 16 },

  { name: 'settings', size: 24 },
  { name: 'menu',     size: 24 },
  { name: 'back',     size: 24 },
  { name: 'up',       size: 24 },
  { name: 'down',     size: 24 },
  { name: 'enter',    size: 24 },
  { name: 'refresh',  size: 24 },
  { name: 'check',    size: 24 },
  { name: 'warn',     size: 24 },
  { name: 'info',     size: 24 },
  { name: 'power',    size: 24 },
  { name: 'search',   size: 24 },
  { name: 'save',     size: 24 },
  { name: 'cross',    size: 24 },

  // app tiles and menu entries
  { name: 'weather',  size: 24 },
  { name: 'cloud',    size: 24 },
  { name: 'rain',     size: 24 },
  { name: 'snow',     size: 24 },
  { name: 'storm',    size: 24 },
  { name: 'fog',      size: 24 },
  { name: 'moon',     size: 24 },
  { name: 'temperature', size: 24 },
  { name: 'wind',     size: 24 },
  { name: 'timer',    size: 24 },
  { name: 'alarm',    size: 24 },
  { name: 'calendar', size: 24 },
  { name: 'clock',    size: 24 },
  { name: 'printer',  size: 24 },
  { name: 'terminal', size: 24 },
  { name: 'music',    size: 24 },
  { name: 'folder',   size: 24 },
  { name: 'download', size: 24 },
  { name: 'upload',   size: 24 },
  { name: 'bell',     size: 24 },

  { name: 'boot',     size: 32 }
];

const OPTIONAL = new Set(['battery', 'sd', 'usb', 'lock', 'boot']);

const GROUPS = [
  { size: 16, title: '16x16  status row' },
  { size: 24, title: '24x24  actions and app tiles' },
  { size: 32, title: '32x32  boot mark' }
];

function mask(svgSource, size, strokeWidth, dx = 0, dy = 0) {
  const px = size * SS;
  let svg = svgSource
    .replace(/stroke="currentColor"/g, 'stroke="#ffffff"')
    .replace(/(<svg[^>]*?)\swidth="24"/, `$1 width="${px}"`)
    .replace(/(<svg[^>]*?)\sheight="24"/, `$1 height="${px}"`)
    .replace(/stroke-width="2"/g, `stroke-width="${strokeWidth}"`);

  if (dx || dy) {
    svg = svg
      .replace(/(<svg[^>]*>)/, `$1<g transform="translate(${dx} ${dy})">`)
      .replace(/<\/svg>/, '</g></svg>');
  }

  const png = PNG.sync.read(
    new Resvg(svg, { background: 'rgba(0,0,0,0)', fitTo: { mode: 'width', value: px } })
      .render()
      .asPng()
  );

  const bits = new Uint8Array(size * size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let sum = 0;
      for (let dy2 = 0; dy2 < SS; dy2++) {
        for (let dx2 = 0; dx2 < SS; dx2++) {
          sum += png.data[((y * SS + dy2) * png.width + x * SS + dx2) * 4 + 3];
        }
      }
      bits[y * size + x] = sum / (SS * SS) >= 128 ? 1 : 0;
    }
  }

  if (KEEP_CLEAR > 0) {
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (x < KEEP_CLEAR || y < KEEP_CLEAR || x >= size - KEEP_CLEAR || y >= size - KEEP_CLEAR) {
          bits[y * size + x] = 0;
        }
      }
    }
  }
  return bits;
}

function pack(bits, size) {
  const stride = Math.ceil(size / 8);
  const out = new Uint8Array(stride * size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if (bits[y * size + x]) out[y * stride + (x >> 3)] |= 0x80 >> (x & 7);
    }
  }
  return out;
}

const built = [];
for (const icon of ICONS) {
  const file = path.join(SVG_DIR, `${icon.name}.svg`);
  if (!fs.existsSync(file)) throw new Error(`missing svg: ${file}`);
  const [dx, dy] = NUDGE[icon.name] || [0, 0];
  const bits = mask(fs.readFileSync(file, 'utf8'), icon.size, STROKE[icon.size], dx, dy);
  const packed = pack(bits, icon.size);
  const on = bits.reduce((a, b) => a + b, 0);
  if (on === 0) throw new Error(`empty mask: ${icon.name}`);
  built.push({ ...icon, bits, packed, on });
  console.log(
    `${icon.name.padEnd(10)} ${icon.size}x${icon.size}  ${String(on).padStart(3)} px on  ${packed.length} bytes`
  );
}

// ---- header ---------------------------------------------------------------
const lines = [];
lines.push('// Generated by tools/svg2mask/build.mjs - do not edit by hand.');
lines.push('// Artwork: Tabler Icons, MIT licence, https://tabler.io/icons');
lines.push('// Each mask is row-packed, MSB first: stride = ceil(size/8) bytes per row, 1 = ink.');
lines.push('#pragma once');
lines.push('#include <stdint.h>');
lines.push('');
lines.push('// AVR needs PROGMEM; ARM/ESP32 can read flash directly.');
lines.push('#ifndef ICON_FLASH');
lines.push('#  if defined(__AVR__)');
lines.push('#    include <avr/pgmspace.h>');
lines.push('#    define ICON_FLASH PROGMEM');
lines.push('#  else');
lines.push('#    define ICON_FLASH');
lines.push('#  endif');
lines.push('#endif');
lines.push('');

for (const i of built) {
  const id = `ICON_${i.name.toUpperCase()}_${i.size}`;
  const stride = Math.ceil(i.size / 8);
  lines.push(`// ${i.name} ${i.size}x${i.size}`);
  lines.push(`static const uint8_t ${id}[${i.packed.length}] ICON_FLASH = {`);
  const rows = [];
  for (let r = 0; r < i.size; r++) {
    const chunk = Array.from(i.packed.slice(r * stride, r * stride + stride))
      .map((b) => '0x' + b.toString(16).padStart(2, '0'))
      .join(', ');
    rows.push('  ' + chunk + (r === i.size - 1 ? '' : ','));
  }
  lines.push(rows.join('\n'));
  lines.push('};');
  lines.push('');
}

lines.push('struct icon_t {');
lines.push('  const uint8_t* data;');
lines.push('  uint8_t w;');
lines.push('  uint8_t h;');
lines.push('  const char* name;');
lines.push('};');
lines.push('');
lines.push('static const icon_t ICONS[] = {');
for (const i of built) {
  lines.push(
    `  { ICON_${i.name.toUpperCase()}_${i.size}, ${i.size}, ${i.size}, "${i.name}" },`
  );
}
lines.push('};');
lines.push('');
lines.push('static const uint8_t ICON_COUNT = sizeof(ICONS) / sizeof(ICONS[0]);');

fs.mkdirSync(OUT_DIR, { recursive: true });
fs.writeFileSync(HEADER, lines.join('\n') + '\n');

// ---- labelled proof sheet -------------------------------------------------
// Drawn from the same bits that go into the header, so what you see is exactly
// what the firmware blits - no SVG re-render, no separate art path.
const CELL = 200;
const GAP = 16;
const PAD = 30;
const COLS = 5;
const SCALE = { 16: 11, 24: 8, 32: 6 };
const HEADER_H = 46;

const sheetW = PAD * 2 + COLS * CELL + (COLS - 1) * GAP;

let cursor = PAD + 46;
const marks = [];
for (const group of GROUPS) {
  const members = built.filter((i) => i.size === group.size);
  if (!members.length) continue;
  marks.push(`<text x="${PAD}" y="${cursor}" class="group">${group.title}</text>`);
  cursor += HEADER_H;
  for (let r = 0; r < Math.ceil(members.length / COLS); r++) {
    for (let c = 0; c < COLS; c++) {
      const icon = members[r * COLS + c];
      if (!icon) continue;
      const ox = PAD + c * (CELL + GAP);
      const oy = cursor;
      const s = SCALE[icon.size];
      const art = icon.size * s;
      const off = Math.floor((CELL - art) / 2);

      marks.push(`<rect x="${ox}" y="${oy}" width="${CELL}" height="${CELL}" fill="#181b22"/>`);

      // pixel grid
      let grid = '';
      for (let k = 0; k <= icon.size; k++) {
        const p = off + k * s;
        const len = Math.max(0, art - k * s);  // last line has zero length, not a full overshoot
        grid += `M${p} ${off}h${len}M${off} ${p}v${len}`;
      }
      marks.push(
        `<path d="${grid}" transform="translate(${ox} ${oy})" stroke="#23272f" stroke-width="1" fill="none"/>`
      );

      // ink, one subpath per lit pixel
      let ink = '';
      for (let y = 0; y < icon.size; y++) {
        for (let x = 0; x < icon.size; x++) {
          if (icon.bits[y * icon.size + x]) {
            ink += `M${off + x * s} ${off + y * s}h${s}v${s}h-${s}z`;
          }
        }
      }
      marks.push(`<path d="${ink}" transform="translate(${ox} ${oy})" fill="#ffffff"/>`);

      // keep-clear ring
      marks.push(
        `<rect x="${ox + off + s}" y="${oy + off + s}" width="${art - s * 2}" height="${art - s * 2}" ` +
        `fill="none" stroke="#c84646" stroke-width="1"/>`
      );

      const opt = OPTIONAL.has(icon.name) ? ' (optional)' : '';
      marks.push(
        `<text x="${ox + CELL / 2}" y="${oy + CELL + 22}" class="label${opt ? ' opt' : ''}">` +
        `${icon.name}${opt}</text>`
      );
    }
    cursor += CELL + 40;
  }
  cursor += 34;
}

const sheetH = cursor + 34;
const noteY = sheetH - 20;
marks.push(
  `<text x="${PAD}" y="${noteY}" class="note">` +
  `Every pixel above is one bit of ${built.length} masks (${built.reduce((a, b) => a + b.packed.length, 0)} bytes total). ` +
  `Red ring = 1px keep-clear. Source: Tabler Icons, MIT.</text>`
);

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${sheetW}" height="${sheetH}" viewBox="0 0 ${sheetW} ${sheetH}">
<style>
  text { font-family: 'Segoe UI', Arial, sans-serif; }
  .title { font-size: 20px; font-weight: 600; fill: #e1e4eb; }
  .group { font-size: 16px; font-weight: 600; fill: #6e96e6; }
  .label { font-family: Consolas, 'Courier New', monospace; font-size: 13px; fill: #e1e4eb; text-anchor: middle; }
  .opt { fill: #8c93a0; }
  .note { font-size: 11px; fill: #8c93a0; }
</style>
<rect width="${sheetW}" height="${sheetH}" fill="#0e1016"/>
<text x="${PAD}" y="${PAD + 20}" class="title">Icon masks - Tabler Icons, MIT - generated from icons.h</text>
${marks.join('\n')}
</svg>`;

fs.writeFileSync(
  PROOF,
  new Resvg(svg, {
    font: { loadSystemFonts: true, defaultFontFamily: 'Segoe UI' },
    fitTo: { mode: 'width', value: sheetW }
  }).render().asPng()
);

console.log(`\nwrote ${path.relative(ROOT, HEADER)}`);
console.log(`wrote ${path.relative(ROOT, PROOF)}  (${built.length} icons, ${sheetW}x${sheetH})`);
