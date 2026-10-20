import {api} from './api.js';
import {h} from './components.js';
import {t} from './i18n/zh.js';

const SVG = 'http://www.w3.org/2000/svg';
const NODE_WIDTH = 188;
const COLUMN = 236;
const ROW = 58;
const PAD = 16;

function svg(tag, attrs = {}, ...children) {
  const element = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) if (value != null) element.setAttribute(key, String(value));
  element.append(...children);
  return element;
}

// Wrap a label to two lines of roughly 13 CJK characters (or ~24 Latin).
function lines(label) {
  const text = String(label || '');
  const limit = /[　-鿿]/.test(text) ? 13 : 24;
  if (text.length <= limit) return [text];
  const second = text.slice(limit, limit * 2);
  return [text.slice(0, limit), text.length > limit * 2 ? `${second.slice(0, -1)}…` : second];
}

// Tidy left-to-right tree: leaves stack vertically, parents centre on their children.
function layout(nodes) {
  const byId = new Map(nodes.map(node => [node.id, {...node, children: []}]));
  const roots = [];
  for (const node of byId.values()) {
    const parent = node.parent && byId.get(node.parent);
    if (parent && parent !== node) parent.children.push(node); else roots.push(node);
  }
  let next = 0;
  const place = (node, depth) => {
    node.x = PAD + depth * COLUMN;
    if (!node.children.length) { node.y = PAD + next * ROW; next += 1; return; }
    node.children.forEach(child => place(child, depth + 1));
    node.y = (node.children[0].y + node.children[node.children.length - 1].y) / 2;
  };
  roots.forEach(root => place(root, 0));
  const placed = [...byId.values()];
  const width = Math.max(...placed.map(node => node.x)) + NODE_WIDTH + PAD;
  const height = Math.max(PAD * 2 + ROW, next * ROW + PAD * 2);
  return {placed, width, height};
}

export function drawMindMap(nodes, {seen = new Set(), onSelect} = {}) {
  const {placed, width, height} = layout(nodes);
  const byId = new Map(placed.map(node => [node.id, node]));
  const edges = svg('g', {class: 'mm-edges'});
  const boxes = svg('g', {class: 'mm-nodes'});
  for (const node of placed) {
    const parent = node.parent && byId.get(node.parent);
    if (parent) {
      const x1 = parent.x + NODE_WIDTH, y1 = parent.y + 20, x2 = node.x, y2 = node.y + 20;
      const mid = (x1 + x2) / 2;
      edges.append(svg('path', {d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`, class: `mm-edge mm-${node.status}`}));
    }
    const labelLines = lines(node.label);
    const group = svg('g', {
      class: `mm-node mm-kind-${node.kind} mm-${node.status}${seen.has(node.id) ? '' : ' mm-new'}`,
      transform: `translate(${node.x},${node.y})`, tabindex: '0', role: 'button',
      'aria-label': `${node.label}${node.note ? `：${node.note}` : ''}`,
    });
    group.append(
      svg('title', {}, node.note ? `${node.label}\n${node.note}` : node.label),
      svg('rect', {width: NODE_WIDTH, height: 40, rx: 8}),
      svg('text', {x: 12, y: labelLines.length > 1 ? 17 : 24},
        ...labelLines.map((line, index) => svg('tspan', {x: 12, dy: index ? 14 : 0}, line))),
    );
    if (node.kind !== 'root' && node.kind !== 'reference') {
      group.append(svg('text', {x: NODE_WIDTH - 8, y: 12, class: 'mm-tag', 'text-anchor': 'end'}, t(`mindmap.kind.${node.kind}`)));
    }
    const select = () => onSelect?.(node);
    group.addEventListener('click', select);
    group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
    boxes.append(group);
  }
  return svg('svg', {class: 'mindmap-svg', width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': t('mindmap.title')}, edges, boxes);
}

// A self-refreshing mind map panel: drag with the left button to pan, wheel to zoom.
export function liveMindMap(ctx, projectId, {compact = false} = {}) {
  const canvas = h('div', {class: `mindmap-canvas${compact ? ' compact' : ''}`, style: `height:${compact ? 340 : 620}px`});
  const detail = h('div', {class: 'mindmap-detail muted'}, t('mindmap.hint'));
  const stamp = h('small', {class: 'muted'});
  const seen = new Set();
  let signature = '';
  const view = {scale: 1, x: 0, y: 0, touched: false};
  const chart = () => canvas.querySelector('svg');
  const apply = () => {
    const element = chart();
    if (element) element.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  };
  const fit = () => {
    const element = chart();
    if (!element) return;
    const width = Number(element.getAttribute('width'));
    const height = Number(element.getAttribute('height'));
    view.scale = Math.min(1.2, Math.max(0.15, Math.min((canvas.clientWidth - 24) / width, (canvas.clientHeight - 24) / height)));
    view.x = (canvas.clientWidth - width * view.scale) / 2;
    view.y = (canvas.clientHeight - height * view.scale) / 2;
    view.touched = false;
    apply();
  };
  const zoomAt = (factor, cx = canvas.clientWidth / 2, cy = canvas.clientHeight / 2) => {
    const next = Math.min(3, Math.max(0.15, view.scale * factor));
    view.x = cx - (cx - view.x) * (next / view.scale);
    view.y = cy - (cy - view.y) * (next / view.scale);
    view.scale = next;
    view.touched = true;
    apply();
  };
  canvas.addEventListener('wheel', event => {
    // Beside the chat, a plain wheel scrolls the side column; Ctrl+wheel zooms.
    if (compact && !event.ctrlKey) return;
    event.preventDefault();
    const box = canvas.getBoundingClientRect();
    zoomAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - box.left, event.clientY - box.top);
  }, {passive: false});
  let drag = null;
  canvas.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    drag = {x: event.clientX, y: event.clientY, ox: view.x, oy: view.y, moved: false, id: event.pointerId};
  });
  canvas.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) { drag.moved = true; canvas.setPointerCapture(event.pointerId); canvas.classList.add('dragging'); }
    view.x = drag.ox + dx;
    view.y = drag.oy + dy;
    view.touched = true;
    apply();
  });
  const endDrag = event => {
    if (!drag || event.pointerId !== drag.id) return;
    if (drag.moved) {
      canvas.classList.remove('dragging');
      // Swallow the click that ends a drag so it doesn't select a node.
      canvas.addEventListener('click', stop => { stop.stopPropagation(); stop.preventDefault(); }, {capture: true, once: true});
    }
    drag = null;
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  const tools = h('div', {class: 'toolbar mindmap-zoom'},
    h('button', {type: 'button', class: 'button small', onclick: () => zoomAt(1 / 1.25), 'aria-label': t('mindmap.zoomOut')}, '−'),
    h('button', {type: 'button', class: 'button small', onclick: fit}, t('mindmap.fit')),
    h('button', {type: 'button', class: 'button small', onclick: () => zoomAt(1.25), 'aria-label': t('mindmap.zoomIn')}, '+'));
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { if (!view.touched) fit(); }) : null;
  observer?.observe(canvas);
  ctx.onDispose?.(() => observer?.disconnect());
  const select = node => {
    detail.className = 'mindmap-detail';
    detail.replaceChildren(h('strong', {}, node.label), node.note ? h('p', {}, node.note) : h('p', {class: 'muted'}, t('mindmap.noNote')),
      h('small', {class: 'muted'}, `${t(`mindmap.kind.${node.kind}`)} · ${t(`mindmap.status.${node.status}`)}`));
  };
  async function refresh() {
    const data = await api(`/projects/${encodeURIComponent(projectId)}/mindmap`, {signal: ctx.signal});
    const next = JSON.stringify(data.nodes);
    if (next === signature) return;
    const first = !signature;
    signature = next;
    if (first) data.nodes.forEach(node => seen.add(node.id));
    canvas.replaceChildren(data.nodes.length > 1 ? drawMindMap(data.nodes, {seen, onSelect: select}) : h('p', {class: 'muted mindmap-empty'}, t('mindmap.empty')));
    data.nodes.forEach(node => seen.add(node.id));
    // Keep the user's pan/zoom across live updates; fit only until they move it.
    requestAnimationFrame(() => (view.touched ? apply() : fit()));
    stamp.textContent = data.updated_at ? `${t('mindmap.updated')} ${new Date(data.updated_at).toLocaleTimeString()}` : '';
  }
  void refresh().catch(() => {});
  ctx.poll(refresh, 3000);
  return h('div', {class: 'mindmap stack'},
    h('div', {class: 'row spread wrap'}, h('span', {class: 'mindmap-live'}, h('span', {class: 'pulse', 'aria-hidden': 'true'}), t('mindmap.live'), stamp), tools),
    canvas, h('small', {class: 'muted'}, t(compact ? 'mindmap.controlsCompact' : 'mindmap.controls')), detail);
}
