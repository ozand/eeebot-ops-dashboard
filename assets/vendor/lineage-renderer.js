(function () {
  'use strict';
  // #208: the lineage renderer. No dependencies. The generator emits one payload per
  // UTC day: nodes[] with sha, cycle_id, parent, parent_basis ('recorded' | 'inferred'),
  // optional parent_day (recorded parent lives in an earlier day), ts, outcome, kind,
  // optional title, optional current. Every parent here was decided by the generator's
  // single parent expression; the renderer never invents an edge. Edge provenance is
  // carried into the element: inferred edges are dashed `lineage-edge-chronological`,
  // recorded edges are solid `arch-edge`, and both carry data-basis.
  //
  // Layout: a tidy top-down forest. Each subtree occupies a disjoint x-interval and a
  // parent is centred over its children, so edges of different subtrees never cross —
  // the crossing minimisation d3-dag was vendored for is a property of this layout,
  // not a computation, which is why the 390 KB of d3 + d3-dag were dropped (#208).
  var PITCH_X = 36;   // horizontal distance between neighbouring leaves
  var PITCH_Y = 48;   // vertical distance between depths
  var MARGIN_X = 20;
  var MARGIN_TOP = 28; // room for the ★ / ↩ marks above the first row
  var RADIUS = 9;

  function layoutDay(payload) {
    var nodes = (payload && payload.nodes) || [];
    var byId = {};
    nodes.forEach(function (node) { byId[node.sha] = node; });
    var children = {};
    var roots = [];
    nodes.forEach(function (node) {
      var parent = node.parent;
      if (parent && byId[parent] && parent !== node.sha) {
        (children[parent] || (children[parent] = [])).push(node.sha);
      } else {
        roots.push(node.sha);
      }
    });
    // Children keep the generator's chronological order (nodes[] is sorted by ts).
    var width = {};
    function measure(sha, guard) {
      if (guard[sha]) return 1;
      guard[sha] = true;
      var kids = children[sha] || [];
      var total = 0;
      kids.forEach(function (kid) { total += measure(kid, guard); });
      width[sha] = Math.max(1, total);
      return width[sha];
    }
    roots.forEach(function (root) { measure(root, {}); });
    var positions = {};
    var maxDepth = 0;
    function place(sha, left, depth, guard) {
      if (guard[sha]) return;
      guard[sha] = true;
      var span = width[sha] || 1;
      positions[sha] = {
        x: MARGIN_X + (left + span / 2) * PITCH_X,
        y: MARGIN_TOP + depth * PITCH_Y,
        depth: depth
      };
      if (depth > maxDepth) maxDepth = depth;
      var cursor = left;
      (children[sha] || []).forEach(function (kid) {
        place(kid, cursor, depth + 1, guard);
        cursor += width[kid] || 1;
      });
    }
    var cursor = 0;
    roots.forEach(function (root) {
      place(root, cursor, 0, {});
      cursor += width[root] || 1;
    });
    // A parent cycle (a→b→a) makes every member a non-root and nothing above
    // would place it. Re-root anything still unplaced so a bad payload draws
    // every node instead of a blank svg, and say so on the counter.
    nodes.forEach(function (node) {
      if (positions[node.sha]) return;
      window.__lineageRendererReRooted = (window.__lineageRendererReRooted || 0) + 1;
      measure(node.sha, {});
      place(node.sha, cursor, 0, {});
      cursor += width[node.sha] || 1;
    });
    var totalUnits = Math.max(1, cursor);
    var svgWidth = Math.max(180, Math.round(MARGIN_X * 2 + totalUnits * PITCH_X));
    var svgHeight = Math.max(72, MARGIN_TOP + maxDepth * PITCH_Y + RADIUS + 20);
    var edges = [];
    nodes.forEach(function (node) {
      var parent = node.parent;
      if (!(parent && byId[parent] && parent !== node.sha)) return;
      var source = positions[parent];
      var target = positions[node.sha];
      if (!source || !target) return;
      edges.push({
        source: parent,
        target: node.sha,
        basis: node.parent_basis === 'inferred' ? 'inferred' : 'recorded',
        points: [[source.x, source.y], [source.x, (source.y + target.y) / 2], [target.x, (source.y + target.y) / 2], [target.x, target.y]]
      });
    });
    return { width: svgWidth, height: svgHeight, positions: positions, edges: edges, nodes: nodes };
  }

  function monthDay(day) {
    var parts = String(day || '').split('-');
    var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var month = months[Number(parts[1]) - 1];
    if (parts.length !== 3 || !month || isNaN(Number(parts[2]))) return String(day || '');
    return month + ' ' + String(Number(parts[2])).padStart(2, '0');
  }

  function svgEl(tag) {
    return document.createElementNS('http://www.w3.org/2000/svg', tag);
  }

  function renderDay(svg, payload) {
    window.__lineageRendererAttempts = (window.__lineageRendererAttempts || 0) + 1;
    var nodes = (payload && payload.nodes) || [];
    if (!nodes.length) return;
    var layout = layoutDay(payload);
    svg.setAttribute('width', layout.width);
    svg.setAttribute('height', layout.height);
    svg.setAttribute('viewBox', '0 0 ' + layout.width + ' ' + layout.height);
    svg.replaceChildren();
    svg.setAttribute('data-lineage-rendered', 'lineage-tree');
    layout.edges.forEach(function (edge) {
      var path = svgEl('path');
      path.setAttribute('fill', 'none');
      path.setAttribute('class', edge.basis === 'inferred' ? 'lineage-edge lineage-edge-chronological' : 'lineage-edge arch-edge');
      path.setAttribute('data-basis', edge.basis);
      path.setAttribute('data-source', edge.source);
      path.setAttribute('data-target', edge.target);
      if (edge.basis === 'inferred') path.setAttribute('stroke-dasharray', '6 5');
      path.setAttribute('d', edge.points.map(function (point, index) {
        return (index ? 'L' : 'M') + point[0] + ' ' + point[1];
      }).join(' '));
      svg.appendChild(path);
    });
    nodes.forEach(function (node) {
      var position = layout.positions[node.sha];
      if (!position) return;
      var isCurrent = node.current || (payload.current_sha && node.sha === payload.current_sha);
      if (node.parent_day) {
        var stub = svgEl('text');
        stub.setAttribute('class', 'lineage-hidden-parent');
        stub.setAttribute('data-target', node.sha);
        stub.setAttribute('x', position.x);
        // Above the star when the node carries both marks.
        stub.setAttribute('y', position.y - (isCurrent ? 26 : 14));
        stub.setAttribute('text-anchor', 'middle');
        stub.textContent = '↩ from ' + monthDay(node.parent_day);
        svg.appendChild(stub);
      }
      if (isCurrent) {
        var star = svgEl('text');
        star.setAttribute('class', 'arch-star');
        star.setAttribute('x', position.x);
        star.setAttribute('y', position.y - 14);
        star.setAttribute('text-anchor', 'middle');
        star.textContent = '★';
        svg.appendChild(star);
      }
      var circle = svgEl('circle');
      circle.setAttribute('class', 'arch-node arch-' + (node.outcome || 'integrated') + ' lineage-node');
      circle.setAttribute('data-cycle-id', node.cycle_id || node.sha);
      circle.setAttribute('cx', position.x);
      circle.setAttribute('cy', position.y);
      circle.setAttribute('r', String(RADIUS));
      var title = svgEl('title');
      title.textContent = node.title || node.cycle_id || node.sha;
      circle.appendChild(title);
      svg.appendChild(circle);
    });
    // Counted last: a render that threw half-way is not a success.
    window.__lineageRendererSuccesses = (window.__lineageRendererSuccesses || 0) + 1;
  }

  // Day filter (#208 step 7). Calendar-based on the viewer's clock: "today" is the UTC
  // date of `now`, not the newest day that happens to have data, and when the requested
  // window holds no data the note says so instead of silently showing an older day.
  function utcDay(date) { return date.toISOString().slice(0, 10); }
  function selectDays(mode, days, nowValue) {
    var now = nowValue ? new Date(nowValue) : new Date();
    if (isNaN(now.getTime())) return { keep: days.slice(), note: '' };  // unparseable clock: show everything, claim nothing
    var latest = days.length ? days[days.length - 1] : '';
    var today = utcDay(now);
    var yesterday = utcDay(new Date(now.getTime() - 86400000));
    var keep;
    if (mode === 'today') {
      keep = days.filter(function (day) { return day === today; });
    } else if (mode === '24h') {
      // Day-granular by design: the sections are UTC days, so "24h" keeps every
      // day that overlaps [now - 24h, now] — up to two of them.
      var floor = now.getTime() - 86400000;
      keep = days.filter(function (day) {
        var start = Date.parse(day + 'T00:00:00Z');
        return start + 86400000 > floor && start <= now.getTime();
      });
    } else if (mode === 'yesterday-today') {
      keep = days.filter(function (day) { return day === today || day === yesterday; });
    } else {
      // 'range' (and anything unknown) is resolved by attachFilter from the inputs; here it means "no calendar filter".
      keep = days.slice();
    }
    var note = '';
    if (mode !== 'range' && days.indexOf(today) === -1) {
      note = 'No data for ' + today + ' (UTC) yet' + (latest ? '; latest day with data: ' + latest : '') + '.';
    }
    return { keep: keep, note: note };
  }

  function attachFilter(root) {
    var groups = Array.prototype.slice.call(root.querySelectorAll('.lineage-day-group'));
    var days = groups.map(function (group) { return group.getAttribute('data-day'); }).sort();
    var noteEl = document.querySelector('.lineage-filter-note');
    function apply(mode) {
      var keep;
      var note = '';
      if (mode === 'range') {
        var from = document.querySelector('[data-lineage-from]').value;
        var to = document.querySelector('[data-lineage-to]').value;
        keep = days.filter(function (day) { return (!from || day >= from) && (!to || day <= to); });
      } else {
        var selection = selectDays(mode, days);
        keep = selection.keep;
        note = selection.note;
      }
      groups.forEach(function (group) { group.hidden = keep.indexOf(group.getAttribute('data-day')) === -1; });
      if (noteEl) { noteEl.textContent = note; noteEl.hidden = !note; }
      document.querySelectorAll('[data-lineage-filter]').forEach(function (button) {
        button.classList.toggle('active', button.getAttribute('data-lineage-filter') === mode);
      });
    }
    document.querySelectorAll('[data-lineage-filter]').forEach(function (button) {
      button.addEventListener('click', function () { apply(button.getAttribute('data-lineage-filter')); });
    });
    apply(root.getAttribute('data-lineage-default-mode') || 'yesterday-today');
  }

  function start() {
    document.querySelectorAll('.lineage-day-data').forEach(function (script) {
      var section = script.closest('.lineage-day-group');
      var svg = section && section.querySelector('svg[data-lineage-renderer]');
      if (!svg) return;
      try { renderDay(svg, JSON.parse(script.textContent)); } catch (_error) { window.__lineageRendererError = String(_error); }
    });
    var root = document.querySelector('.lineage-day-groups');
    if (root) attachFilter(root);
  }

  window.lineageRenderer = { layoutDay: layoutDay, renderDay: renderDay };
  window.lineageDayFilter = { select: selectDays };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  window.__lineageRendererLoaded = true;
}());
