(function () {
  'use strict';

  var PITCH_X = 44;
  var PITCH_Y = 54;
  var MARGIN_X = 24;
  var MARGIN_TOP = 32;
  var RADIUS = 9;
  var state = { payload: null, mode: 'yesterday-today', rendered: null };

  function nodeIdToDomId(nodeId) {
    // Keep encodeURIComponent's percent escapes intact. Replacing '%' with '_'
    // is not injective because node IDs may themselves contain underscores.
    return 'node-' + encodeURIComponent(String(nodeId || ''));
  }
  function domIdToNodeId(domId) {
    if (!domId || !String(domId).startsWith('node-')) return null;
    try { return decodeURIComponent(String(domId).slice(5)); }
    catch (_error) { return null; }
  }
  function svgEl(tag) { return document.createElementNS('http://www.w3.org/2000/svg', tag); }
  function parseTs(node) {
    if (!node || node.ts == null || node.ts === '') return null;
    var value = Date.parse(node.ts);
    return isNaN(value) ? null : value;
  }
  function utcDay(date) { return date.toISOString().slice(0, 10); }
  function dayStartMs(day) { return Date.parse(day + 'T00:00:00.000Z'); }
  function byNodeId(payload) {
    var out = {};
    ((payload && payload.nodes) || []).forEach(function (node) { out[node.node_id] = node; });
    return out;
  }
  function parentEdges(payload) {
    var out = {};
    ((payload && payload.edges) || []).forEach(function (edge) {
      if (edge && edge.target) out[edge.target] = edge;
    });
    return out;
  }
  function resolveToken(payload, token) {
    if (!payload || !token) return null;
    var raw = String(token);
    var exact = domIdToNodeId(raw.startsWith('node-') ? raw : 'node-' + raw);
    var nodes = byNodeId(payload);
    if (exact && nodes[exact]) return exact;
    try {
      var decoded = decodeURIComponent(raw);
      if (nodes[decoded]) return decoded;
      if (payload.aliases && payload.aliases[decoded]) return payload.aliases[decoded];
      if (payload.aliases && payload.aliases[raw]) return payload.aliases[raw];
    } catch (_error) {
      if (payload.aliases && payload.aliases[raw]) return payload.aliases[raw];
    }
    return null;
  }
  function showNote(text) {
    var note = document.querySelector('.lineage-filter-note');
    if (!note) return;
    note.textContent = text || '';
    note.hidden = !text;
  }
  function setActiveButton(mode) {
    document.querySelectorAll('[data-lineage-filter]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-lineage-filter') === mode);
    });
  }
  function windowForMode(mode, payload, nowValue) {
    if (mode === 'all') return { all: true, note: '' };
    var now = nowValue ? new Date(nowValue) : new Date();
    if (isNaN(now.getTime())) return { all: true, note: 'Clock unavailable; showing all loaded history.' };
    if (mode === '24h') return { start: now.getTime() - 86400000, end: now.getTime(), note: '' };
    if (mode === 'today') {
      var todayStart = dayStartMs(utcDay(now));
      return { start: todayStart, end: now.getTime(), note: '' };
    }
    if (mode === 'range') {
      var fromEl = document.querySelector('[data-lineage-from]');
      var toEl = document.querySelector('[data-lineage-to]');
      var from = fromEl && fromEl.value;
      var to = toEl && toEl.value;
      if (from && to && from > to) return { invalid: true, note: "Invalid range: 'from' must be before or equal to 'to'" };
      return {
        start: from ? dayStartMs(from) : -Infinity,
        end: to ? dayStartMs(to) + 86400000 : Infinity,
        halfOpen: true,
        note: ''
      };
    }
    var start = dayStartMs(utcDay(new Date(now.getTime() - 86400000)));
    return { start: start, end: now.getTime(), note: '' };
  }
  function inWindow(node, win) {
    if (win.all) return true;
    var ts = parseTs(node);
    if (ts == null) return false;
    if (win.halfOpen) return ts >= win.start && ts < win.end;
    return ts >= win.start && ts <= win.end;
  }
  function edgeBasisForPath(pathEdges) {
    var seen = {};
    pathEdges.forEach(function (edge) { seen[edge.basis || 'recorded'] = true; });
    var keys = Object.keys(seen);
    return keys.length > 1 ? 'mixed' : (keys[0] || 'recorded');
  }
  function projectUnifiedGraph(payload, opts) {
    opts = opts || {};
    var nodes = (payload && payload.nodes) || [];
    var nodeMap = byNodeId(payload);
    var pEdges = parentEdges(payload);
    var win = opts.window || { all: true };
    if (win.invalid) return { nodes: [], edges: [], empty: false, note: win.note, invalid: true };
    var visible = {};
    nodes.forEach(function (node) { if (inWindow(node, win)) visible[node.node_id] = true; });
    var visibleIds = Object.keys(visible);
    if (!win.all && visibleIds.length === 0) {
      return { nodes: [], edges: [], empty: true, note: 'No cycles recorded in selected interval' };
    }
    if (win.all) {
      return { nodes: nodes.slice(), edges: ((payload && payload.edges) || []).filter(function (edge) { return nodeMap[edge.source] && nodeMap[edge.target]; }).map(function (edge) {
        return { source: edge.source, target: edge.target, basis: edge.basis || 'recorded', type: 'canonical', path: [edge.source, edge.target] };
      }), visible: visible };
    }
    var descendantHits = {};
    visibleIds.forEach(function (id) {
      var seen = {};
      var cur = id;
      while (pEdges[cur] && pEdges[cur].source && !seen[cur]) {
        seen[cur] = true;
        var parent = pEdges[cur].source;
        if (!nodeMap[parent]) break;
        descendantHits[parent] = descendantHits[parent] || {};
        descendantHits[parent][id] = true;
        cur = parent;
      }
    });
    var keep = {};
    visibleIds.forEach(function (id) { keep[id] = true; });
    // Preserve contextual endpoints. A common ancestor/junction is retained;
    // a single-descendant chain retains its root endpoint and collapses only
    // the maximal linear intermediates into a provenance-bearing path.
    Object.keys(descendantHits).forEach(function (id) {
      var descendants = Object.keys(descendantHits[id]);
      var parent = pEdges[id] && pEdges[id].source;
      if (descendants.length >= 2 || !parent || !nodeMap[parent]) keep[id] = true;
    });
    var projectedEdges = [];
    Object.keys(keep).forEach(function (target) {
      var direct = pEdges[target];
      if (!direct || !direct.source) return;
      var source = direct.source;
      var pathIds = [target];
      var pathEdges = [direct];
      var guard = {};
      // Walk only through hidden nodes. A retained visible/context node is an
      // endpoint, never an intermediate in a shortcut path.
      while (source && nodeMap[source] && !guard[source] && !visible[source]) {
        guard[source] = true;
        pathIds.unshift(source);
        var next = pEdges[source];
        if (!next || !next.source || !nodeMap[next.source]) break;
        pathEdges.unshift(next);
        source = next.source;
      }
      if (pathIds[0] !== source) pathIds.unshift(source);
      if (!nodeMap[source]) return;
      var basis = edgeBasisForPath(pathEdges);
      var hiddenCount = Math.max(0, pathIds.length - 2);
      projectedEdges.push({
        source: source,
        target: target,
        basis: basis,
        type: hiddenCount ? 'collapsed' : (visible[source] && visible[target] ? 'canonical' : 'context'),
        collapsedNodes: hiddenCount,
        collapsedEdges: pathEdges.length,
        path: pathIds
      });
    });
    return { nodes: nodes.filter(function (node) { return keep[node.node_id]; }), edges: projectedEdges, visible: visible, descendantHits: descendantHits };
  }
  function layoutProjection(projection) {
    var nodes = projection.nodes || [];
    var nodeMap = {};
    nodes.forEach(function (node) { nodeMap[node.node_id] = node; });
    var children = {};
    var incoming = {};
    (projection.edges || []).forEach(function (edge) {
      if (!nodeMap[edge.source] || !nodeMap[edge.target]) return;
      (children[edge.source] || (children[edge.source] = [])).push(edge.target);
      incoming[edge.target] = true;
    });
    var roots = nodes.filter(function (node) { return !incoming[node.node_id]; }).map(function (node) { return node.node_id; });
    var width = {};
    var cycle_detected = false;
    function measure(id, guard) {
      if (guard[id]) { cycle_detected = true; return 1; }
      guard[id] = true;
      var total = 0;
      (children[id] || []).forEach(function (kid) { total += measure(kid, Object.assign({}, guard)); });
      width[id] = Math.max(1, total);
      return width[id];
    }
    roots.forEach(function (root) { measure(root, {}); });
    var positions = {};
    var maxDepth = 0;
    function place(id, left, depth, guard) {
      if (guard[id]) { cycle_detected = true; return; }
      guard[id] = true;
      var span = width[id] || 1;
      positions[id] = { x: MARGIN_X + (left + span / 2) * PITCH_X, y: MARGIN_TOP + depth * PITCH_Y, depth: depth };
      if (depth > maxDepth) maxDepth = depth;
      var cursor = left;
      (children[id] || []).forEach(function (kid) { place(kid, cursor, depth + 1, Object.assign({}, guard)); cursor += width[kid] || 1; });
    }
    var cursor = 0;
    roots.forEach(function (root) { place(root, cursor, 0, {}); cursor += width[root] || 1; });
    nodes.forEach(function (node) {
      if (positions[node.node_id]) return;
      cycle_detected = true;
      measure(node.node_id, {});
      place(node.node_id, cursor, 0, {});
      cursor += width[node.node_id] || 1;
    });
    return { positions: positions, width: Math.max(220, Math.round(MARGIN_X * 2 + Math.max(1, cursor) * PITCH_X)), height: Math.max(84, MARGIN_TOP + maxDepth * PITCH_Y + RADIUS + 28), cycle_detected: cycle_detected };
  }
  function renderUnified(svg, payload, projection) {
    projection = projection || projectUnifiedGraph(payload, { window: { all: true } });
    var layout = layoutProjection(projection);
    state.rendered = projection;
    svg.setAttribute('width', layout.width);
    svg.setAttribute('height', layout.height);
    svg.setAttribute('viewBox', '0 0 ' + layout.width + ' ' + layout.height);
    svg.setAttribute('data-lineage-rendered', 'unified-dag');
    if (layout.cycle_detected) svg.setAttribute('data-cycle-detected', 'true');
    svg.replaceChildren();
    if (projection.empty || projection.invalid) {
      var text = svgEl('text');
      text.setAttribute('class', 'lineage-empty-state');
      text.setAttribute('x', 16);
      text.setAttribute('y', 32);
      text.textContent = projection.note || 'No lineage nodes to render';
      svg.appendChild(text);
      return;
    }
    (projection.edges || []).forEach(function (edge) {
      var source = layout.positions[edge.source];
      var target = layout.positions[edge.target];
      if (!source || !target) return;
      var path = svgEl('path');
      path.setAttribute('fill', 'none');
      var edgeClass = edge.basis === 'recorded' ? 'lineage-edge arch-edge' : 'lineage-edge lineage-edge-chronological';
      if (edge.type === 'context' || edge.type === 'collapsed') edgeClass += ' lineage-context-edge';
      path.setAttribute('class', edgeClass);
      path.setAttribute('data-edge-type', edge.type || 'canonical');
      path.setAttribute('data-basis', edge.basis || 'recorded');
      path.setAttribute('data-source', edge.source);
      path.setAttribute('data-target', edge.target);
      path.setAttribute('data-path', (edge.path || [edge.source, edge.target]).join('->'));
      if (edge.type === 'collapsed') {
        path.setAttribute('data-collapsed-nodes', String(edge.collapsedNodes || 0));
        path.setAttribute('data-collapsed-edges', String(edge.collapsedEdges || 0));
      }
      if (edge.basis !== 'recorded') path.setAttribute('stroke-dasharray', edge.basis === 'mixed' ? '2 3 8 3' : '6 5');
      var mid = (source.y + target.y) / 2;
      path.setAttribute('d', 'M' + source.x + ' ' + source.y + 'L' + source.x + ' ' + mid + 'L' + target.x + ' ' + mid + 'L' + target.x + ' ' + target.y);
      svg.appendChild(path);
      if (edge.type === 'collapsed') {
        var label = svgEl('text');
        label.setAttribute('class', 'lineage-collapsed-label');
        label.setAttribute('x', (source.x + target.x) / 2);
        label.setAttribute('y', mid - 4);
        label.setAttribute('text-anchor', 'middle');
        label.textContent = String(edge.collapsedNodes || 0) + ' hidden nodes';
        svg.appendChild(label);
      }
    });
    var visible = projection.visible || {};
    (projection.nodes || []).forEach(function (node) {
      var pos = layout.positions[node.node_id];
      if (!pos) return;
      if (node.current || (payload.current_node_id && node.node_id === payload.current_node_id)) {
        var star = svgEl('text');
        star.setAttribute('class', 'arch-star');
        star.setAttribute('x', pos.x);
        star.setAttribute('y', pos.y - 14);
        star.setAttribute('text-anchor', 'middle');
        star.textContent = '★';
        svg.appendChild(star);
      }
      if (node.parent_known === false) {
        var unknown = svgEl('text');
        unknown.setAttribute('class', 'lineage-hidden-parent');
        unknown.setAttribute('x', pos.x);
        unknown.setAttribute('y', pos.y - (node.current ? 26 : 14));
        unknown.setAttribute('text-anchor', 'middle');
        unknown.textContent = 'unknown parent';
        svg.appendChild(unknown);
      }
      var circle = svgEl('circle');
      var cid = node.cycle_id || node.node_id;
      var context = state.mode !== 'all' && !visible[node.node_id];
      circle.setAttribute('class', 'arch-node arch-' + (node.outcome || 'integrated') + ' lineage-node' + (context ? ' lineage-context-node' : ''));
      if (context) circle.setAttribute('data-context', (projection.descendantHits && projection.descendantHits[node.node_id] && Object.keys(projection.descendantHits[node.node_id]).length >= 2) ? 'junction' : 'ancestor');
      circle.setAttribute('data-cycle-id', cid);
      circle.setAttribute('data-node-id', node.node_id);
      if (node.sha) circle.setAttribute('data-sha', node.sha);
      if (node.ts_status === 'invalid') circle.setAttribute('data-ts-status', 'invalid');
      circle.setAttribute('cx', pos.x);
      circle.setAttribute('cy', pos.y);
      circle.setAttribute('r', String(RADIUS));
      circle.setAttribute('tabindex', '0');
      circle.setAttribute('role', 'button');
      circle.setAttribute('aria-label', (node.title || cid) + ' — click for details');
      circle.setAttribute('id', nodeIdToDomId(node.node_id));
      var title = svgEl('title');
      title.textContent = node.title || cid;
      circle.appendChild(title);
      svg.appendChild(circle);
    });
  }
  function applyFilter(mode) {
    var payload = state.payload;
    var svg = document.getElementById('lineage-svg');
    if (!payload || !svg) return;
    state.mode = mode || 'all';
    setActiveButton(state.mode);
    var win = windowForMode(state.mode, payload);
    var projection = projectUnifiedGraph(payload, { window: win });
    showNote(projection.note || win.note || '');
    renderUnified(svg, payload, projection);
  }
  function selectNode(token) {
    var payload = state.payload;
    if (!payload) return null;
    var nodeId = resolveToken(payload, token);
    if (!nodeId) {
      var cov = payload.coverage || {};
      showNote('Node ' + token + ' not found in loaded history (coverage: ' + (cov.from_ts || '?') + ' to ' + (cov.to_ts || '?') + ')');
      return null;
    }
    var targetNode = (payload.nodes || []).filter(function (node) { return node.node_id === nodeId; })[0];
    if (state.mode !== 'all' && targetNode && !inWindow(targetNode, windowForMode(state.mode, payload))) {
      // A deep link must not leave the user looking at an active filter that
      // hides its target. Make the visible filter state honest first.
      applyFilter('all');
    }
    var el = document.getElementById(nodeIdToDomId(nodeId));
    if (!el) { applyFilter('all'); el = document.getElementById(nodeIdToDomId(nodeId)); }
    if (!el) return null;
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return el;
  }
  function selectNodeFromHash(hash) {
    if (!hash || String(hash).indexOf('#node-') !== 0) return null;
    return selectNode(String(hash).slice(6));
  }
  function start() {
    var script = document.getElementById('lineage-data');
    var svg = document.getElementById('lineage-svg');
    if (!script || !svg) return;
    try { state.payload = JSON.parse(script.textContent); } catch (error) { window.__lineageRendererError = String(error); return; }
    document.querySelectorAll('[data-lineage-filter]').forEach(function (button) {
      button.addEventListener('click', function () { applyFilter(button.getAttribute('data-lineage-filter')); });
    });
    applyFilter((document.querySelector('.lineage-unified-graph') || svg).getAttribute('data-lineage-default-mode') || 'yesterday-today');
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      var node = document.activeElement && document.activeElement.closest('.lineage-node');
      if (node) { event.preventDefault(); node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); }
    });
    window.__lineageRendererLoaded = true;
  }
  window.lineageRenderer = {
    nodeIdToDomId: nodeIdToDomId,
    domIdToNodeId: domIdToNodeId,
    projectUnifiedGraph: projectUnifiedGraph,
    renderUnified: renderUnified,
    applyFilter: applyFilter,
    selectNode: selectNode,
    selectNodeFromHash: selectNodeFromHash
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}());
