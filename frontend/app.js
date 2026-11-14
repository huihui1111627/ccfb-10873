/* ClaimNet frontend: views, graph rendering, workbench. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  view: 'dashboard',
  overview: null,
  gangs: [],
  gangFilter: { minScore: 0, status: '', analysis: '' },
  cy: null,
  egoSeed: null,
  egoDepth: 2,
  nodeTypes: ['person', 'vehicle', 'phone', 'bank_card', 'address', 'repair_shop'],
  selectedCase: null,
  compare: null,
  model: null,
};

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { }
    throw new Error(msg);
  }
  return res.json();
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body ?? {}) });

function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = 'toast'), 2600);
}
const fmt = (n) => (n == null ? '–' : Number(n).toLocaleString('zh-CN'));
const money = (n) => (n == null ? '–' : '¥' + Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 }));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const scoreClass = (s) => s >= 80 ? 'high' : s >= 60 ? 'mid' : 'low';
const scoreColor = (s) => s >= 80 ? '#ef4444' : s >= 60 ? '#f59e0b' : '#06b6d4';

/* ------------------------------------------------------------------ views */
const TITLES = {
  dashboard: '总览', import: '案件导入', network: '关系网络', gangs: '团伙列表',
  patterns: '模式识别', changes: '变化对比', workbench: '调查工作台',
  evaluation: '评估', settings: '权重与评分',
};

function switchView(name) {
  state.view = name;
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  $$('.view').forEach(v => v.classList.add('hidden'));
  $('#view-' + name).classList.remove('hidden');
  $('#page-title').textContent = TITLES[name];
  if (name === 'network') setTimeout(initCytoscape, 30);
  if (name === 'dashboard') loadDashboard();
  if (name === 'gangs') loadGangs();
  if (name === 'patterns') loadPatterns();
  if (name === 'changes') loadAnalysesForCompare();
  if (name === 'workbench') loadCases();
  if (name === 'evaluation') loadEvaluation();
  if (name === 'settings') loadSettings();
  if (name === 'import') loadImports();
}

/* ------------------------------------------------------------------ dashboard */
async function loadDashboard() {
  const data = await api('/api/overview');
  state.overview = data;
  const latest = data.latest_analysis;
  const kpis = [
    { label: '理赔记录', value: fmt(data.counts.claims), cls: '', sub: '查勘 ' + fmt(data.counts.surveys) },
    { label: '涉案人员', value: fmt(data.counts.persons_with_claims), cls: '' },
    { label: '识别团伙', value: latest ? fmt(latest.stats.gangs) : '–', cls: 'danger', sub: '候选 ' + (latest ? latest.stats.candidates : '–') },
    { label: '修理厂', value: fmt(data.counts.repair_shops), cls: '' },
    { label: '模型版本', value: 'v' + data.model_version, cls: 'ok', sub: '阈值 ' + data.threshold },
  ];
  $('#kpis').innerHTML = kpis.map(k => `
    <div class="kpi ${k.cls}"><div class="label">${k.label}</div>
      <div class="value">${k.value}${k.sub ? `<small>${esc(k.sub)}</small>` : ''}</div></div>`).join('');

  $('#dash-gangs').innerHTML = (latest?.gangs || []).slice(0, 8).map(g => `
    <div class="gang-mini" data-gang="${g.gang_id}">
      <div class="score-badge ${scoreClass(g.score)}">${g.score.toFixed(0)}</div>
      <div class="body">
        <div class="title">${g.gang_id} <span class="tag">${g.size} 人</span>
          ${g.status !== 'open' ? `<span class="tag ${g.status}">${g.status === 'confirmed' ? '已确认' : '已排除'}</span>` : ''}</div>
        <div class="sub">${esc(g.top_reasons.join(' · '))}</div>
      </div>
    </div>`).join('') || '<div class="empty">尚未运行分析</div>';
  $$('#dash-gangs .gang-mini').forEach(el => el.onclick = () => openGangFromAnywhere(el.dataset.gang));

  $('#dash-alerts').innerHTML = (data.alerts || []).slice(0, 8).map(a => `
    <div class="alert ${a.severity}">
      <div><div class="atitle">${esc(a.title)}</div>
      <div class="atime">${esc(a.created_at.slice(0, 16).replace('T', ' '))}</div></div>
    </div>`).join('') || '<div class="empty">暂无变化提示</div>';

  const w = data.feature_weights || {};
  const maxW = Math.max(...Object.values(w), 0.01);
  $('#weight-bars').innerHTML = Object.entries(w).map(([k, v]) => `
    <div class="wb-row"><span>${FEATURE_LABELS[k] || k}</span>
      <div class="wb-track"><i class="wb-fill" style="width:${(v / maxW * 100).toFixed(1)}%"></i></div>
      <span class="wb-val">${v.toFixed(3)}</span></div>`).join('');

  $('#sb-claims').textContent = fmt(data.counts.claims);
  $('#sb-persons').textContent = fmt(data.counts.persons_with_claims);
  $('#sb-gangs').textContent = latest ? latest.stats.gangs : '–';
  $('#sb-model').textContent = data.model_version;
  $('#data-version').textContent = '数据版本 ' + data.counts.data_version;
}

const FEATURE_LABELS = {
  identifier_sharing: '身份关联共用', claim_frequency: '出险频次异常',
  shop_concentration: '修理厂集中', collision_ring: '三者车互碰',
  time_burst: '短期集中出险', payout: '赔付金额异常',
  weak_attachment: '弱关联挂接', network_density: '关联密度', role_swap: '角色互换',
};
const REL_LABELS = {
  shared_phone: '共用电话', shared_bank_card: '共用银行卡', shared_address: '同一住址',
  co_claim: '同一事故', co_repair_shop: '同一修理厂',
  mutual_collision: '三者车互碰', shared_driver: '同车驾驶',
};

/* ------------------------------------------------------------------ import */
async function loadImports() {
  const rows = await api('/api/imports');
  $('#imports-table tbody').innerHTML = rows.map(r => `
    <tr><td>${esc(r.name)}</td><td>${esc(r.created_at.slice(0, 16).replace('T', ' '))}</td>
      <td>${fmt(r.policy_count)}</td><td>${fmt(r.claim_count)}</td><td>${fmt(r.survey_count)}</td></tr>`
  ).join('') || '<tr><td colspan="5" class="hint">暂无导入记录</td></tr>';
}

async function importFiles(files) {
  const byName = {};
  for (const f of files) {
    const key = f.name.replace(/\.json$/i, '').toLowerCase();
    const map = { policies: 'policies', policy: 'policies', claims: 'claims', claim: 'claims', surveys: 'surveys', survey: 'surveys', shops: 'shops', shop: 'shops' };
    const slot = map[key];
    if (!slot) { toast('无法识别的文件: ' + f.name, true); continue; }
    byName[slot] = JSON.parse(await f.text());
  }
  if (!Object.keys(byName).length) return;
  const res = await post('/api/import', { name: 'manual-' + Date.now(), ...byName });
  $('#import-status').textContent =
    `已导入：保单 ${res.policies} · 理赔 ${res.claims} · 查勘 ${res.surveys} · 修理厂 ${res.shops}`;
  toast('导入完成，数据版本 ' + res.data_version);
  loadImports(); loadDashboard();
}

/* ------------------------------------------------------------------ network */
const KIND_COLORS = {
  person: '#ef4444', vehicle: '#3b82f6', phone: '#8b5cf6', bank_card: '#06b6d4',
  address: '#eab308', repair_shop: '#10b981', claim: '#64748b', survey: '#94a3b8',
};
const KIND_LABELS = {
  person: '人员', vehicle: '车辆', phone: '电话', bank_card: '银行卡',
  address: '地址', repair_shop: '修理厂', claim: '赔案', survey: '查勘',
};

function nodeSize(n) {
  if (n.kind === 'person') return 22 + Math.min(14, (n.claims || 0) * 2);
  if (n.kind === 'repair_shop') return 26;
  if (n.kind === 'vehicle') return 16;
  if (n.kind === 'claim') return 9;
  return 12;
}

function toElements(payload) {
  const nodes = payload.nodes.map(n => ({
    data: {
      id: n.id, label: n.label, kind: n.kind, raw: n,
    },
    position: undefined,
  }));
  const edges = payload.edges.map(e => ({
    data: {
      id: e.id, source: e.source, target: e.target,
      weight: e.weight || 1, relation: e.relation,
      strong: e.weight >= 0.5,
    },
  }));
  return { nodes, edges };
}

/* Deterministic ring layout: BFS from the highest-degree node, one ring per level.
   O(n) and allocation-light, so 1–2k nodes lay out in milliseconds and never
   block the UI (no force simulation). */
function ringLayout(cy, rootId, nodeCount) {
  const nodes = cy.nodes();
  const n = nodes.length;
  if (!n) return {};
  const ids = nodes.map(nd => nd.id());
  const adj = new Map();
  ids.forEach(id => adj.set(id, []));
  cy.edges().forEach(e => {
    const s = e.data('source'), t = e.data('target');
    if (adj.has(s) && adj.has(t)) {
      const ws = adj.get(s), wt = adj.get(t);
      if (ws.length < 400) ws.push(t);
      if (wt.length < 400) wt.push(s);
    }
  });

  // BFS over every connected component, each seeded by its local hub, so the
  // levels are true graph distance rather than insertion order.
  const level = new Map();
  const comps = [];
  const seen = new Set();
  const hubOf = (start) => {
    // local hub = highest degree inside this component (bounded scan)
    let hub = start, best = -1;
    const q = [start], loc = new Set([start]);
    for (let i = 0; i < q.length && i < 4000; i++) {
      const cur = q[i];
      const d = adj.get(cur).length;
      if (d > best) { best = d; hub = cur; }
      for (const x of adj.get(cur)) if (!loc.has(x)) { loc.add(x); q.push(x); }
    }
    return { hub, members: q };
  };
  for (const id of ids) {
    if (seen.has(id)) continue;
    const { hub, members } = hubOf(id);
    members.forEach(m => seen.add(m));
    const root = (rootId && members.includes(rootId)) ? rootId : hub;
    level.set(root, 0);
    const q = [root];
    for (let i = 0; i < q.length; i++) {
      const cur = q[i];
      for (const nb of adj.get(cur)) {
        if (!level.has(nb)) { level.set(nb, level.get(cur) + 1); q.push(nb); }
      }
    }
    comps.push(root);
  }

  // group by level, then place each level on a ring sized so that adjacent
  // nodes keep a minimum arc distance — this is what stops large rings from
  // bunching into a hairball.
  const byLevel = new Map();
  ids.forEach(id => {
    const lv = level.has(id) ? level.get(id) : 0;
    if (!byLevel.has(lv)) byLevel.set(lv, []);
    byLevel.get(lv).push(id);
  });
  const maxLevel = Math.max(...byLevel.keys(), 0);
  const nodeGap = 46;
  const pos = {};
  // radius of a level is driven by its own node count and the cumulative
  // circumference needed by inner levels
  let radius = 0;
  const radii = [];
  for (let lv = 0; lv <= maxLevel; lv++) {
    const count = (byLevel.get(lv) || []).length;
    if (lv === 0) { radii[0] = 0; continue; }
    const needed = Math.max(120, (count * nodeGap) / (2 * Math.PI));
    radius = Math.max(radius + 110, needed);
    radii[lv] = radius;
  }
  // size the rings around the largest component; smaller components get their
  // own offset further out so they stay readable
  // Order nodes within a level by their parent's angle so children stay near
  // the parent they hang off — the standard radial-tree trick that removes most
  // crossing edges without a force simulation.
  const parentAngle = new Map([[comps[0] || ids[0], 0]]);
  const orderKey = (id) => parentAngle.has(id) ? parentAngle.get(id) : 0;
  byLevel.forEach((members, lv) => {
    if (lv === 0) { members.forEach(id => { pos[id] = { x: 0, y: 0 }; parentAngle.set(id, 0); }); return; }
    members.sort((a, b) => orderKey(a) - orderKey(b));
    const r = radii[lv] || 160;
    const step = (Math.PI * 2) / Math.max(members.length, 6);
    members.forEach((id, i) => {
      const a = i * step + lv * 0.35;
      const x = Math.cos(a) * r;
      const y = Math.sin(a) * r;
      pos[id] = { x, y };
      parentAngle.set(id, a);
    });
  });
  return pos;
}

/* Cytoscape renders text for every labelled node; above ~600 nodes that alone
   costs more than the whole layout.  Hide labels on big graphs and only reveal
   the hovered / selected neighbourhood, which keeps pan and zoom smooth. */
function applyLabelPolicy(cy) {
  const n = cy.nodes().length;
  if (n > 600) {
    cy.style().selector('node').style({ 'label': '', 'font-size': 0 }).update();
    cy.style().selector('node:selected, node.hover').style({
      'label': 'data(label)', 'font-size': 10,
    }).update();
    cy.off('mouseover.labelpolicy').on('mouseover.labelpolicy', 'node', evt => {
      evt.target.addClass('hover');
    });
    cy.off('mouseout.labelpolicy').on('mouseout.labelpolicy', 'node', evt => {
      evt.target.removeClass('hover');
    });
  } else {
    cy.style().selector('node').style({ 'label': 'data(label)', 'font-size': 9 }).update();
  }
}

function initCytoscape() {
  if (state.cy) { state.cy.resize(); return; }
  state.cy = cytoscape({
    container: $('#cy'),
    style: [
      { selector: 'node', style: {
          'background-color': ele => KIND_COLORS[ele.data('kind')] || '#64748b',
          'label': 'data(label)', 'font-size': 9, 'color': '#dbe7fb',
          'text-outline-color': '#0b1220', 'text-outline-width': 2,
          'width': ele => nodeSize(ele.data('raw') || {}),
          'height': ele => nodeSize(ele.data('raw') || {}),
          'border-width': 1, 'border-color': 'rgba(255,255,255,.18)',
        }},
      { selector: 'node[kind="person"]', style: { 'shape': 'ellipse' } },
      { selector: 'node[kind="vehicle"]', style: { 'shape': 'round-rectangle' } },
      { selector: 'node[kind="repair_shop"]', style: { 'shape': 'diamond' } },
      { selector: 'node[kind="claim"]', style: { 'shape': 'round-rectangle' } },
      { selector: 'node[kind="phone"], node[kind="bank_card"]', style: { 'shape': 'hexagon' } },
      { selector: 'node[kind="address"]', style: { 'shape': 'triangle' } },
      { selector: 'edge', style: {
          'width': ele => 0.6 + Math.min(3.2, (ele.data('weight') || 1) * 1.4),
          'line-color': ele => ele.data('strong') ? 'rgba(239,68,68,.6)' : 'rgba(70,96,140,.55)',
          'curve-style': 'bezier', 'opacity': 0.75,
        }},
      { selector: 'node:selected', style: {
          'border-width': 3, 'border-color': '#facc15',
        }},
      { selector: 'node.dim', style: { 'opacity': 0.18 } },
      { selector: 'edge.dim', style: { 'opacity': 0.08 } },
    ],
    wheelSensitivity: 0.22,
    minZoom: 0.05, maxZoom: 3,
    hideEdgesOnViewport: true,
    textureOnViewport: true,
    motionBlur: false,
  });
  state.cy.on('tap', 'node', evt => onNodeTap(evt.target));
  state.cy.on('tap', evt => { if (evt.target === state.cy) closeDetail(); });
}

async function loadEgo(seed, depth, merge = false) {
  initCytoscape();
  const cy = state.cy;
  const types = state.nodeTypes.join(',');
  const payload = await api(`/api/ego?node=${encodeURIComponent(seed)}&depth=${depth}&types=${types}`);
  if (payload.error) { toast(payload.error, true); return; }
  const el = toElements(payload);
  if (!merge) {
    cy.elements().remove();
    cy.add(el.nodes.concat(el.edges));
  } else {
    const existing = new Set(cy.nodes().map(n => n.id()));
    const newNodes = el.nodes.filter(n => !existing.has(n.data.id));
    cy.add(newNodes);
    const existingE = new Set(cy.edges().map(e => e.id()));
    cy.add(el.edges.filter(e => !existingE.has(e.data.id)));
  }
  // Deterministic concentric layout for every size: a force simulation over
  // 1.5k nodes costs ~12 s, which is unusable, while the radial layout is O(n)
  // and finishes instantly.  Angular ordering below keeps edge crossings down.
  const pos = ringLayout(cy, payload.seed, cy.nodes().length);
  cy.layout({ name: 'preset', positions: pos, fit: true, padding: 40 }).run();
  cy.fit(undefined, 40);
  applyLabelPolicy(cy);
  $('#net-status').textContent =
    `${payload.node_count} 节点 · ${payload.edge_count} 关系 · ${depth} 度` +
    (payload.window_days ? ` · 近 ${payload.window_days} 天` : '') +
    (payload.truncated ? ' · 已截断，请缩小类型或度数' : '');
  state.egoSeed = payload.seed;
  state.egoDepth = depth;
  if (payload.truncated) toast('节点过多，已截断显示，可减少节点类型或度数', true);
}

function onNodeTap(node) {
  const id = node.id();
  const raw = node.data('raw') || {};
  state.cy.elements().addClass('dim');
  node.removeClass('dim');
  node.connectedEdges().removeClass('dim');
  node.connectedEdges().connectedNodes().removeClass('dim');
  showNodeDetail(id, raw);
}

function showNodeDetail(id, raw) {
  const box = $('#net-detail');
  box.classList.remove('hidden');
  const kind = raw.kind;
  let body = '';
  if (kind === 'person') {
    const rows = {
      '身份证': raw.id_no, '出险次数': raw.claims, '赔付合计': money(raw.payout),
      '作为被保险人': raw.as_insured, '作为驾驶员': raw.as_driver, '作为三者车': raw.as_third,
      '首次出险': raw.first_claim, '最近出险': raw.last_claim,
    };
    body = Object.entries(rows).map(([k, v]) => `<div class="kv"><span>${k}</span><b>${esc(v)}</b></div>`).join('');
    body += `<div style="margin-top:10px;display:flex;gap:6px">
      <button class="btn" data-expand="${esc(id)}">展开 1 度</button>
      <button class="btn ghost" data-person="${esc(raw.id_no)}">档案</button></div>`;
  } else if (kind === 'repair_shop') {
    body = `<div class="kv"><span>修理厂 ID</span><b>${esc(raw.value)}</b></div>`;
    body += `<button class="btn" style="margin-top:10px" data-shop="${esc(raw.value)}">查看集中出险</button>`;
  } else {
    const v = raw.value || raw.id_no || id;
    body = `<div class="kv"><span>类型</span><b>${KIND_LABELS[kind] || kind}</b></div>
      <div class="kv"><span>值</span><b>${esc(v)}</b></div>`;
  }
  box.innerHTML = `<div style="display:flex;justify-content:space-between">
      <h3>${esc(raw.label || id)}</h3>
      <button class="btn ghost" style="padding:2px 8px" onclick="closeDetail()">×</button></div>
      <div class="hint" style="margin-bottom:8px">${KIND_LABELS[kind] || kind}</div>${body}`;
  $$('[data-expand]', box).forEach(b => b.onclick = async () => {
    await loadEgo(b.dataset.expand, 1, true);
    toast('已展开一跳邻居');
  });
  $$('[data-person]', box).forEach(b => b.onclick = () => openPersonProfile(b.dataset.person));
  $$('[data-shop]', box).forEach(b => b.onclick = () => openShopProfile(b.dataset.shop));
}
window.closeDetail = () => $('#net-detail').classList.add('hidden');

async function loadFullGraph() {
  initCytoscape();
  const types = state.nodeTypes.join(',');
  const [payload, gangs] = await Promise.all([
    api(`/api/graph?limit=2500&focus=risk&types=${types}`),
    api('/api/gangs'),
  ]);
  const el = toElements(payload);
  const gangOf = new Map();
  gangs.forEach((g, gi) => (g.members || []).forEach(m => gangOf.set('person:' + m.id_no, gi)));
  const palette = ['#ef4444','#f97316','#eab308','#22c55e','#14b8a6','#0ea5e9','#6366f1','#a855f7','#ec4899','#f43f5e'];
  el.nodes.forEach(nd => {
    if (nd.data.kind === 'person' && gangOf.has(nd.data.id)) {
      nd.data.gangIdx = gangOf.get(nd.data.id);
      nd.data.gangColor = palette[gangOf.get(nd.data.id) % palette.length];
    }
  });
  state.cy.elements().remove();
  state.cy.add(el.nodes.concat(el.edges));
  state.cy.style().selector('node[gangIdx]').style({ 'background-color': 'data(gangColor)' }).update();

  // Cluster layout: members of each gang sit on a small disc; every entity
  // (vehicle / shop / phone / card / address) attaches to the disc of the gang
  // it serves most, so relations stay local instead of forming a second
  // hairball ring.
  const pos = {};
  const gangKeys = [];
  const membersByGang = new Map();
  el.nodes.forEach(nd => {
    if (nd.data.gangIdx != null) {
      const k = nd.data.gangIdx;
      if (!membersByGang.has(k)) { membersByGang.set(k, []); gangKeys.push(k); }
      membersByGang.get(k).push(nd.data.id);
    }
  });
  const entityHome = new Map();   // entity id -> gangIdx (majority vote)
  el.edges.forEach(e => {
    const s = state.cy.getElementById(e.data.source);
    const t = state.cy.getElementById(e.data.target);
    const si = s.data('gangIdx'), ti = t.data('gangIdx');
    const sKind = s.data('kind'), tKind = t.data('kind');
    let entity = null, gang = null;
    if (si != null && tKind !== 'person') { entity = e.data.target; gang = si; }
    if (ti != null && sKind !== 'person') { entity = e.data.source; gang = ti; }
    if (entity == null) return;
    if (!entityHome.has(entity)) entityHome.set(entity, new Map());
    const votes = entityHome.get(entity);
    votes.set(gang, (votes.get(gang) || 0) + 1);
  });
  const homeOf = new Map();
  entityHome.forEach((votes, entity) => {
    let best = -1, bestN = -1;
    votes.forEach((n, g) => { if (n > bestN) { bestN = n; best = g; } });
    homeOf.set(entity, best);
  });

  // pack gang discs on a square grid: big gangs get bigger cells, and the
  // cell pitch is derived from the largest disc so nothing overlaps
  const gangSizes = gangKeys.map(k => (membersByGang.get(k) || []).length);
  const maxDisc = Math.max(60, ...gangSizes.map(n => 26 + Math.sqrt(n) * 7 + 60));
  const cols = Math.ceil(Math.sqrt(gangKeys.length));
  const pitch = maxDisc * 1.9;
  const discs = {};
  gangKeys.forEach((k, gi) => {
    const col = gi % cols, row = Math.floor(gi / cols);
    discs[k] = { x: col * pitch, y: row * pitch, a: 0 };
  });
  const R = pitch * cols;
  gangKeys.forEach(k => {
    const members = membersByGang.get(k) || [];
    const rr = 26 + Math.sqrt(members.length) * 7;
    members.forEach((id, i) => {
      const aa = (i / Math.max(1, members.length)) * Math.PI * 2;
      pos[id] = { x: discs[k].x + Math.cos(aa) * rr, y: discs[k].y + Math.sin(aa) * rr };
    });
  });
  // entities orbit their home disc on a slightly larger radius
  const entityBuckets = new Map();
  homeOf.forEach((g, entity) => {
    if (g == null) return;
    if (!entityBuckets.has(g)) entityBuckets.set(g, []);
    entityBuckets.get(g).push(entity);
  });
  entityBuckets.forEach((ents, g) => {
    const disc = discs[g];
    if (!disc) return;
    const members = membersByGang.get(g) || [];
    const base = 26 + Math.sqrt(members.length) * 7 + 34;
    ents.forEach((id, i) => {
      const a = (i / Math.max(1, ents.length)) * Math.PI * 2;
      const r = base + (i % 2) * 22;
      pos[id] = { x: disc.x + Math.cos(a) * r, y: disc.y + Math.sin(a) * r };
    });
  });
  // leftovers (no gang vote at all) go to a wide outer ring
  const strays = el.nodes.filter(nd => pos[nd.data.id] === undefined).map(nd => nd.data.id);
  strays.forEach((id, i) => {
    const a = (i / Math.max(1, strays.length)) * Math.PI * 2;
    pos[id] = { x: Math.cos(a) * (R + 240), y: Math.sin(a) * (R + 240) };
  });
  state.cy.layout({ name: 'preset', positions: pos, fit: true, padding: 30 }).run();
  state.cy.fit(undefined, 30);
  applyLabelPolicy(state.cy);
  $('#net-status').textContent =
    `${payload.nodes.length} 节点 · ${payload.edges.length} 关系 · ${gangs.length} 个团伙（仅涉案实体）`
    + (payload.truncated ? ' · 已截断' : '');
}

/* ------------------------------------------------------------------ gangs */
async function loadGangs() {
  const params = new URLSearchParams();
  if (state.gangFilter.analysis) params.set('analysis_id', state.gangFilter.analysis);
  if (state.gangFilter.status) params.set('status', state.gangFilter.status);
  params.set('min_score', state.gangFilter.minScore);
  const gangs = await api('/api/gangs?' + params.toString());
  state.gangs = gangs;
  if (!state.gangFilter.analysis) {
    const opts = await api('/api/analyses');
    const sel = $('#gang-analysis');
    sel.innerHTML = '<option value="">最新批次</option>' +
      opts.map(a => `<option value="${a.analysis_id}">#${a.analysis_id} · ${a.created_at.slice(0, 16).replace('T', ' ')} · ${a.gangs} 团伙</option>`).join('');
  }
  $('#gang-grid').innerHTML = gangs.map(g => {
    const patterns = (g.patterns || []).map(p => p.label).join(' · ');
    return `<div class="gang-card" data-gang="${g.gang_id}" data-case="${g.case_id || ''}">
      <div class="score-side"><div class="big-score" style="color:${scoreColor(g.score)}">${g.score.toFixed(1)}</div>
        <div class="score-label">风险分</div></div>
      <div class="top"><div>
        <div class="gname">${g.gang_id}
          <span class="tag">${g.size} 人</span>
          ${g.status !== 'open' ? `<span class="tag ${g.status}">${g.status === 'confirmed' ? '已确认' : '已排除'}</span>` : ''}
        </div>
        <div class="meta">核心 ${(g.members || []).filter(m => m.role === 'core').length} ·
          弱挂接 ${(g.members || []).filter(m => m.role === 'pendant').length} ·
          ${(g.evidence || []).length} 条关系证据</div>
      </div></div>
      <div class="reasons">
        ${(g.reasons || []).slice(0, 3).map(r => `<div class="reason">${esc(r.text)}</div>`).join('')}
      </div>
      <div class="progress"><i style="width:${g.score}%;background:${scoreColor(g.score)}"></i></div>
      <div class="meta" style="margin-top:8px">${esc(patterns || '')}</div>
    </div>`;
  }).join('') || '<div class="empty">没有符合条件的团伙</div>';
  $$('.gang-card').forEach(c => c.onclick = () => openGang(c.dataset.gang, c.dataset.case));
}

async function openGangFromAnywhere(gangId) {
  const gang = await api(`/api/gangs/${gangId}`);
  switchView('workbench');
  await loadCases();
  if (gang.case_id) selectCase(gang.case_id);
}

async function openGang(gangId, caseId) {
  const gang = await api(`/api/gangs/${gangId}`);
  switchView('workbench');
  await loadCases();
  if (caseId) selectCase(caseId);
}

function openPersonProfile(idNo) {
  api('/api/person/' + encodeURIComponent(idNo)).then(p => {
    const box = $('#net-detail');
    box.classList.remove('hidden');
    box.innerHTML = `<div style="display:flex;justify-content:space-between">
        <h3>${esc(p.person.name)}</h3>
        <button class="btn ghost" style="padding:2px 8px" onclick="closeDetail()">×</button></div>
      <div class="kv"><span>身份证</span><b>${esc(p.person.id_no)}</b></div>
      <div class="kv"><span>出险</span><b>${p.person.claims} 次</b></div>
      <div class="kv"><span>赔付</span><b>${money(p.person.payout)}</b></div>
      <div class="kv"><span>电话</span><b>${p.person.phones.join(' / ')}</b></div>
      <div class="kv"><span>银行卡</span><b>${p.person.cards.join(' / ')}</b></div>
      <div class="kv"><span>地址</span><b>${esc(p.person.addresses[0] || '–')}</b></div>
      <h3 style="margin-top:14px">关联人员（${p.edges.length}）</h3>
      ${p.edges.slice(0, 12).map(e => `<div class="kv"><span>${esc(e.other_name)}</span>
        <b>${e.relations.map(r => REL_LABELS[r] || r).join('、')}</b></div>`).join('')}`;
  });
}

function openShopProfile(shopId) {
  api('/api/shop/' + encodeURIComponent(shopId)).then(s => {
    const box = $('#net-detail');
    box.classList.remove('hidden');
    box.innerHTML = `<div style="display:flex;justify-content:space-between">
        <h3>${esc(s.shop.name)}</h3>
        <button class="btn ghost" style="padding:2px 8px" onclick="closeDetail()">×</button></div>
      <div class="kv"><span>理赔</span><b>${s.claim_count} 笔</b></div>
      <div class="kv"><span>赔付</span><b>${money(s.payout)}</b></div>
      <div class="kv"><span>涉及人员</span><b>${s.people} 人</b></div>
      <h3 style="margin-top:12px">重复客户</h3>
      ${s.repeat_customers.slice(0, 8).map(c => `<div class="kv"><span>${esc(c.name)}</span><b>${c.claims} 笔</b></div>`).join('')}
      ${s.bursts.length ? `<div class="hint" style="margin-top:10px;color:#f59e0b">⚠ 集中出险：${esc(s.bursts[0].detail || '')}</div>` : ''}`;
  });
}

/* ------------------------------------------------------------------ patterns */
async function loadPatterns() {
  const [shops, rings] = await Promise.all([api('/api/patterns/shops'), api('/api/patterns/collisions')]);
  $('#pattern-shops').innerHTML = shops.map(s => `
    <div class="pattern"><div class="ptitle">${esc(s.shop_name)}</div>
      <div class="pdet">${s.start} ~ ${s.end}，${s.claim_count} 笔理赔</div>
      <div class="nums"><span>涉及 <b>${s.people}</b> 人</span>
        <span>重复客户 <b>${s.repeat_customers.length}</b></span>
        <span>赔付 <b>${money(s.payout)}</b></span></div></div>`).join('')
    || '<div class="empty">无</div>';
  $('#pattern-rings').innerHTML = rings.map(r => `
    <div class="pattern"><div class="ptitle">${r.members.length} 人互碰闭环</div>
      <div class="pdet">${r.start} ~ ${r.end}</div>
      <div class="nums"><span>理赔 <b>${r.claim_count}</b> 笔</span>
        <span>赔付 <b>${money(r.payout)}</b></span></div></div>`).join('')
    || '<div class="empty">无</div>';
}

/* ------------------------------------------------------------------ changes */
async function loadAnalysesForCompare() {
  const list = await api('/api/analyses');
  const opts = list.map(a => `<option value="${a.analysis_id}">#${a.analysis_id} · ${a.created_at.slice(0, 16).replace('T', ' ')}</option>`).join('');
  $('#cmp-old').innerHTML = opts;
  $('#cmp-new').innerHTML = opts;
  if (list.length >= 2) {
    $('#cmp-new').value = list[0].analysis_id;
    $('#cmp-old').value = list[1].analysis_id;
  }
}

async function compareRuns() {
  const oldId = $('#cmp-old').value, newId = $('#cmp-new').value;
  if (!oldId || !newId) { toast('需要至少两个分析批次', true); return; }
  const cmp = await api(`/api/snapshots/compare?old=${oldId}&new=${newId}`);
  state.compare = cmp;
  const s = cmp.summary;
  $('#compare-summary').innerHTML = [
    { label: '延续团伙', value: s.matched, cls: '' },
    { label: '新增团伙', value: s.new_gangs, cls: 'ok' },
    { label: '消失团伙', value: s.dissolved_gangs, cls: '' },
    { label: '新增成员', value: s.members_added, cls: s.members_added ? 'warn' : '' },
    { label: '移除成员', value: s.members_removed, cls: '' },
  ].map(k => `<div class="kpi ${k.cls}"><div class="label">${k.label}</div><div class="value">${k.value}</div></div>`).join('');

  const parts = [];
  for (const c of cmp.changes) {
    if (c.type === 'matched') {
      parts.push(`<div class="cmp-gang">
        <div class="cmp-head"><div><b>${c.new_gang_id}</b> ← ${c.old_gang_id}
          <span class="tag">匹配度 ${(c.similarity * 100).toFixed(0)}%</span></div>
          <div>${c.old_score} → <b style="color:${scoreColor(c.new_score)}">${c.new_score}</b>
            <span class="hint">${c.score_delta >= 0 ? '+' : ''}${c.score_delta}</span></div></div>
        <div class="cmp-members">
          ${c.removed.map(m => `<span class="member-chip removed">${esc(m.name)}</span>`).join('')}
          ${c.added.map(m => `<span class="member-chip added">+ ${esc(m.name)}（${m.role === 'core' ? '核心' : '弱挂接'}）</span>`).join('')}
          ${!c.added.length && !c.removed.length ? '<span class="hint">成员无变化</span>' : ''}
        </div></div>`);
    } else if (c.type === 'new') {
      parts.push(`<div class="cmp-gang" style="border-left:3px solid #22c55e">
        <div class="cmp-head"><b>新团伙 ${c.new_gang_id}</b>
          <span>${c.new_size} 人 · ${c.new_score} 分</span></div>
        <div class="cmp-members">${c.members.map(m => `<span class="member-chip added">${esc(m.name)}</span>`).join('')}</div></div>`);
    } else if (c.type === 'dissolved') {
      parts.push(`<div class="cmp-gang" style="border-left:3px solid #64748b">
        <div class="cmp-head"><b>${c.old_gang_id}</b> 未复现
          <span>${c.old_size} 人 · ${c.old_score} 分</span></div></div>`);
    }
  }
  $('#compare-body').innerHTML = parts.join('') || '<div class="empty">无变化</div>';
}

/* ------------------------------------------------------------------ workbench */
async function loadCases() {
  const raw = await api('/api/cases');
  // cases are stable per organisation now; keep the newest entry per case
  const seen = new Map();
  for (const c of raw) if (!seen.has(c.case_id)) seen.set(c.case_id, c);
  const cases = [...seen.values()];
  $('#case-list').innerHTML = cases.map(c => `
    <div class="case-item ${c.case_id === state.selectedCase ? 'active' : ''}" data-case="${c.case_id}">
      <div class="ct"><span>${esc(c.gang?.gang_id || '—')}</span>
        <span class="tag ${c.status}">${c.status === 'open' ? '待调查' : c.status === 'confirmed' ? '已确认' : '已排除'}</span></div>
      <div class="cm">${c.gang ? `${c.gang.size} 人 · ${c.gang.score.toFixed(1)} 分` : ''}</div>
      <div class="cm">${esc(c.case_id)}</div>
    </div>`).join('') || '<div class="empty">暂无案件</div>';
  $$('.case-item').forEach(el => el.onclick = () => selectCase(el.dataset.case));
  if (!state.selectedCase && cases.length) selectCase(cases[0].case_id);
}

async function selectCase(caseId) {
  state.selectedCase = caseId;
  $$('.case-item').forEach(el => el.classList.toggle('active', el.dataset.case === caseId));
  const rep = await api(`/api/cases/${caseId}/report`);
  renderCase(rep);
}

function weakBasis(m) {
  if (!m.weak_link || !m.weak_link.length) return '—';
  const groups = {};
  for (const l of m.weak_link) {
    const rel = (l.relations || []).map(r => REL_LABELS[r] || r).join('+');
    (groups[rel] = groups[rel] || []).push(l.core_member_name);
  }
  return esc(Object.entries(groups)
    .map(([rel, names]) => `${rel} → ${names.length > 3 ? names.length + ' 名核心成员' : names.join('、')}`)
    .join('；'));
}

function renderCase(rep) {
  const statusTag = rep.status === 'confirmed' ? '已确认' : rep.status === 'excluded' ? '已排除' : '待调查';
  const links = rep.evidence.slice(0, 20).map(e => `
    <div class="evidence-item">
      <div>${esc(e.a_name)} ↔ ${esc(e.b_name)}
        <span class="rel">${e.relations.map(r => r.label).join('、')}</span>
        <span class="hint">权重 ${e.weight}</span></div>
      ${e.items.slice(0, 2).map(it => `<div class="hint">${esc(it.relation)}：${esc(String(it.shared_value).slice(0, 60))}
        ${it.dates?.length ? ' · ' + esc(it.dates.slice(-2).join('、')) : ''}</div>`).join('')}
    </div>`).join('');

  $('#case-main').innerHTML = `
    <div class="case-header">
      <div>
        <h2>${rep.gang_id} <span class="tag ${rep.status}">${statusTag}</span></h2>
        <div class="sub">${rep.case_id} · 批次 #${rep.analysis_id} · ${esc(rep.analysis_created_at.slice(0, 16).replace('T', ' '))}</div>
        <div class="sub" style="margin-top:6px">风险分 <b style="color:${scoreColor(rep.score)};font-size:18px">${rep.score}</b>
          · ${rep.totals.members} 人（核心 ${rep.totals.core} / 弱挂接 ${rep.totals.pendants}）
          · ${rep.totals.claims} 笔理赔 · ${money(rep.totals.payout)}</div>
      </div>
      <div class="case-actions">
        <button class="btn" id="btn-report-md">导出报告</button>
        <button class="btn" id="btn-net-this">关系网络</button>
      </div>
    </div>

    <h3 class="sub">风险评分依据</h3>
    <div class="reason-block">
      ${rep.reasons.map(r => `<div class="reason-item">
        <span class="pts">+${r.points}</span>
        <span><b>${esc(r.label || r.feature)}</b>：${esc(r.text)}</span></div>`).join('')}
    </div>

    <h3 class="sub">作案模式</h3>
    <div class="reason-block">
      ${rep.patterns.length ? rep.patterns.map(p => `<div class="reason-item">
        <span class="pts" style="color:#fbbf24">${esc(p.label)}</span>
        <span>${esc(p.detail)}</span></div>`).join('') : '<div class="hint">未识别到显著模式</div>'}
    </div>

    <h3 class="sub">成员（${rep.members.length}）</h3>
    <table class="member-table"><thead><tr>
      <th>姓名</th><th>角色</th><th>出险</th><th>赔付</th><th>身份证</th><th>挂接依据</th>
    </tr></thead><tbody>
      ${rep.members.map(m => `<tr>
        <td><span class="link" data-person="${esc(m.id_no)}">${esc(m.name)}</span></td>
        <td>${m.role === 'core' ? '<span class="tag">核心</span>' : '<span class="tag" style="color:#fbbf24;border-color:#78350f">弱挂接</span>'}</td>
        <td>${m.claims ?? '–'}</td><td>${money(m.payout)}</td>
        <td style="font-family:ui-monospace,monospace;font-size:10.5px">${esc(m.id_no)}</td>
        <td class="hint">${weakBasis(m)}</td>
      </tr>`).join('')}
    </tbody></table>

    <h3 class="sub" style="margin-top:20px">关系证据链</h3>
    ${links || '<div class="hint">无</div>'}

    <h3 class="sub" style="margin-top:20px">关联理赔（${rep.claims.length}）</h3>
    <table class="member-table"><thead><tr>
      <th>赔案号</th><th>出险日期</th><th>类型</th><th>赔付</th><th>修理厂</th><th>驾驶员</th><th>三者</th>
    </tr></thead><tbody>
      ${rep.claims.slice(0, 40).map(c => `<tr>
        <td style="font-family:ui-monospace,monospace;font-size:10.5px">${esc(c.claim_no)}</td>
        <td>${esc(c.loss_date)}</td><td>${esc(c.accident_type || '')}</td>
        <td>${money(c.payout)}</td><td>${esc(c.shop_name || '')}</td>
        <td>${esc(c.driver_name || '')}</td><td>${esc(c.third_name || '')}</td>
      </tr>`).join('')}
    </tbody></table>

    <h3 class="sub" style="margin-top:20px">调查结论</h3>
    ${rep.feedback.length ? rep.feedback.map(f => `<div class="hint">${esc(f.created_at.slice(0, 16))} · ${esc(f.analyst)} · ${f.decision === 'confirm' ? '确认' : '排除'}${f.note ? ' · ' + esc(f.note) : ''}</div>`).join('') : '<div class="hint">尚未标记</div>'}
    <div class="feedback-box">
      <input id="feedback-note" placeholder="调查备注（可选）">
      <button class="btn" id="btn-confirm" style="border-color:#166534;color:#86efac">确认为欺诈</button>
      <button class="btn ghost" id="btn-exclude" style="border-color:#7f1d1d;color:#fca5a5">排除</button>
    </div>
    <div class="hint" id="feedback-result" style="margin-top:8px"></div>
  `;
  $$('[data-person]', $('#case-main')).forEach(el => el.onclick = () => openPersonProfile(el.dataset.person));
  $('#btn-report-md').onclick = () => window.open(`/api/cases/${rep.case_id}/report?download=true&fmt=md`, '_blank');
  $('#btn-net-this').onclick = () => {
    const core = rep.members.find(m => m.role === 'core') || rep.members[0];
    switchView('network');
    $('#ego-node').value = 'person:' + core.id_no;
    setTimeout(() => loadEgo('person:' + core.id_no, 2), 60);
  };
  const send = (decision) => async () => {
    const note = $('#feedback-note').value;
    const btn = decision === 'confirm' ? $('#btn-confirm') : $('#btn-exclude');
    btn.disabled = true;
    try {
      const res = await post(`/api/cases/${rep.case_id}/feedback`,
                             { case_id: rep.case_id, decision, note });
      const summary = `已${decision === 'confirm' ? '确认' : '排除'}，模型升级至 v${res.model_version}，` +
        res.changes.slice(0, 3).map(c => `${c.label} ${c.before}→${c.after}`).join('；');
      // re-render first (it rebuilds this entire panel), then write the result
      await selectCase(rep.case_id);
      await loadCases();
      const box = $('#feedback-result');
      if (box) box.textContent = summary;
      toast('反馈已应用，模型 v' + res.model_version);
    } catch (e) {
      $('#feedback-result').textContent = '操作失败：' + e.message;
      toast('反馈失败：' + e.message, true);
      btn.disabled = false;
    }
  };
  $('#btn-confirm').onclick = send('confirm');
  $('#btn-exclude').onclick = send('exclude');
}

/* ------------------------------------------------------------------ evaluation */
async function loadEvaluation() {
  let res;
  try { res = await api('/api/evaluation'); } catch (e) {
    $('#eval-summary').innerHTML = `<div class="hint">${esc(e.message)}</div>`;
    return;
  }
  $('#eval-summary').innerHTML = [
    { label: '团伙精确率', value: (res.gang_level.strict.precision * 100).toFixed(1) + '%', cls: 'ok' },
    { label: '团伙召回率', value: (res.gang_level.strict.recall * 100).toFixed(1) + '%', cls: 'ok' },
    { label: '团伙 F1', value: (res.gang_level.strict.f1 * 100).toFixed(1) + '%', cls: '' },
    { label: '人员精确率', value: (res.person_level.precision * 100).toFixed(1) + '%', cls: '' },
    { label: '人员召回率', value: (res.person_level.recall * 100).toFixed(1) + '%', cls: '' },
    { label: '弱挂接召回', value: (res.weak_pendants.recall * 100).toFixed(1) + '%', cls: 'warn' },
  ].map(k => `<div class="kpi ${k.cls}"><div class="label">${k.label}</div><div class="value">${k.value}</div></div>`).join('');

  const truth = await api('/api/evaluation/truth');
  const missed = new Set(res.missed_gangs.map(g => g.gang_id));
  $('#eval-truth').innerHTML = truth.g.map(g => {
    const miss = missed.has(g.gang_id);
    return `<div class="eval-row ${miss ? 'miss' : 'hit'}">
      <span>${g.gang_id}</span>
      <div class="bar"><i style="width:${miss ? 0 : 100}%"></i></div>
      <span class="hint">${g.style} · ${g.size}人</span></div>`;
  }).join('');
  $('#eval-fp').innerHTML = res.false_positive_gangs.map(g => `
    <div class="eval-row miss"><span>${g.gang_id}</span>
      <div class="bar"><i style="width:100%"></i></div>
      <span class="hint">${g.score}分 · ${g.size}人</span></div>
    <div class="hint" style="padding:0 10px 6px">${esc(g.reasons[0] || '')}</div>`).join('')
    || '<div class="empty">无误报</div>';
}

/* ------------------------------------------------------------------ settings */
async function loadSettings() {
  const model = await api('/api/model');
  state.model = model;
  $('#affinity-editor').innerHTML = Object.entries(model.affinity_weights).map(([k, v]) => `
    <div class="we-row"><span>${REL_LABELS[k] || k}</span>
      <input type="number" step="0.05" min="0" max="1" data-aff="${k}" value="${v}">
      <span class="hint">${v >= 0.5 ? '强' : '弱'}</span></div>`).join('');
  $('#feature-editor').innerHTML = Object.entries(model.feature_weights).map(([k, v]) => `
    <div class="we-row"><span>${FEATURE_LABELS[k] || k}</span>
      <input type="number" step="0.01" min="0" max="0.6" data-feat="${k}" value="${v}">
      <span class="hint">${(v * 100).toFixed(0)}%</span></div>`).join('');
  $('#threshold-input').value = model.threshold;
  const h = model.recent_history || [];
  $('#model-history').innerHTML = h.length ? h.slice().reverse().map(x => `
    <div class="history-item"><span>v${x.from_version} → v${x.to_version} ·
      ${x.decision === 'confirm' ? '确认' : '排除'} ${x.gang_id}</span>
      <span class="chg">${x.changes.slice(0, 3).map(c => c.label + ' ' + c.delta).join('，')}</span></div>`).join('')
    : '<div class="hint">尚无反馈校准记录。在调查工作台标记案件后会出现在这里。</div>';
}

/* ------------------------------------------------------------------ boot */
$$('.nav-item').forEach(b => b.onclick = () => switchView(b.dataset.view));

$('#btn-refresh').onclick = () => { loadDashboard(); if (state.view !== 'dashboard') switchView(state.view); };
$('#btn-analyse').onclick = async () => {
  $('#btn-analyse').disabled = true;
  try {
    const res = await post('/api/analyse', {});
    toast(`分析完成：${res.stats.gangs} 个团伙（${res.stats.duration_ms} ms）`);
    if (res.changes) {
      const s = res.changes.summary;
      toast(`较上次：新增团伙 ${s.new_gangs}，新增成员 ${s.members_added}`);
    }
    state.cy = null;
    await loadDashboard();
  } catch (e) { toast('分析失败：' + e.message, true); }
  $('#btn-analyse').disabled = false;
};

$('#pick-files').onclick = () => $('#file-input').click();
$('#file-input').onchange = e => importFiles(e.target.files);
const dz = $('#drop-zone');
dz.ondragover = e => { e.preventDefault(); dz.classList.add('dragover'); };
dz.ondragleave = () => dz.classList.remove('dragover');
dz.ondrop = e => { e.preventDefault(); dz.classList.remove('dragover'); importFiles(e.dataTransfer.files); };
$('#btn-load-raw').onclick = async () => {
  const res = await post('/api/import/raw');
  toast(`已加载 ${res.claims} 笔理赔`);
  loadImports(); loadDashboard();
};
$('#btn-reset').onclick = async () => {
  if (!confirm('清空数据库？')) return;
  await post('/api/reset');
  toast('数据库已清空');
  loadDashboard();
};

$$('#type-chips .chip').forEach(chip => chip.onclick = () => {
  chip.classList.toggle('on');
  state.nodeTypes = $$('#type-chips .chip.on').map(c => c.dataset.type);
});
$('#btn-ego').onclick = () => {
  const raw = $('#ego-node').value.trim();
  if (!raw) return toast('请输入起点节点', true);
  loadEgo(raw, Number($('#ego-depth').value));
};
$('#ego-node').onkeydown = e => { if (e.key === 'Enter') $('#btn-ego').onclick(); };
$('#btn-full-graph').onclick = () => loadFullGraph();

$('#gang-min-score').oninput = e => { $('#gang-min-score-val').textContent = e.target.value; };
$('#gang-min-score').onchange = e => { state.gangFilter.minScore = e.target.value; loadGangs(); };
$('#gang-status').onchange = e => { state.gangFilter.status = e.target.value; loadGangs(); };
$('#gang-analysis').onchange = e => { state.gangFilter.analysis = e.target.value; loadGangs(); };

$('#btn-compare').onclick = compareRuns;

$('#btn-save-affinity').onclick = async () => {
  const payload = { affinity_weights: {}, affinity_windows: {} };
  $$('[data-aff]').forEach(i => payload.affinity_weights[i.dataset.aff] = Number(i.value));
  await post('/api/model/weights', payload);
  toast('权重已保存，将按新权重重算');
  await post('/api/analyse', {});
  switchView('settings');
};
$('#btn-save-features').onclick = async () => {
  const payload = { feature_weights: {}, threshold: Number($('#threshold-input').value) };
  $$('[data-feat]').forEach(i => payload.feature_weights[i.dataset.feat] = Number(i.value));
  await post('/api/model/weights', payload);
  toast('评分权重已保存');
  await post('/api/analyse', {});
  switchView('settings');
};

loadDashboard();
