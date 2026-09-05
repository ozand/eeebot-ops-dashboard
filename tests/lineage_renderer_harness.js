// #208: execute assets/vendor/lineage-renderer.js under Node against one day payload.
//
// Builds the smallest DOM the renderer touches (one <script class="lineage-day-data">
// inside a <section class="lineage-day-group"> that holds one <svg>), loads any vendored
// d3 files that still exist (so the pre-#208 renderer runs too), loads the renderer, and
// prints the rendered <svg> subtree plus the renderer's own counters as JSON.
//
//   node tests/lineage_renderer_harness.js <payload.json> [filter-probe.json]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const vendorDir = path.join(repoRoot, 'assets', 'vendor');
const payloadText = fs.readFileSync(process.argv[2], 'utf8');
const filterProbe = process.argv[3] ? JSON.parse(fs.readFileSync(process.argv[3], 'utf8')) : null;

class El {
  constructor(tag) {
    this.tag = tag;
    this.attrs = {};
    this.children = [];
    this.textContent = '';
    this.hidden = false;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  closest() { return section; }
  querySelector(selector) { return selector.indexOf('svg') === 0 ? svg : null; }
  querySelectorAll() { return []; }
  addEventListener() {}
  toJSON() {
    return { tag: this.tag, attrs: this.attrs, text: this.textContent, children: this.children };
  }
}

const svg = new El('svg');
svg.setAttribute('class', 'lineage-day-svg arch-tree');
svg.setAttribute('width', '180');
svg.setAttribute('height', '72');
svg.setAttribute('viewBox', '0 0 180 72');
svg.setAttribute('data-lineage-renderer', 'd3-dag');
const section = new El('section');
section.setAttribute('class', 'lineage-day-group');
const script = new El('script');
script.setAttribute('class', 'lineage-day-data');
script.textContent = payloadText;

const document = {
  readyState: 'complete',
  querySelectorAll(selector) { return selector === '.lineage-day-data' ? [script] : []; },
  querySelector() { return null; },
  getElementById() { return null; },
  createElementNS(_ns, tag) { return new El(tag); },
  createElement(tag) { return new El(tag); },
  addEventListener() {},
};
globalThis.window = globalThis;
globalThis.document = document;

for (const name of ['d3.min.js', 'd3-dag.iife.min.js']) {
  const file = path.join(vendorDir, name);
  if (fs.existsSync(file)) vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: name });
}
vm.runInThisContext(fs.readFileSync(path.join(vendorDir, 'lineage-renderer.js'), 'utf8'), { filename: 'lineage-renderer.js' });

const out = {
  loaded: globalThis.__lineageRendererLoaded === true,
  attempts: globalThis.__lineageRendererAttempts || 0,
  successes: globalThis.__lineageRendererSuccesses || 0,
  error: globalThis.__lineageRendererError || null,
  has_d3: !!(globalThis.d3 && globalThis.d3.sugiyama),
  svg: svg.toJSON(),
  filter: null,
};
if (filterProbe && globalThis.lineageDayFilter && typeof globalThis.lineageDayFilter.select === 'function') {
  out.filter = filterProbe.map(function (probe) {
    return globalThis.lineageDayFilter.select(probe.mode, probe.days, probe.now);
  });
}
process.stdout.write(JSON.stringify(out));
