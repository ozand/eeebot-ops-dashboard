(function () {
  'use strict';

  function renderDay(svg, payload) {
    if (!window.d3 || !window.d3.graphStratify || !window.d3.sugiyama) return;
    var width = Number(svg.getAttribute('width')) || 180;
    var height = Number(svg.getAttribute('height')) || 72;
    var nodes = payload.nodes || [];
    if (!nodes.length) return;
    var records = nodes.map(function (node) {
      return { id: node.sha, parentIds: node.parent ? [node.parent] : [], value: node };
    });
    var dag;
    try {
      dag = window.d3.graphStratify()(records);
      window.d3.sugiyama().size([Math.max(1, height - 40), Math.max(1, width - 40)])(dag);
    } catch (_error) {
      return;
    }
    var byDepth = {};
    dag.nodes().forEach(function (node) {
      var depth = Math.round(node.x);
      (byDepth[depth] || (byDepth[depth] = [])).push(node);
    });
    var depths = Object.keys(byDepth).map(Number).sort(function (a, b) { return a - b; });
    var rowWidth = Math.min(6, Math.max.apply(null, depths.map(function (depth) { return byDepth[depth].length; }).concat([1]))) * 42;
    var rows = Math.max.apply(null, depths.map(function (depth) { return Math.ceil(byDepth[depth].length / 6); }).concat([1]));
    width = Math.max(180, rowWidth + 40);
    height = Math.max(72, depths.length * 50 + rows * 34 + 20);
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    var depthIndex = {};
    depths.forEach(function (depth, index) {
      byDepth[depth].sort(function (a, b) { return a.y - b.y; });
      byDepth[depth].forEach(function (node, indexInDepth) {
        depthIndex[node.data.id] = {
          x: 20 + (indexInDepth % 6) * 42,
          y: 24 + index * 50 + Math.floor(indexInDepth / 6) * 34
        };
      });
    });
    var project = function (node) {
      return depthIndex[node.data.id] || { x: 20, y: 24 };
    };
    svg.replaceChildren();
    dag.links().forEach(function (link) {
      var source = project(link.source);
      var target = project(link.target);
      var points = [
        [source.x, source.y],
        [source.x, (source.y + target.y) / 2],
        [target.x, (source.y + target.y) / 2],
        [target.x, target.y]
      ];
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('class', 'lineage-edge arch-edge');
      path.setAttribute('d', points.map(function (point, index) {
        return (index ? 'L' : 'M') + point[0] + ' ' + point[1];
      }).join(' '));
      svg.appendChild(path);
    });
    dag.nodes().forEach(function (dagNode) {
      var position = project(dagNode);
      var value = dagNode.data.value || {};
      var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('class', 'arch-node arch-' + (value.outcome || 'integrated') + ' lineage-node');
      circle.setAttribute('data-cycle-id', value.cycle_id || value.sha);
      circle.setAttribute('cx', position.x);
      circle.setAttribute('cy', position.y);
      circle.setAttribute('r', '9');
      var title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = value.title || '(untitled cycle)';
      circle.appendChild(title);
      svg.appendChild(circle);
    });
  }

  function start() {
    document.querySelectorAll('.lineage-day-data').forEach(function (script) {
      var section = script.closest('.lineage-day-group');
      var svg = section && section.querySelector('svg[data-lineage-renderer="d3-dag"]');
      if (!svg) return;
      try { renderDay(svg, JSON.parse(script.textContent)); } catch (_error) { /* keep fallback */ }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}());
