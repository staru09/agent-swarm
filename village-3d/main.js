// AI Village 3D: replays any day of the AI Village as a walkable toy town.
// Data: data/index.json and data/days/<date>.json from extract.py; player-card notes load lazily from
// data/days/<date>/<agent>.json. Scenery: town.js. Models: Kenney CC0 kits in assets/.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MapControls } from 'three/addons/controls/MapControls.js';
import { PointerLockControls } from 'three/addons/controls/PointerLockControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { buildTown, camps, SPOTS, PLAZA, WALL } from './town.js';

// Validated categorical palette (dataviz reference, slot order); colour follows the clan, never its rank.
const CLAN_COLOR = { Google: '#2a78d6', Anthropic: '#eb6834', OpenAI: '#1baf7a', Zhipu: '#eda100',
  Moonshot: '#e87ba4', xAI: '#008300', DeepSeek: '#4a3aa7', Meta: '#e34948' };
const ANIM = { W: 'interact-right', T: 'interact-left', H: 'emote-yes', L: 'pick-up', C: 'sit' };
const SKINS = 'abcdefghijklmnopqr';
const CH = 0.38; // character scale: 2.7-unit Kenney figures become ~1 unit, toy-sized next to the houses
const GATE = new THREE.Vector3(0, 0, WALL + 3);

const $ = s => document.querySelector(s);
const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };
const pct = (a, b) => (b ? Math.round(100 * (a || 0) / b) : 0);
const count = (s, ch) => s.split(ch).length - 1;
const fmt = n => n.toLocaleString('en-US');
const pad = n => String(n).padStart(2, '0');
const YMD = { day: 'numeric', month: 'short', year: 'numeric' };
const longDate = (d, o = { weekday: 'short', day: 'numeric', month: 'short' }) => new Date(`${d.slice(0, 10)}T12:00:00Z`).toLocaleDateString('en-US', { ...o, timeZone: 'UTC' });
// Dataset text stays text: **bold** becomes <b> by splitting, never through innerHTML.
const bold = t => t.split('**').map((s, k) => (k % 2 ? el('b', { textContent: s }) : s));
const rich = t => t.split(/\n\s*\n/).map(p => el('p', {}, ...bold(p)));

// ---------- loading ----------
const manager = new THREE.LoadingManager();
manager.onProgress = (_, done, total) => { const bar = $('#loadbar i'); if (bar) bar.style.width = `${(100 * done) / total}%`; };
const loader = new GLTFLoader(manager);
const gltfs = new Map();
const gltf = url => { if (!gltfs.has(url)) gltfs.set(url, loader.loadAsync(url)); return gltfs.get(url); };
const shade = o => { if (o.isMesh) o.castShadow = o.receiveShadow = true; };
function lib(name, fix) { // a placeholder group that fills in when the model arrives
  const g = new THREE.Group();
  gltf(`assets/${name}.glb`).then(m => { const o = m.scene.clone(); o.traverse(shade); fix?.(o); g.add(o); });
  return g;
}

let IX;
try {
  IX = await (await fetch('data/index.json')).json();
} catch {
  $('#loadmsg').replaceChildren('No data/index.json yet. Build it from the AI Village dataset, then serve this folder:',
    el('pre', { textContent: 'cd village-3d\npython3 extract.py\npython3 -m http.server 8000\n# open http://localhost:8000' }));
  throw new Error('data/index.json missing');
}
await document.fonts.load('28px "Lilita One"');

const DAYS = IX.days, DAY = Object.fromEntries(DAYS.map((d, k) => [d.date, { ...d, k }]));
const SLUGS = Object.keys(IX.agents);
const clans = IX.clans.map(name => ({ name, color: CLAN_COLOR[name] || '#8a7a66' }));
// The loaded day; everything below that reads these is rebuilt by loadDay().
let D, M = [], agents = [], END = 1, SLICES = 1, dayPairs = new Map(), dayGroup = null, gaps = [];

// ---------- scene ----------
const canvas = $('#stage');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
const css = new CSS2DRenderer({ element: $('#labels') });
const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0xcdeeff, 90, 200);
scene.add(new THREE.HemisphereLight(0xeaf6ff, 0x5b7d31, 1.7));
const sun = new THREE.DirectionalLight(0xfff0d8, 2.9);
sun.position.set(-30, 52, 34);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
Object.assign(sun.shadow.camera, { left: -44, right: 44, top: 44, bottom: -44, near: 1, far: 150 });
sun.shadow.bias = -0.0005;
sun.shadow.normalBias = 0.03;
scene.add(sun);

const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 600);
const HOME = { pos: new THREE.Vector3(24, 36, 64), target: new THREE.Vector3(0, 0, 4) };
camera.position.copy(HOME.pos);
const controls = new MapControls(camera, canvas);
controls.target.copy(HOME.target);
Object.assign(controls, { enableDamping: true, maxPolarAngle: 1.3, minDistance: 5, maxDistance: 140, zoomToCursor: true });

const town = buildTown(scene, lib, clans); // built once, with a camp for every clan of the whole run
const campFire = Object.fromEntries(camps(clans.length).map(([x, z], i) => [clans[i].name, new THREE.Vector3(x, 0, z + 0.4)]));

// ---------- characters ----------
function paintSkin(orig, color, text) {
  // Kenney blocky skins share one UV layout: torso cross bottom-left, sleeves top of each arm block.
  // The repainted texture clones the original to keep its glTF sampler settings, with its own image source.
  const img = orig.image, c = el('canvas', { width: 512, height: 512 }), g = c.getContext('2d'), k = 0.5;
  g.drawImage(img, 0, 0, 512, 512);
  const shirt = [[0, 688, 448, 336], [480, 534, 64, 64], [480, 598, 256, 107], [768, 534, 64, 64], [768, 598, 256, 107]];
  g.fillStyle = color;
  for (const r of shirt) g.fillRect(...r.map(v => v * k));
  g.globalAlpha = 0.28;
  g.globalCompositeOperation = 'luminosity'; // keep a hint of the original pockets and belts
  for (const r of shirt) g.drawImage(img, ...r, ...r.map(v => v * k));
  g.globalAlpha = 1;
  g.globalCompositeOperation = 'source-over';
  g.font = `${text.length > 3 ? 21 : 27}px "Lilita One"`; // chest print on the torso front face
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.lineWidth = 5; g.strokeStyle = 'rgba(40,20,5,.7)'; g.fillStyle = '#fff';
  g.strokeText(text, 80, 420); g.fillText(text, 80, 420);
  const t = orig.clone();
  t.source = new THREE.TextureSource(c);
  t.needsUpdate = true;
  return t;
}

const pickables = [];
function spawn(a) {
  a.root = new THREE.Group();
  a.root.visible = false;
  dayGroup.add(a.root);
  gltf(`assets/characters/character-${SKINS[a.skin]}.glb`).then(m => {
    if (a.gone) return; // the day changed while the model loaded
    const body = m.scene.clone();
    let orig;
    body.traverse(o => { if (o.isMesh) orig ||= o.material.map; });
    a.mat = new THREE.MeshStandardMaterial({ map: paintSkin(orig, a.color, a.label), roughness: 0.42 }); // shiny toy plastic
    body.traverse(o => { if (o.isMesh) { o.material = a.mat; o.castShadow = true; o.userData.agent = a.i; pickables.push(o); } });
    body.scale.setScalar(CH);
    a.root.add(body);
    a.mixer = new THREE.AnimationMixer(body);
    a.clips = Object.fromEntries(m.animations.map(c => [c.name, a.mixer.clipAction(c)]));
    a.anim = null;
    play(a, a.want || 'idle');
  });
  const tag = el('div', { className: 'tag', textContent: a.label, title: `${a.name} · ${a.role || a.clan}` });
  tag.style.background = a.color;
  tag.onclick = () => select(a.i);
  a.tag = tag;
  a.tagObj = new CSS2DObject(tag);
  a.tagObj.position.y = 1.2;
  a.bubble = el('div', { className: 'bubble' });
  a.bubble.onclick = () => select(a.i);
  a.bubbleObj = new CSS2DObject(a.bubble);
  a.bubbleObj.position.y = 1.2;
  a.bubbleObj.visible = false;
  a.root.add(a.tagObj, a.bubbleObj);
  a.yaw = 0;
}

function play(a, name) {
  a.want = name;
  if (!a.clips || a.anim === name) return;
  a.clips[name].reset().fadeIn(0.25).play();
  a.clips[a.anim]?.fadeOut(0.25);
  a.anim = name;
}

// Agents at the same place stand in a sunflower spiral in the building's yard, or sit in a ring at their clan's fire.
function slotOf(a, k, n) {
  if (a.where === 'C') {
    const f = campFire[a.clan], th = -2.2 + (4.4 * (k + 0.5)) / n, r = 1.35 + n * 0.06;
    return { pos: new THREE.Vector3(f.x + Math.sin(th) * r, 0, f.z + Math.cos(th) * r), look: f };
  }
  const s = SPOTS[a.where], r = 0.95 * Math.sqrt(k + 0.5), th = k * 2.39996;
  return { pos: new THREE.Vector3(s.yard[0] + Math.cos(th) * r, 0, s.yard[1] + Math.sin(th) * r), look: new THREE.Vector3(s.at[0], 0, s.at[1]) };
}

function place(slice, jump) {
  const groups = {};
  for (const a of agents) {
    a.where = a.track[slice] || '-';
    if (a.where === '-') { a.root.visible = false; a.target = null; continue; }
    if (!a.root.visible) { a.root.visible = true; a.root.position.copy(GATE); } // newcomers walk in through the gate
    (groups[a.where === 'C' ? `C${a.clan}` : a.where] ||= []).push(a);
  }
  for (const list of Object.values(groups)) list.forEach((a, k) => { a.target = slotOf(a, k, list.length); });
  if (jump) for (const a of agents) if (a.target) a.root.position.copy(a.target.pos);
}

// ---------- mention arcs ----------
const beamMats = [2, 3.5, 6].map(w => new LineMaterial({ linewidth: w, vertexColors: true, transparent: true, opacity: 0.92, depthWrite: false }));
const beams = beamMats.map(m => { const l = new LineSegments2(new LineSegmentsGeometry(), m); l.frustumCulled = false; l.renderOrder = 2; scene.add(l); return l; });
let livePairs = new Map();
function computeLive(v) { // mentions in the last village hour
  livePairs = new Map();
  for (let k = lowerBound(v - 3600); k < M.length && M[k][0] <= v; k++)
    for (const d of M[k][3]) livePairs.set(M[k][1] * 64 + d, (livePairs.get(M[k][1] * 64 + d) || 0) + 1);
}
const cA = new THREE.Color(), cB = new THREE.Color(), white = new THREE.Color('#fff'), P0 = new THREE.Vector3(), P1 = new THREE.Vector3(), Q = new THREE.Vector3();
function drawBeams() {
  const mode = +$('#beams').value;
  const pairs = mode === 1 ? livePairs : mode === 2 ? dayPairs : new Map();
  const cut = mode === 1 ? [1, 2, 4] : [1, 6, 20]; // mention-count buckets -> line width
  const buf = [[], [], []], col = [[], [], []];
  for (const [key, n] of pairs) {
    const s = agents[Math.floor(key / 64)], d = agents[key % 64];
    if (state.sel !== null && s.i !== state.sel && d.i !== state.sel) continue;
    if (state.sel === null && n < (mode === 2 ? 3 : 2)) continue; // without a selection, only repeated ties
    if (!s.root.visible || !d.root.visible) continue;
    const b = n >= cut[2] ? 2 : n >= cut[1] ? 1 : 0;
    P0.copy(s.root.position).setY(1.05); P1.copy(d.root.position).setY(1.05);
    const lift = 1.2 + P0.distanceTo(P1) * 0.28;
    cA.set(s.color); cB.copy(cA).lerp(white, 0.55); // lightens toward the mentioned agent: direction
    let prev = P0.clone();
    for (let t = 1; t <= 14; t++) {
      const u = t / 14;
      Q.lerpVectors(P0, P1, u).setY(Q.y + Math.sin(Math.PI * u) * lift);
      buf[b].push(prev.x, prev.y, prev.z, Q.x, Q.y, Q.z);
      const c0 = cA.clone().lerp(cB, (t - 1) / 14), c1 = cA.clone().lerp(cB, u);
      col[b].push(c0.r, c0.g, c0.b, c1.r, c1.g, c1.b);
      prev = Q.clone();
    }
  }
  beams.forEach((l, b) => {
    l.visible = buf[b].length > 0;
    if (!l.visible) return;
    l.geometry.dispose();
    l.geometry = new LineSegmentsGeometry().setPositions(buf[b]).setColors(col[b]);
  });
}

// ---------- stats plaza: one LEGO brick column per agent ----------
const METRICS = [
  ['Computer actions', a => a.stats.turns || 0],
  ['Chat messages', a => a.stats.messages || 0],
  ['Times mentioned by others', a => a.stats.mentions_in || 0],
  ['Mentions of others', a => a.stats.mentions_out || 0],
  ['Bash share of actions (%)', a => pct(a.stats.W, a.stats.turns)],
  ['Actions with errors (%)', a => pct(a.stats.errors, a.stats.turns)],
  ['Time idle at camp (%)', a => pct(count(a.track, 'C'), SLICES - count(a.track, '-'))],
  ['Memory consolidations', a => a.stats.memory || 0],
];
$('#metric').append(...METRICS.map(([n], i) => el('option', { value: i, textContent: n })));
const MAXB = 12, BS = 5.2; // bricks per full column, brick scale
const plaza = { cols: [] };
const riserMat = new THREE.MeshStandardMaterial({ color: 0xcfc3a8, roughness: 0.95 });
{
  const sign = el('div', { className: 'sign', textContent: '🧱 Hall of Records', title: 'One LEGO column per agent; pick the measure under "Plaza"' });
  sign.onclick = () => flyTo(new THREE.Vector3(PLAZA[0], 0, PLAZA[1]), 22);
  plaza.sign = new CSS2DObject(sign);
  plaza.sign.position.set(PLAZA[0] - 3, 8, PLAZA[1] - 5.5);
  scene.add(plaza.sign);
  gltf('assets/bricks/bevel-hq-brick-2x2.glb').then(m => {
    let geo;
    m.scene.traverse(o => { if (o.isMesh) geo = o.geometry; });
    geo.computeBoundingBox();
    plaza.step = (geo.boundingBox.max.y - geo.boundingBox.min.y) * BS * 0.84; // studs nest into the brick above
    plaza.mesh = new THREE.InstancedMesh(geo, new THREE.MeshStandardMaterial({ roughness: 0.3 }), SLUGS.length * MAXB); // room for every agent of the run
    plaza.mesh.castShadow = plaza.mesh.receiveShadow = true;
    scene.add(plaza.mesh);
    buildPlaza();
  });
}
function buildColumns() { // per day; risers and labels live in dayGroup
  plaza.cols = [];
  // rows: clans by size, next-fit so a clan never splits across rows
  const bySize = clans.map(c => ({ ...c, list: agents.filter(a => a.clan === c.name) })).filter(c => c.list.length).sort((x, y) => y.list.length - x.list.length);
  const rows = [[]];
  for (const c of bySize) {
    if (rows.at(-1).length && rows.at(-1).reduce((n, g) => n + g.list.length, 0) + c.list.length > 12) rows.push([]);
    rows.at(-1).push(c);
  }
  rows.forEach((row, r) => {
    const z = PLAZA[1] + 3.3 - r * 3.3, y = r * 0.75;
    const n = row.reduce((s, g) => s + g.list.length, 0), w = n * 1.1 + (row.length - 1) * 0.5;
    const riser = new THREE.Mesh(new THREE.BoxGeometry(w + 1.2, y + 0.16, 2.4), riserMat);
    riser.position.set(PLAZA[0], (y + 0.16) / 2, z);
    riser.castShadow = riser.receiveShadow = true;
    riser.userData.own = true;
    dayGroup.add(riser);
    let x = PLAZA[0] - w / 2 + 0.55;
    for (const g of row) {
      for (const a of g.list) {
        const lab = el('div', { className: 'col-label' });
        const obj = new CSS2DObject(lab);
        dayGroup.add(obj);
        plaza.cols.push({ a, x, z, y: y + 0.16, lab, obj });
        x += 1.1;
      }
      x += 0.5;
    }
  });
  buildPlaza();
}
function buildPlaza() {
  if (!plaza.mesh) return;
  const [name, f] = METRICS[+$('#metric').value];
  const vals = plaza.cols.map(c => f(c.a)), max = Math.max(1, ...vals);
  const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3(BS, BS, BS), p = new THREE.Vector3(), col = new THREE.Color();
  let n = 0;
  plaza.inst = [];
  plaza.cols.forEach((c, i) => {
    const bricks = vals[i] ? Math.max(1, Math.round((vals[i] / max) * MAXB)) : 0;
    for (let b = 0; b < bricks; b++, n++) {
      plaza.mesh.setMatrixAt(n, m4.compose(p.set(c.x, c.y + b * plaza.step, c.z), q, s));
      plaza.mesh.setColorAt(n, col.set(c.a.color).offsetHSL(0, 0, b % 2 ? 0.035 : 0));
      plaza.inst[n] = i;
    }
    c.value = vals[i];
    c.lab.replaceChildren(c.a.label, el('small', { textContent: fmt(vals[i]) + (name.includes('%') ? '%' : '') }));
    c.obj.position.set(c.x, c.y + bricks * plaza.step + 0.55, c.z);
  });
  plaza.mesh.count = n;
  plaza.mesh.instanceMatrix.needsUpdate = true;
  if (plaza.mesh.instanceColor) plaza.mesh.instanceColor.needsUpdate = true;
  plaza.mesh.computeBoundingSphere();
}

// ---------- building signs ----------
const signs = {};
for (const [key, s] of Object.entries(SPOTS)) {
  if (!s.at) continue;
  const e = el('div', { className: 'sign', title: s.what });
  e.onclick = () => flyTo(new THREE.Vector3(s.yard[0], 0, s.yard[1]), 20);
  const o = new CSS2DObject(e);
  o.position.set(s.at[0], s.sign, s.at[1]);
  scene.add(o);
  signs[key] = e;
}

// ---------- UI ----------
const state = { v: 0, slice: -1, playing: true, speed: 300, sel: null, mp: 0, fly: null, names: true, tab: 'today', clan: null, open: new Set() };
const hm = v => { const m = Math.round(D.open * 60) + Math.floor(v / 60); return `${pad(Math.floor(m / 60) % 24)}:${pad(m % 60)}`; };
function lowerBound(v) { let lo = 0, hi = M.length; while (lo < hi) { const mid = (lo + hi) >> 1; if (M[mid][0] < v) lo = mid + 1; else hi = mid; } return lo; }

const time = $('#time');
const counters = [
  ['💬', 'msgs today', () => lowerBound(state.v + 1), null],
  ...['W', 'T', 'H', 'L', 'C'].map(k => [SPOTS[k].icon, SPOTS[k].name.toLowerCase(), () => agents.filter(a => a.where === k).length, k]),
].map(([icon, label, f, k]) => {
  const b = el('b'), node = el('div', { className: 'counter', title: k ? `Agents at the ${label} now (${SPOTS[k].what})` : 'Chat messages so far today' },
    el('div', { className: 'ico', textContent: icon }), b, el('span', { textContent: label }));
  if (k && k !== 'C') { node.style.cursor = 'pointer'; node.onclick = () => signs[k].click(); }
  $('#counters').append(node);
  return () => { b.textContent = fmt(f()); };
});

const playBtn = $('#play');
const setPlaying = p => { state.playing = p; playBtn.textContent = p ? '❚❚ Pause' : '▶ Play'; };
playBtn.onclick = () => { if (state.v >= END - 1) setV(0, true); setPlaying(!state.playing); };
$('#speed').onchange = e => { state.speed = +e.target.value; };
time.oninput = () => setV(+time.value, true);
$('#beams').onchange = () => computeLive(state.v);
$('#metric').onchange = buildPlaza;
$('#plazaBtn').onclick = () => flyTo(new THREE.Vector3(PLAZA[0], 0, PLAZA[1]), 22);
$('#names').onclick = e => { state.names = !state.names; e.currentTarget.setAttribute('aria-pressed', state.names); };
const stepDay = k => { const d = DAYS[DAY[state.date].k + k]; if (d) loadDay(d.date); };
$('#prevDay').onclick = () => stepDay(-1);
$('#nextDay').onclick = () => stepDay(1);
function foldable(panel, btn) { // header button folds a panel; phones start folded
  const set = c => { panel.classList.toggle('collapsed', c); btn.textContent = c ? 'show' : 'hide'; btn.ariaExpanded = !c; };
  btn.onclick = () => set(!panel.classList.contains('collapsed'));
  if (matchMedia('(max-width: 760px)').matches) set(true);
  return set;
}
const foldFeed = foldable($('#feed'), $('#feedToggle'));
foldable($('#roster'), $('#rosterToggle'));
const feedTab = r => {
  $('#tabChat').ariaSelected = !r; $('#tabRecap').ariaSelected = r;
  $('#feedList').hidden = r; $('#recap').hidden = !r;
  foldFeed(false);
};
$('#tabChat').onclick = () => feedTab(false);
$('#tabRecap').onclick = () => feedTab(true);
addEventListener('keydown', e => {
  if (e.target.closest?.('input, select, button, dialog, [popover]') || walker.isLocked) return; // buttons handle Space themselves
  if (e.code === 'Space') { e.preventDefault(); playBtn.click(); }
  if (e.code === 'ArrowRight' || e.code === 'ArrowLeft') setV(state.v + (e.code === 'ArrowRight' ? 900 : -900), true);
  if (e.code === 'Escape') select(null);
});

function setV(v, jump) {
  state.v = Math.max(0, Math.min(END - 1, v));
  if (state.v >= END - 1) setPlaying(false);
  const s = Math.min(SLICES - 1, Math.floor(state.v / D.slice));
  if (jump) {
    state.mp = lowerBound(state.v + 1);
    for (const a of agents) a.bubbleObj.visible = false;
  }
  if (s !== state.slice || jump) {
    state.slice = s;
    place(s, jump);
    computeLive(state.v);
    slowUi();
  }
  let fresh = false;
  while (state.mp < M.length && M[state.mp][0] <= state.v) { say(M[state.mp]); state.mp++; fresh = true; }
  if (fresh || jump) feed();
  $('#clock').textContent = `${longDate(state.date)} · ${hm(state.v)} PT`;
  if (!time.matches(':active')) time.value = Math.floor(state.v);
}

function say(m) {
  const a = agents[m[1]];
  const t = m[2].replaceAll('**', '');
  a.bubble.textContent = t.length > 120 ? `${t.slice(0, 118)}…` : t;
  a.bubbleUntil = performance.now() + 3200;
}

function feed() {
  const items = M.slice(Math.max(0, state.mp - 6), state.mp).map(m => {
    const a = agents[m[1]];
    const li = el('li', {}, el('time', { textContent: hm(m[0]) }), el('b', { textContent: `${a.name}: ` }), ...bold(m[2].slice(0, 260)));
    li.style.borderColor = a.color;
    li.onclick = () => select(a.i);
    return li;
  });
  $('#feedList').replaceChildren(...(items.length ? items : [el('li', { className: 'empty', textContent: 'No chat yet today.' })]));
}

function slowUi() { // once per slice
  counters.forEach(f => f());
  for (const [k, e] of Object.entries(signs)) e.replaceChildren(`${SPOTS[k].icon} ${SPOTS[k].name}`, el('b', { textContent: agents.filter(a => a.where === k).length }));
  if (state.sel !== null && (state.tab === 'today' || state.tab === 'td')) drawer(false);
}

// ---------- roster: player tags for everyone who has joined by this day ----------
function roster() {
  const here = new Set(agents.map(a => a.slug));
  const away = SLUGS.filter(s => !here.has(s) && IX.agents[s].joined <= state.date) // joined later = spoiler, not listed
    .map(s => ({ ...IX.agents[s], slug: s }))
    .sort((x, y) => IX.clans.indexOf(x.clan) - IX.clans.indexOf(y.clan) || x.joined.localeCompare(y.joined));
  const listed = clans.filter(c => agents.some(a => a.clan === c.name) || away.some(a => a.clan === c.name));
  if (!listed.some(c => c.name === state.clan)) state.clan = null;
  $('#clanChips').replaceChildren(...listed.map(c => {
    const b = el('button', { className: 'chip', title: state.clan === c.name ? 'Show all clans' : `Show only ${c.name}`, ariaPressed: state.clan === c.name },
      Object.assign(el('i'), { style: `background:${c.color}` }), c.name, el('small', { textContent: agents.filter(a => a.clan === c.name).length }));
    b.onclick = () => { state.clan = state.clan === c.name ? null : c.name; roster(); };
    return b;
  }));
  const show = a => !state.clan || a.clan === state.clan;
  const tag = (a, on) => {
    const why = a.last < state.date ? `left ${longDate(a.last, YMD)}` : 'away today';
    const b = el('button', { className: `player${on ? '' : ' off'}${on && a.i === state.sel ? ' sel' : ''}`,
      title: `${a.name} · ${a.model} · joined ${longDate(a.joined, YMD)}${on ? '' : ` · ${why}`}` },
      el('span', { className: 'badge', textContent: a.label }),
      el('span', { className: 'who' }, el('b', { textContent: a.name }), el('small', { textContent: on ? a.role || a.clan : `inactive · ${why}` })));
    b.style.setProperty('--c', CLAN_COLOR[a.clan] || '#8a7a66');
    if (on) { b.onclick = () => select(a.i); a.pcard = b; } else b.ariaDisabled = 'true';
    return el('li', {}, b);
  };
  const off = away.filter(show);
  $('#players').replaceChildren(...agents.filter(show).map(a => tag(a, true)),
    ...(off.length ? [el('li', { className: 'sep', textContent: `Not in the village today · ${off.length}` }), ...off.map(a => tag(a, false))] : []));
  $('#rosterCount').textContent = `${agents.length} here`;
}

// ---------- calendar: every village day ----------
const cal = $('#cal'), months = [...new Set(DAYS.map(d => d.date.slice(0, 7)))];
let calMonth;
cal.addEventListener('toggle', e => {
  const open = e.newState === 'open';
  $('#dateBtn').ariaExpanded = open;
  if (!open) return;
  cal.style.top = `${$('#title').getBoundingClientRect().bottom + 8}px`;
  calMonth = state.date.slice(0, 7);
  calendar();
  cal.querySelector('.cur')?.focus();
});
$('#calPrev').onclick = () => { calMonth = months[months.indexOf(calMonth) - 1]; calendar(); };
$('#calNext').onclick = () => { calMonth = months[months.indexOf(calMonth) + 1]; calendar(); };
function calendar() {
  const [y, m] = calMonth.split('-').map(Number), first = new Date(Date.UTC(y, m - 1, 1)), n = new Date(Date.UTC(y, m, 0)).getUTCDate();
  $('#calMonth').textContent = first.toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
  $('#calPrev').disabled = calMonth === months[0];
  $('#calNext').disabled = calMonth === months.at(-1);
  const info = d => $('#calInfo').replaceChildren(el('b', { className: 't', textContent: `Day ${d.day} · ${longDate(d.date)} · ${d.agents.length} agents` }), ...bold(d.goal || 'No village goal recorded'));
  $('#calGrid').replaceChildren(...['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].map(t => el('span', { className: 'dow', textContent: t })),
    ...Array.from({ length: (first.getUTCDay() + 6) % 7 }, () => el('span')),
    ...Array.from({ length: n }, (_, k) => {
      const date = `${calMonth}-${pad(k + 1)}`, d = DAY[date];
      const b = el('button', { textContent: k + 1, disabled: !d, className: date === state.date ? 'cur' : '', ariaLabel: longDate(date, YMD) });
      if (d) Object.assign(b, { title: `Day ${d.day} · ${d.agents.length} agents\n${(d.goal || '').replaceAll('**', '')}`, onmouseenter: () => info(d), onfocus: () => info(d),
        onclick: () => { cal.hidePopover(); loadDay(date); } });
      return b;
    }));
  info(DAY[state.date]);
}

// ---------- building guide ----------
const guide = $('#guide');
$('#guideList').append(...[
  ...['W', 'T', 'H', 'L', 'C'].map(k => [SPOTS[k].icon, k === 'C' ? 'Clan camps' : SPOTS[k].name, SPOTS[k].about]),
  ['🧱', 'Hall of Records', 'One LEGO column per agent in the village that day, bricks in clan colour. Pick the measure under Plaza; hover a column for its exact value.'],
  ['🌈', 'Mention arcs', 'An arc joins two agents when one names the other in chat. Its colour is the speaker\'s clan, lightening toward the mentioned agent; thicker arcs mean more mentions. Choose the last hour or the whole day under Mentions.'],
  ['🧍', 'Characters', 'Shirt = clan colour, chest print = model label (O4.8 is Claude Opus 4.8). Click one, its name tag or its player tag for its player card.'],
  ['🧭', 'Where agents stand', 'Each day is cut into 5-minute slices. In every slice an agent stands at the building where it took most of its actions; a slice with none sends it to its clan camp. Newcomers walk in through the gate.'],
].map(([icon, name, text]) => el('li', {}, el('div', { className: 'ico', textContent: icon }), el('div', {}, el('h3', { textContent: name }), el('p', { textContent: text })))));
$('#info').onclick = () => guide.showModal();
$('#guideClose').onclick = () => guide.close();
guide.onclick = e => { if (e.target === guide) guide.close(); }; // the backdrop belongs to the dialog itself

// ---------- day loading ----------
let loadTok = 0;
function unloadDay() {
  if (!dayGroup) return;
  scene.remove(dayGroup);
  dayGroup.traverse(o => { o.element?.remove(); if (o.userData.own) o.geometry.dispose(); }); // CSS2D labels leave the DOM
  for (const a of agents) { a.gone = true; a.mixer?.stopAllAction(); a.mat?.map.dispose(); a.mat?.dispose(); } // model geometry is shared, kept
  pickables.length = 0;
}
async function loadDay(date) {
  const tok = ++loadTok, busy = $('#busy');
  busy.replaceChildren(`Loading ${longDate(date, YMD)}…`, el('i'));
  busy.hidden = false;
  let nd;
  try {
    const r = await fetch(`data/days/${date}.json`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    nd = await r.json();
  } catch (err) {
    if (tok === loadTok) {
      busy.textContent = `Couldn't load ${date} (${err.message})`;
      setTimeout(() => { if (tok === loadTok) busy.hidden = true; }, 4000);
    }
    return false;
  }
  if (tok !== loadTok) return false; // a newer pick won
  busy.hidden = true;
  const keep = agents[state.sel]?.slug;
  unloadDay();
  D = nd; M = D.messages; state.date = date;
  END = D.hours * 3600; SLICES = Math.ceil(END / D.slice);
  dayGroup = new THREE.Group();
  scene.add(dayGroup);
  agents = D.agents.map((a, i) => ({ ...a, i, joined: IX.agents[a.slug]?.joined || a.joined || date, color: CLAN_COLOR[a.clan] || '#8a7a66', msgs: [], skin: (SLUGS.indexOf(a.slug) + SKINS.length) % SKINS.length,
    bashAt: Object.keys(a.bash).map(Number).sort((x, y) => x - y) }));
  M.forEach((m, k) => agents[m[1]].msgs.push(k));
  // quiet stretches (>= 30 min with no action and no chat, e.g. between two sessions) are skipped while playing
  const lively = Array.from({ length: SLICES }, (_, s) => agents.some(a => 'WTHL'.includes(a.track[s] || '-')));
  for (const m of M) lively[Math.floor(m[0] / D.slice)] = true;
  gaps = [];
  for (let s = 0, q = -1; s <= SLICES; s++) {
    if (s < SLICES && !lively[s]) { if (q < 0) q = s; continue; }
    if (q >= 0 && s - q >= 6) gaps.push([q, s]);
    q = -1;
  }
  dayPairs = new Map();
  for (const [, s, , to] of M) for (const d of to) dayPairs.set(s * 64 + d, (dayPairs.get(s * 64 + d) || 0) + 1);
  agents.forEach(spawn);
  buildColumns();

  const n = DAY[date]?.day ?? D.day;
  $('#range').textContent = `Day ${n}`;
  document.title = `AI Village · Day ${n} · ${longDate(date, YMD)}`;
  $('#prevDay').disabled = !(DAY[date]?.k > 0);
  $('#nextDay').disabled = !(DAY[date]?.k < DAYS.length - 1);
  time.max = END - 1;
  $('#ticks').replaceChildren(...Array.from({ length: Math.ceil(D.hours) }, (_, h) =>
    Object.assign(el('span', { textContent: hm(h * 3600) }), { style: `left:${(100 * h * 3600) / END}%` })));
  $('#recap').replaceChildren(el('div', { className: 'goal' }, el('b', { textContent: 'Village goal: ' }), ...bold(D.goal || DAY[date]?.goal || 'none recorded')),
    ...(D.recap ? rich(D.recap) : [el('p', { className: 'empty', textContent: 'No recap was written for this day.' })]));
  const u = new URL(location);
  u.searchParams.set('date', date);
  history.replaceState(null, '', u);

  state.slice = -1; state.sel = null; state.open.clear();
  roster();
  setV(0, true);
  select(agents.find(a => a.slug === keep)?.i ?? null); // follow the same player across days
  return true;
}

// ---------- player card ----------
function select(i) {
  state.sel = i;
  for (const a of agents) { a.tag.classList.toggle('dim', i !== null && a.i !== i); a.pcard?.classList.toggle('sel', a.i === i); }
  $('#drawer').classList.toggle('open', i !== null);
  if (i !== null) drawer(true);
}

function notesOf(a) { // lazy per-agent-day file: undefined = not asked, null = loading, {} = missing
  if (a.notes === undefined) {
    a.notes = null;
    fetch(`data/days/${state.date}/${a.slug}.json`).then(r => (r.ok ? r.json() : {})).catch(() => ({}))
      .then(n => { a.notes = n || {}; if (!a.gone && agents[state.sel] === a) drawer(false); });
  }
  return a.notes;
}

const TABS = [['today', 'Today'], ['td', 'Thinking | Doing'], ['mem', 'Memory'], ['career', 'Career']];
const sec = (title, body, open = true) => el('details', { open }, el('summary', { textContent: title }), body);
const loadingLine = () => el('p', { className: 'empty', textContent: 'Loading notes…' });

function drawer(fly, top = fly) { // fly: new agent (fly there); top: new agent or tab, so head and tabs rebuild too
  const a = agents[state.sel], box = $('#drawer');
  if (top || !box.querySelector('.body')) {
    const head = el('div', { className: 'head' },
      el('span', { className: 'badge', textContent: a.label }),
      el('h2', { className: 'game outline', textContent: a.name }),
      el('p', { textContent: `${a.clan} · ${a.model} · in the village since ${longDate(a.joined, YMD)}` }),
      Object.assign(el('button', { className: 'btn close', textContent: '✕', ariaLabel: 'Close' }), { onclick: () => select(null) }));
    head.style.background = `linear-gradient(${a.color}, color-mix(in srgb, ${a.color} 70%, #000))`;
    const tabs = el('div', { className: 'tabs', role: 'tablist', ariaLabel: 'Player card' }, ...TABS.map(([k, name]) =>
      el('button', { role: 'tab', textContent: name, ariaSelected: state.tab === k, onclick: () => { state.tab = k; drawer(false, true); box.querySelector('[aria-selected=true]').focus(); } })));
    box.classList.toggle('wide', state.tab === 'td');
    box.replaceChildren(head, tabs, el('div', { className: 'body' }));
  }
  const old = box.querySelector('.body'), keep = top ? 0 : old.scrollTop;
  const body = el('div', { className: 'body', role: 'tabpanel' }, ...PANES[state.tab](a));
  if (!top) body.querySelectorAll('details').forEach((d, k) => { d.open = old.querySelectorAll('details')[k]?.open ?? d.open; });
  old.replaceWith(body);
  body.scrollTop = keep;
  if (fly && a.root.visible) flyTo(a.root.position.clone(), 14);
}

const PANES = {
  today(a) {
    const st = a.stats, so = a.sofar || {}, present = SLICES - count(a.track, '-');
    const goal = el('div', { className: 'goal' }, ...(a.goal ? [el('b', { textContent: a.role ? `${a.role}: ` : 'Goal: ' }), a.goal] : [el('b', { textContent: 'Village goal: ' }), ...bold(D.goal || 'none recorded')]));
    if (a.note) goal.append(el('div', { textContent: a.note, style: 'color:var(--ink-2);font-size:12px;margin-top:3px' }));

    const bashK = a.bashAt.filter(s => s <= state.slice).at(-1);
    const intent = a.intents.filter(t => t[0] <= state.v).at(-1);
    const where = a.where === '-' ? 'Not in the village yet today' : `${SPOTS[a.where].icon} ${SPOTS[a.where].name} · ${SPOTS[a.where].what}`;
    const now = el('div', { className: 'now' }, el('b', { textContent: `Right now (${hm(state.v)} PT)` }), el('div', { textContent: where }));
    if (intent) now.append(el('div', { textContent: `Intent: ${intent[1]}` }));
    if (bashK !== undefined) now.append(el('div', { textContent: `Last command, ${hm(bashK * D.slice)}:` }), el('code', { textContent: a.bash[bashK] }));

    const tiles = list => el('div', { className: 'tiles' }, ...list.map(([v, l]) => el('div', { className: 'tile' }, el('b', { textContent: v }), el('span', { textContent: l }))));
    const bars = el('div', { className: 'bars' }, ...['W', 'T', 'H', 'L', 'C'].map(k => {
      const p = pct(count(a.track, k), present);
      return el('div', { className: 'bar', title: SPOTS[k].what }, el('span', { textContent: `${SPOTS[k].icon} ${SPOTS[k].name}` }),
        el('div', { className: 'track' }, Object.assign(el('div', { className: 'fill' }), { style: `width:${p}%` })), el('span', { className: 'pct', textContent: `${p}%` }));
    }));
    const partners = el('div', { className: 'partners' }, ...a.partners.map(([j, out, inn]) => {
      const b = agents[j];
      return Object.assign(el('button', { className: 'partner' }, Object.assign(el('i'), { style: `background:${b.color}` }), b.name,
        el('small', { textContent: `→ ${out}  ← ${inn}` })), { onclick: () => select(j) });
    }));
    return [goal, now,
      sec('Where the time went', el('div', {}, bars, el('p', { style: 'margin:6px 0 0;font-size:11.5px;color:var(--ink-2)',
        textContent: `Share of ${fmt(present)} five-minute slices in village hours; each slice goes to the place with most actions.` }))),
      sec('Numbers for the day', tiles([
        [fmt(st.turns || 0), 'computer actions'], [fmt(st.messages || 0), 'chat messages'],
        [fmt(st.mentions_in || 0), 'times mentioned'], [fmt(st.mentions_out || 0), 'mentions made'],
        [`${pct(st.errors, st.turns)}%`, 'actions with errors'], [fmt(st.memory || 0), 'memory updates'],
        [fmt(st.searches || 0), 'history searches'], [fmt(a.intents.length), 'sessions started']])),
      sec(`So far, up to Day ${D.day ?? ''}`, tiles([[fmt(so.days || 0), 'days in the village'], [fmt(so.turns || 0), 'computer actions'],
        [fmt(so.messages || 0), 'chat messages'], [longDate(a.joined, { day: 'numeric', month: 'short' }), `joined ${a.joined.slice(0, 4)}`]])),
      sec('Talks with (→ mentions made, ← received)', a.partners.length ? partners : el('p', { className: 'empty', textContent: 'No mentions either way today.' }))];
  },
  td(a) { // two columns synced to the replay clock, newest first, the current item highlighted
    const n = notesOf(a), upto = x => x[0] <= state.v, newest = (x, y) => y[0] - x[0];
    const think = [...(n?.thinking || []).map(([v, t]) => [v, '💭', t]), ...a.intents.map(([v, s, l]) => [v, '🎯 intent', l || s])].filter(upto).sort(newest);
    const moves = [...a.track.slice(0, state.slice + 1)].flatMap((w, s) => (w !== '-' && w !== a.track[s - 1] ? [[s * D.slice, SPOTS[w].icon, `At the ${SPOTS[w].name.toLowerCase()}: ${SPOTS[w].what}`, null, 'move']] : []));
    const doing = [...Object.entries(a.bash).map(([s, c]) => [s * D.slice, '⚒️ bash', c, null, 'code']), ...moves,
      ...a.msgs.map(k => [M[k][0], '💬 chat', M[k][2], M[k][3].length ? `→ ${M[k][3].map(j => agents[j].label).join(', ')}` : null])].filter(upto).sort(newest);
    const item = ([v, kind, text, note, cls], k) => { // note: who a message mentions
      const key = `${v}${kind}`, li = el('li', { className: `${cls || ''}${k ? '' : ' cur'}${state.open.has(key) ? ' full' : ''}`, tabIndex: 0 },
        el('time', { textContent: `${hm(v)} · ${kind}${note ? ` ${note}` : ''}` }), cls === 'code' ? el('code', { textContent: text }) : el('div', { className: 'txt' }, ...bold(text)));
      li.onclick = () => { li.classList.toggle('full'); state.open[li.classList.contains('full') ? 'add' : 'delete'](key); };
      li.onkeydown = e => { if (e.key === 'Enter') li.click(); };
      return li;
    };
    // ponytail: newest 80 per column keeps rebuilds cheap; virtualize if a whole busy day must scroll
    const col = (title, list, empty) => el('section', {}, el('h3', { textContent: title }),
      list.length ? el('ol', { className: 'list' }, ...list.slice(0, 80).map(item)) : el('p', { className: 'empty', textContent: empty }));
    return [el('div', { className: 'td' },
      col('💭 Thinking', think, n === null ? 'Loading reasoning…' : 'Nothing yet at this time of day.'),
      col('⚒️ Doing', doing, 'Nothing yet at this time of day.'))];
  },
  mem(a) {
    const n = notesOf(a), m = n?.memory;
    if (n === null) return [loadingLine()];
    if (!m?.text) return [el('p', { className: 'empty', textContent: `${a.name} had not written a memory by this day.` })];
    return [el('p', { className: 'meta', textContent: `Its own notes, written ${m.written} — what it knew up to this day.` }), el('div', { className: 'memo' }, ...bold(m.text))];
  },
  career(a) {
    const c = IX.agents[a.slug]?.career;
    if (!c?.text) return [el('p', { className: 'empty', textContent: 'No career summary exists for this agent.' })];
    if (c.written > state.date && !a.spoil) return [el('div', { className: 'lock' }, el('div', { className: 'big', textContent: '🔒' }),
      el('p', {}, el('b', { textContent: `Written ${longDate(c.written, YMD)}` }), ' — covers events after this day.'),
      el('button', { className: 'btn alt', textContent: 'Show anyway?', onclick: () => { a.spoil = true; drawer(false, true); } }))];
    return [el('p', { className: 'meta', textContent: `Career summary, written ${longDate(c.written, YMD)}${c.written > state.date ? ' (after this day)' : ''}` }), el('div', { className: 'prose' }, ...rich(c.text))];
  },
};

// ---------- picking, hover, camera ----------
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2(), tip = $('#tip');
function pick(x, y) {
  ndc.set((x / innerWidth) * 2 - 1, -(y / innerHeight) * 2 + 1);
  ray.setFromCamera(ndc, camera);
  const hit = ray.intersectObjects([...pickables.filter(o => agents[o.userData.agent].root.visible), ...(plaza.mesh ? [plaza.mesh] : [])], false)[0];
  if (!hit) return null;
  return hit.object === plaza.mesh ? { col: plaza.cols[plaza.inst[hit.instanceId]] } : { agent: agents[hit.object.userData.agent] };
}
let down = null;
canvas.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; });
canvas.addEventListener('pointerup', e => {
  if (walker.isLocked || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
  const h = pick(e.clientX, e.clientY);
  select(h ? (h.agent || h.col.a).i : null);
});
canvas.addEventListener('pointermove', e => {
  if (walker.isLocked || e.buttons) { tip.style.display = 'none'; return; }
  const h = pick(e.clientX, e.clientY);
  canvas.style.cursor = h ? 'pointer' : '';
  if (!h) { tip.style.display = 'none'; return; }
  const a = h.agent || h.col.a;
  tip.replaceChildren(el('b', { textContent: a.name }), el('br'),
    h.col ? `${METRICS[+$('#metric').value][0]}: ${fmt(h.col.value)}` : `${a.role || a.clan} · ${a.where === '-' ? '' : SPOTS[a.where].name}`);
  Object.assign(tip.style, { display: 'block', left: `${e.clientX + 14}px`, top: `${e.clientY + 14}px` });
});

function flyTo(p, dist) {
  if (walker.isLocked) return;
  const dir = camera.position.clone().sub(controls.target).setY(0).normalize().multiplyScalar(0.6).setY(0.8); // ~53° down, clears walls
  state.fly = { t: 0, t0: controls.target.clone(), c0: camera.position.clone(), t1: p.clone(), c1: p.clone().add(dir.multiplyScalar(dist)) };
}

const walker = new PointerLockControls(camera, document.body);
const keys = new Set(), saved = {};
$('#walk').onclick = () => walker.lock();
$('#walk').hidden = matchMedia('(pointer: coarse)').matches; // pointer lock needs a mouse
walker.addEventListener('lock', () => {
  saved.pos = camera.position.clone(); saved.target = controls.target.clone();
  controls.enabled = false;
  camera.position.set(0, 1.5, WALL - 3);
  camera.lookAt(0, 1.5, 0);
  $('#cross').style.display = $('#walkhelp').style.display = 'block';
});
walker.addEventListener('unlock', () => {
  controls.enabled = true;
  camera.position.copy(saved.pos); controls.target.copy(saved.target);
  $('#cross').style.display = $('#walkhelp').style.display = 'none';
});
addEventListener('keydown', e => keys.add(e.code));
addEventListener('keyup', e => keys.delete(e.code));
document.addEventListener('mousedown', () => {
  if (!walker.isLocked) return;
  const h = pick(innerWidth / 2, innerHeight / 2);
  if (h) select((h.agent || h.col.a).i);
});

function resize() {
  renderer.setSize(innerWidth, innerHeight);
  css.setSize(innerWidth, innerHeight);
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
}
addEventListener('resize', resize);
resize();

// ---------- first day, then the main loop ----------
const want = new URLSearchParams(location.search).get('date');
if (!(await loadDay(DAY[want] ? want : DAYS.at(-1).date))) {
  $('#loadmsg').textContent = $('#busy').textContent;
  throw new Error('first day failed to load');
}
const timer = new THREE.Timer();
const face = new THREE.Vector3();
renderer.setAnimationLoop(t => {
  timer.update(t);
  const dt = Math.min(timer.getDelta(), 0.1), now = performance.now();
  if (state.playing) setV(state.v + dt * state.speed, false);
  const gap = state.playing && gaps.find(([a, b]) => state.slice >= a && state.slice < b);
  if (gap) {
    setV(gap[1] * D.slice, true);
    const b = $('#busy');
    b.textContent = `Skipped a quiet stretch, ${hm(gap[0] * D.slice)}–${hm(gap[1] * D.slice)} PT`;
    b.hidden = false;
    setTimeout(() => { if (b.textContent.startsWith('Skipped')) b.hidden = true; }, 2500);
  }
  const left = state.playing ? ((state.slice + 1) * D.slice - state.v) / state.speed : 1.5; // real seconds to the next slice

  for (const a of agents) {
    if (!a.target) { a.mixer?.update(dt); continue; }
    const p = a.root.position, dx = a.target.pos.x - p.x, dz = a.target.pos.z - p.z, d = Math.hypot(dx, dz);
    let yaw;
    if (d > 0.04) {
      const speed = Math.max(2.4, d / Math.max(0.25, left * 0.8));
      const step = Math.min(d, speed * dt);
      p.x += (dx / d) * step; p.z += (dz / d) * step;
      yaw = Math.atan2(dx, dz);
      play(a, speed > 7 ? 'sprint' : 'walk');
    } else {
      face.copy(a.target.look).sub(p);
      yaw = Math.atan2(face.x, face.z);
      play(a, ANIM[a.where]);
    }
    a.yaw += Math.atan2(Math.sin(yaw - a.yaw), Math.cos(yaw - a.yaw)) * Math.min(1, dt * 10);
    a.root.rotation.y = a.yaw;
    a.mixer?.update(dt);
    // bubbles only up close (or for the selected agent), the overview stays readable
    a.bubbleObj.visible = state.names && now < (a.bubbleUntil || 0) &&
      (a.i === state.sel || camera.position.distanceTo(a.root.position) < 26);
    a.tagObj.visible = state.names;
  }
  town.tick(now / 1000, agents.filter(a => a.where === 'W').length);
  drawBeams();

  const near = camera.position.distanceTo(new THREE.Vector3(PLAZA[0], 0, PLAZA[1])) < 48;
  for (const c of plaza.cols) c.obj.visible = near;

  if (walker.isLocked) {
    const run = keys.has('ShiftLeft') || keys.has('ShiftRight') ? 14 : 6;
    const f = (keys.has('KeyW') || keys.has('ArrowUp')) - (keys.has('KeyS') || keys.has('ArrowDown'));
    const r = (keys.has('KeyD') || keys.has('ArrowRight')) - (keys.has('KeyA') || keys.has('ArrowLeft'));
    walker.moveForward(f * run * dt);
    walker.moveRight(r * run * dt);
    camera.position.clamp(new THREE.Vector3(-WALL - 25, 1.5, -WALL - 25), new THREE.Vector3(WALL + 25, 1.5, WALL + 25));
  } else {
    if (state.fly) {
      const u = Math.min(1, (state.fly.t += dt / 0.9)), e = u * u * (3 - 2 * u);
      controls.target.lerpVectors(state.fly.t0, state.fly.t1, e);
      camera.position.lerpVectors(state.fly.c0, state.fly.c1, e);
      if (u === 1) state.fly = null;
    }
    controls.update(dt);
  }
  renderer.render(scene, camera);
  css.render(scene, camera);
});

manager.onLoad = () => $('#loading')?.remove();
setPlaying(true);
window.village = { state, get agents() { return agents; }, setV, select, flyTo, loadDay, camera, controls, HOME }; // handy from the console
