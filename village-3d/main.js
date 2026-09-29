// AI Village 3D: replays the last week of the AI Village as a walkable toy town.
// Data: data.json from extract.py. Scenery: town.js. Models: Kenney CC0 kits in assets/.
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

// ---------- loading ----------
const manager = new THREE.LoadingManager();
manager.onProgress = (_, done, total) => { $('#loadbar i').style.width = `${(100 * done) / total}%`; };
const loader = new GLTFLoader(manager);
const gltfs = new Map();
const gltf = url => { if (!gltfs.has(url)) gltfs.set(url, loader.loadAsync(url)); return gltfs.get(url); };
const shade = o => { if (o.isMesh) o.castShadow = o.receiveShadow = true; };
function lib(name, fix) { // a placeholder group that fills in when the model arrives
  const g = new THREE.Group();
  gltf(`assets/${name}.glb`).then(m => { const o = m.scene.clone(); o.traverse(shade); fix?.(o); g.add(o); });
  return g;
}

let D;
try {
  D = await (await fetch('data.json')).json();
} catch {
  $('#loadmsg').replaceChildren('No data.json yet. Build it from the AI Village dataset, then serve this folder:',
    el('pre', { textContent: 'cd village-3d\npython3 extract.py\npython3 -m http.server 8000\n# open http://localhost:8000' }));
  throw new Error('data.json missing');
}
await document.fonts.load('28px "Lilita One"');

const SPAN = D.hours * 3600, END = D.days.length * SPAN, SLICES = D.agents[0].track.length;
const M = D.messages; // [vtime, agent, text, [mentioned agents]] sorted by vtime
const clans = [...new Set(D.agents.map(a => a.clan))].map(name => ({ name, color: CLAN_COLOR[name] }));
const agents = D.agents.map((a, i) => ({ ...a, i, color: CLAN_COLOR[a.clan], msgs: [],
  bashAt: Object.keys(a.bash).map(Number).sort((x, y) => x - y) }));
M.forEach((m, k) => agents[m[1]].msgs.push(k));
const weekPairs = new Map();
for (const [, s, , to] of M) for (const d of to) weekPairs.set(s * 64 + d, (weekPairs.get(s * 64 + d) || 0) + 1);

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

const town = buildTown(scene, lib, clans);
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
for (const a of agents) {
  a.root = new THREE.Group();
  a.root.visible = false;
  scene.add(a.root);
  gltf(`assets/characters/character-${SKINS[a.i % SKINS.length]}.glb`).then(m => {
    const body = m.scene.clone();
    let orig;
    body.traverse(o => { if (o.isMesh) orig ||= o.material.map; });
    const mat = new THREE.MeshStandardMaterial({ map: paintSkin(orig, a.color, a.label), roughness: 0.42 }); // shiny toy plastic
    body.traverse(o => { if (o.isMesh) { o.material = mat; o.castShadow = true; o.userData.agent = a.i; pickables.push(o); } });
    body.scale.setScalar(CH);
    a.root.add(body);
    a.mixer = new THREE.AnimationMixer(body);
    a.clips = Object.fromEntries(m.animations.map(c => [c.name, a.mixer.clipAction(c)]));
    a.anim = null;
    play(a, a.want || 'idle');
  });
  const tag = el('div', { className: 'tag', textContent: a.label, title: `${a.name} · ${a.role}` });
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
    a.where = a.track[slice];
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
  const pairs = mode === 1 ? livePairs : mode === 2 ? weekPairs : new Map();
  const cut = mode === 1 ? [1, 2, 4] : [1, 25, 80]; // mention-count buckets -> line width
  const buf = [[], [], []], col = [[], [], []];
  for (const [key, n] of pairs) {
    const s = agents[Math.floor(key / 64)], d = agents[key % 64];
    if (state.sel !== null && s.i !== state.sel && d.i !== state.sel) continue;
    if (state.sel === null && n < (mode === 2 ? 10 : 2)) continue; // without a selection, only repeated ties
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
{
  // rows: clans by size, next-fit so a clan never splits across rows
  const bySize = [...clans].map(c => ({ ...c, list: agents.filter(a => a.clan === c.name) })).sort((x, y) => y.list.length - x.list.length);
  const rows = [[]];
  for (const c of bySize) {
    if (rows.at(-1).length && rows.at(-1).reduce((n, g) => n + g.list.length, 0) + c.list.length > 12) rows.push([]);
    rows.at(-1).push(c);
  }
  rows.forEach((row, r) => {
    const z = PLAZA[1] + 3.3 - r * 3.3, y = r * 0.75;
    const n = row.reduce((s, g) => s + g.list.length, 0), w = n * 1.1 + (row.length - 1) * 0.5;
    const riser = new THREE.Mesh(new THREE.BoxGeometry(w + 1.2, y + 0.16, 2.4), new THREE.MeshStandardMaterial({ color: 0xcfc3a8, roughness: 0.95 }));
    riser.position.set(PLAZA[0], (y + 0.16) / 2, z);
    riser.castShadow = riser.receiveShadow = true;
    scene.add(riser);
    let x = PLAZA[0] - w / 2 + 0.55;
    for (const g of row) {
      for (const a of g.list) {
        const lab = el('div', { className: 'col-label' });
        const obj = new CSS2DObject(lab);
        scene.add(obj);
        plaza.cols.push({ a, x, z, y: y + 0.16, lab, obj });
        x += 1.1;
      }
      x += 0.5;
    }
  });
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
    plaza.mesh = new THREE.InstancedMesh(geo, new THREE.MeshStandardMaterial({ roughness: 0.3 }), plaza.cols.length * MAXB);
    plaza.mesh.castShadow = plaza.mesh.receiveShadow = true;
    scene.add(plaza.mesh);
    buildPlaza();
  });
}
function buildPlaza() {
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
  plaza.mesh.instanceColor.needsUpdate = true;
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
const state = { v: 0, slice: -1, playing: true, speed: 300, sel: null, mp: 0, fly: null, names: true };
const day = v => Math.min(D.days.length - 1, Math.floor(v / SPAN));
const dateName = d => new Date(`${D.days[d].date}T12:00:00Z`).toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
function hm(v) { const s = v - day(v) * SPAN; return `${String(D.open + Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}`; }
const when = v => `${dateName(day(v))} ${hm(v)}`;
function lowerBound(v) { let lo = 0, hi = M.length; while (lo < hi) { const mid = (lo + hi) >> 1; if (M[mid][0] < v) lo = mid + 1; else hi = mid; } return lo; }

$('#range').textContent = `Days ${D.days[0].day}–${D.days.at(-1).day}`;
$('#legend').append(...clans.map(c => el('div', {}, Object.assign(el('i'), { style: `background:${c.color}` }), c.name,
  el('small', { textContent: agents.filter(a => a.clan === c.name).length }))));
const time = $('#time');
time.max = END - 1;
$('#ticks').append(...D.days.map((d, i) => Object.assign(el('span', {}, dateName(i).split(',')[0], el('small', { textContent: ` · Day ${d.day}` })),
  { style: `left:${(100 * i) / D.days.length}%` })));
const counters = [
  ['💬', 'msgs today', () => lowerBound(state.v + 1) - lowerBound(day(state.v) * SPAN), null],
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
$('#feedToggle').onclick = e => {
  const c = $('#feed').classList.toggle('collapsed');
  e.currentTarget.textContent = c ? 'show' : 'hide';
  e.currentTarget.setAttribute('aria-expanded', !c);
};
addEventListener('keydown', e => {
  if (e.target.closest?.('input, select, button') || walker.isLocked) return; // buttons handle Space themselves
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
  $('#clock').replaceChildren(`${dateName(day(state.v))} · ${hm(state.v)} PT `, el('small', { textContent: `Day ${D.days[day(state.v)].day}` }));
  if (!time.matches(':active')) time.value = Math.floor(state.v);
}

function say(m) {
  const a = agents[m[1]];
  a.bubble.textContent = m[2].length > 120 ? `${m[2].slice(0, 118)}…` : m[2];
  a.bubbleUntil = performance.now() + 3200;
}

function feed() {
  const items = M.slice(Math.max(0, state.mp - 6), state.mp).map(m => {
    const a = agents[m[1]];
    const li = el('li', {}, el('time', { textContent: hm(m[0]) }), el('b', { textContent: `${a.name}: ` }), m[2].slice(0, 260));
    li.style.borderColor = a.color;
    li.onclick = () => select(a.i);
    return li;
  });
  $('#feedList').replaceChildren(...items);
}

function slowUi() { // once per slice
  counters.forEach(f => f());
  for (const [k, e] of Object.entries(signs)) e.replaceChildren(`${SPOTS[k].icon} ${SPOTS[k].name}`, el('b', { textContent: agents.filter(a => a.where === k).length }));
  if (state.sel !== null) drawer(false);
}

// ---------- agent drawer ----------
function select(i) {
  state.sel = i;
  for (const a of agents) a.tag.classList.toggle('dim', i !== null && a.i !== i);
  $('#drawer').classList.toggle('open', i !== null);
  if (i !== null) drawer(true);
}

function drawer(fresh) {
  const a = agents[state.sel], st = a.stats, box = $('#drawer');
  const present = SLICES - count(a.track, '-');
  const head = el('div', { className: 'head' },
    el('h2', { className: 'game outline', textContent: a.name }),
    el('p', { textContent: `${a.clan} · ${a.model} · in the village since ${a.joined}` }),
    Object.assign(el('button', { className: 'btn close', textContent: '✕', ariaLabel: 'Close' }), { onclick: () => select(null) }));
  head.style.background = `linear-gradient(${a.color}, color-mix(in srgb, ${a.color} 70%, #000))`;

  const goal = el('div', { className: 'goal' }, el('b', { textContent: a.role ? `${a.role}: ` : 'Goal: ' }), a.goal || 'no individual goal');
  if (a.note) goal.append(el('div', { textContent: a.note, style: 'color:var(--ink-2);font-size:12px;margin-top:3px' }));

  const bashK = a.bashAt.filter(s => s <= state.slice).at(-1);
  const intent = a.intents.filter(t => t[0] <= state.v).at(-1);
  const where = a.where === '-' ? 'Not in the village yet' : `${SPOTS[a.where].icon} ${SPOTS[a.where].name} · ${SPOTS[a.where].what}`;
  const now = el('div', { className: 'now' }, el('b', { textContent: `Right now (${when(state.v)} PT)` }), el('div', { textContent: where }));
  if (intent) now.append(el('div', { textContent: `Intent: ${intent[1]}` }));
  if (bashK !== undefined) now.append(el('div', { textContent: `Last command, ${hm(bashK * D.slice)}:` }), el('code', { textContent: a.bash[bashK] }));

  const tiles = el('div', { className: 'tiles' }, ...[
    [fmt(st.turns || 0), 'computer actions'], [fmt(st.messages || 0), 'chat messages'],
    [fmt(st.mentions_in || 0), 'times mentioned'], [fmt(st.mentions_out || 0), 'mentions made'],
    [`${pct(st.errors, st.turns)}%`, 'actions with errors'], [fmt(st.memory || 0), 'memory updates'],
    [fmt(st.searches || 0), 'history searches'], [fmt(a.intents.length), 'sessions started'],
  ].map(([v, l]) => el('div', { className: 'tile' }, el('b', { textContent: v }), el('span', { textContent: l }))));

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

  const intents = el('ol', { className: 'list' }, ...a.intents.filter(t => t[0] <= state.v).slice(-40).reverse().map(t =>
    el('li', { className: t === intent ? 'cur' : '', title: t[2] }, el('time', { textContent: when(t[0]) }), t[1])));
  const msgs = el('ol', { className: 'list' }, ...a.msgs.filter(k => M[k][0] <= state.v).slice(-30).reverse().map(k =>
    el('li', {}, el('time', { textContent: when(M[k][0]) + (M[k][3].length ? ` → ${M[k][3].map(j => agents[j].label).join(', ')}` : '') }), M[k][2])));

  const sec = (title, body, open = true) => el('details', { open }, el('summary', { textContent: title }), body);
  const keep = fresh ? null : box.querySelector('.body')?.scrollTop;
  const body = el('div', { className: 'body' }, goal, now,
    sec('Where the time went', el('div', {}, bars, el('p', { style: 'margin:6px 0 0;font-size:11.5px;color:var(--ink-2)',
      textContent: `Share of ${fmt(present)} five-minute slices in village hours; each slice goes to the place with most actions.` }))),
    sec('Numbers for the week', tiles),
    sec('Talks with (→ mentions made, ← received)', partners),
    sec(`What it said it would do (${a.intents.filter(t => t[0] <= state.v).length} so far)`, intents, false),
    sec(`Chat messages (${a.msgs.filter(k => M[k][0] <= state.v).length} so far)`, msgs, false));
  if (!fresh) body.querySelectorAll('details').forEach((d, k) => { d.open = box.querySelectorAll('details')[k]?.open ?? d.open; });
  box.replaceChildren(head, body);
  if (keep) body.scrollTop = keep;
  if (fresh && a.root.visible) flyTo(a.root.position.clone(), 14);
}

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

// ---------- main loop ----------
const timer = new THREE.Timer();
const face = new THREE.Vector3();
setV(0, true);
feed();
renderer.setAnimationLoop(t => {
  timer.update(t);
  const dt = Math.min(timer.getDelta(), 0.1), now = performance.now();
  if (state.playing) setV(state.v + dt * state.speed, false);
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

manager.onLoad = () => $('#loading').remove();
setPlaying(true);
Object.assign(window, { village: { state, agents, setV, select, flyTo, camera, controls, HOME } }); // handy from the console
