// #218: execute assets/vendor/lineage-renderer.js under Node against a unified payload.
//
// Accepts either:
//   - OLD per-day format: { day, current_sha, nodes[], edges[] } (converted to v2 payload)
//   - NEW v2 unified format: { version:2, nodes[], edges[], aliases, coverage, ... }
//
// Prints rendered SVG subtree + renderer counters as JSON.
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
  closest() { return null; }
  querySelector(selector) {
    if (selector && (selector.indexOf('svg') >= 0 || selector === '#lineage-svg')) return svg;
    if (selector && selector.indexOf('[data-lineage-from]') >= 0) return null;
    if (selector && selector.indexOf('[data-lineage-to]') >= 0) return null;
    if (selector && selector.indexOf('.lineage-filter-note') >= 0) return filterNote;
    return null;
  }
  querySelectorAll() { return []; }
  addEventListener() {}
  scrollIntoView() {}
  focus() {}
  blur() {}
  toJSON() {
    return { tag: this.tag, attrs: this.attrs, text: this.textContent, children: this.children };
  }
}

const svg = new El('svg');
svg.setAttribute('id', 'lineage-svg');
svg.setAttribute('class', 'lineage-day-svg lineage-unified-dag arch-tree');
svg.setAttribute('width', '220');
svg.setAttribute('height', '84');
svg.setAttribute('viewBox', '0 0 220 84');
svg.setAttribute('data-lineage-renderer', 'unified-dag');

const filterNote = new El('span');
filterNote.setAttribute('class', 'lineage-filter-note');

// Convert old per-day payload to v2 if needed
let inputPayload = JSON.parse(payloadText);
let v2payload;
if (inputPayload.version === 2) {
  v2payload = inputPayload;
} else {
  // Convert old format: nodes use 'sha' as id; edges use source/target as sha
  const nodes = (inputPayload.nodes || []).map(function(n) {
    var nid = n.sha ? ('c:' + n.sha) : ('a:' + (n.cycle_id || n.sha));
    return Object.assign({}, n, { node_id: nid });
  });
  const nodeById = {};
  nodes.forEach(function(n) { nodeById[n.node_id] = n; });
  const edges = (inputPayload.edges || []).map(function(e) {
    return { source: e.source ? 'c:' + e.source : null, target: e.target ? 'c:' + e.target : null, basis: e.basis, source_available: true };
  }).filter(function(e) { return e.source && e.target; });
  const aliases = {};
  nodes.forEach(function(n) {
    if (n.cycle_id) aliases[n.cycle_id] = n.node_id;
  });
  v2payload = {
    version: 2,
    current_sha: inputPayload.current_sha || '',
    current_node_id: inputPayload.current_sha ? ('c:' + inputPayload.current_sha) : '',
    coverage: { raw_read_rows: nodes.length, unique_candidate_nodes: nodes.length, emitted_nodes: nodes.length, truncated: false },
    nodes: nodes,
    edges: edges,
    aliases: aliases,
  };
}

const lineageDataScript = new El('script');
lineageDataScript.setAttribute('id', 'lineage-data');
lineageDataScript.setAttribute('type', 'application/json');
lineageDataScript.textContent = JSON.stringify(v2payload);

const document = {
  readyState: 'complete',
  _listeners: {},
  querySelectorAll(selector) { return []; },
  querySelector(selector) {
    if (!selector) return null;
    if (selector === '#lineage-svg' || selector.indexOf('lineage-svg') >= 0) return svg;
    if (selector.indexOf('lineage-filter-note') >= 0) return filterNote;
    if (selector.indexOf('[data-lineage-from]') >= 0) return null;
    if (selector.indexOf('[data-lineage-to]') >= 0) return null;
    return null;
  },
  getElementById(id) {
    if (id === 'lineage-svg') return svg;
    if (id === 'lineage-data') return lineageDataScript;
    return null;
  },
  createElementNS(_ns, tag) { return new El(tag); },
  createElement(tag) { return new El(tag); },
  addEventListener(ev, fn) {
    if (!document._listeners[ev]) document._listeners[ev] = [];
    document._listeners[ev].push(fn);
  },
  _fire(ev, arg) {
    (document._listeners[ev] || []).forEach(function(fn) { fn(arg || {}); });
  },
};
globalThis.window = Object.assign(globalThis, { lineageRenderer: null, lineageDayFilter: null, location: { hash: '' } });
globalThis.document = document;
globalThis.history = { replaceState() {} };

vm.runInThisContext(fs.readFileSync(path.join(vendorDir, 'lineage-renderer.js'), 'utf8'), { filename: 'lineage-renderer.js' });

// Trigger DOMContentLoaded if renderer registered for it
document._fire('DOMContentLoaded');

// Always re-render with 'all' for geometry harness tests (overrides default filter)
if (globalThis.lineageRenderer && typeof globalThis.lineageRenderer.applyFilter === 'function') {
  globalThis.lineageRenderer.applyFilter('all');
}

const out = {
  loaded: globalThis.__lineageRendererLoaded === true,
  attempts: 1,
  successes: (svg.children.length > 0 && svg.getAttribute('data-lineage-rendered') === 'unified-dag') ? 1 : 0,
  error: globalThis.__lineageRendererError || null,
  reRooted: globalThis.__lineageRendererReRooted || 0,
  has_d3: false,
  svg: svg.toJSON(),
  filter: null,
};

// Filter probe: call projectUnifiedGraph with given windows
if (filterProbe && globalThis.lineageRenderer && typeof globalThis.lineageRenderer.projectUnifiedGraph === 'function') {
  out.filter = filterProbe.map(function(probe) {
    var win;
    var now = probe.now ? new Date(probe.now) : new Date();
    if (probe.mode === 'all') {
      win = { all: true };
    } else if (probe.mode === '24h') {
      win = { start: now.getTime() - 86400000, end: now.getTime() };
    } else if (probe.mode === 'today') {
      var todayStart = Date.parse(now.toISOString().slice(0, 10) + 'T00:00:00.000Z');
      win = { start: todayStart, end: now.getTime() };
    } else {
      win = { all: true };
    }
    var proj = globalThis.lineageRenderer.projectUnifiedGraph(v2payload, { window: win });
    return {
      mode: probe.mode,
      nodeCount: proj.nodes.length,
      edgeCount: proj.edges.length,
      empty: proj.empty || false,
      note: proj.note || '',
    };
  });
} else if (filterProbe && globalThis.lineageDayFilter && typeof globalThis.lineageDayFilter.select === 'function') {
  // Legacy fallback for old tests that used lineageDayFilter.select
  out.filter = filterProbe.map(function(probe) {
    return globalThis.lineageDayFilter.select(probe.mode, probe.days || [], probe.now);
  });
}

process.stdout.write(JSON.stringify(out));
