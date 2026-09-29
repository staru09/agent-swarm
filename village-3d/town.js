// The static village: ground, castle walls, the four activity buildings, clan camps and scenery.
// Everything is assembled from Kenney CC0 kits (assets/), 1 kit unit = 1 grid cell.
import * as THREE from 'three';

const TAU = Math.PI * 2;
// Wall panels sit on a cell's +x edge; these rotations turn them to face each side.
const SIDE = { e: 0, n: Math.PI / 2, w: Math.PI, s: -Math.PI / 2 };

// Where things stand (world units, +z = south, where the gate and the default camera are).
export const SPOTS = {
  W: { name: 'Workshop', icon: '⚒️', what: 'bash / terminal', at: [-17, 3], yard: [-10.5, 4], sign: 10 },
  T: { name: 'Watchtower', icon: '🔭', what: 'browser & GUI', at: [15, -10], yard: [11, -3.5], sign: 16.5 },
  H: { name: 'Town Hall', icon: '💬', what: 'chat & human requests', at: [0, -10], yard: [0, -3.2], sign: 11 },
  L: { name: 'Library', icon: '📚', what: 'memory & history search', at: [17, 5], yard: [12, 6.5], sign: 11.5 },
  C: { name: 'Clan camp', icon: '🔥', what: 'paused / idle' },
};
export const PLAZA = [-13, -11];
export const WALL = 25; // half-size of the castle wall square

export const camps = n => Array.from({ length: n }, (_, i) => { // either side of the gate road
  const k = i - n / 2, x = k < 0 ? -4.6 + (k + 1) * 5 : 4.6 + k * 5;
  return [x, 18.5];
});

export function buildTown(scene, lib, clans) {
  const put = (name, x, z, { ry = 0, s = 2, y = 0, parent = scene, fix } = {}) => {
    const o = lib(name, fix);
    o.position.set(x, y, z); o.rotation.y = ry; o.scale.setScalar(s);
    parent.add(o);
    return o;
  };
  const rnd = mulberry(7);
  const campAt = camps(clans.length);
  const keepOut = [ // [x, z, radius] where scenery must not grow
    [SPOTS.W.at[0], SPOTS.W.at[1], 6], [SPOTS.W.yard[0], SPOTS.W.yard[1], 5], [SPOTS.T.at[0], SPOTS.T.at[1], 4.5],
    [SPOTS.T.yard[0], SPOTS.T.yard[1], 4.5], [SPOTS.H.at[0], SPOTS.H.at[1], 7], [SPOTS.L.at[0], SPOTS.L.at[1], 4.5],
    [SPOTS.L.yard[0], SPOTS.L.yard[1], 4], [PLAZA[0], PLAZA[1], 10], [0, 8, 4.5],
    ...campAt.map(([x, z]) => [x, z, 3.6]),
  ];
  const free = (x, z) => keepOut.every(([kx, kz, r]) => Math.hypot(x - kx, z - kz) > r) && !(Math.abs(x) < 3 && z > 0);

  scene.add(ground());

  // Castle wall ring with corner towers and a south gate.
  for (let i = -WALL; i <= WALL; i += 2) {
    for (const [x, z] of [[i, -WALL], [i, WALL], [-WALL, i], [WALL, i]]) {
      if (Math.abs(x) === WALL && Math.abs(z) === WALL) continue;
      if (z === WALL && Math.abs(x) <= 3) continue; // gate gap
      put('castle/wall', x, z, { s: 2, ry: Math.abs(x) === WALL ? Math.PI / 2 : 0 });
    }
  }
  for (const [x, z] of [[-WALL, -WALL], [WALL, -WALL], [-WALL, WALL], [WALL, WALL]]) tower(x, z, 2.4, 1, 'castle/tower-square-top-roof-rounded');
  for (const x of [-4.4, 4.4]) tower(x, WALL, 2.2, 1, 'castle/tower-square-top-roof');
  put('castle/flag-banner-long', -3.4, WALL + 1.2, { s: 2.2 });
  put('castle/flag-banner-long', 3.4, WALL + 1.2, { s: 2.2, ry: Math.PI });

  // Town Hall: 3x3 cells, two floors, big hip roof, market stalls out front.
  const hall = house(...SPOTS.H.at, 3, 3, 2, { s: 2.2, roof: 'town/roof-high-point', roofY: 1.35, door: 'town/wall-door' });
  put('town/chimney', 0.35, -0.9, { s: 1, y: 2, parent: hall });
  for (const x of [-1, 1]) put('town/banner-red', x, 1.05, { s: 1, ry: SIDE.s, parent: hall, y: 0.9 });
  put('castle/flag', 0, 0, { s: 0.9, y: 3.35, parent: hall });
  for (const x of [-4.6, 4.6]) put('town/lantern', x, -5.2, { s: 1.6 });
  put('town/stall-red', -5.8, 4.2, { s: 1.8, ry: 0.5 });
  put('town/stall-green', 5.8, 4.2, { s: 1.8, ry: -0.5 });
  put('town/stall-bench', -6.8, 5.6, { s: 1.8, ry: 0.5 });
  put('town/cart', 7.5, -8, { s: 1.8, ry: 1.2 });

  // Workshop: a timber windmill whose sails spin faster the more agents are running bash.
  const mill = house(...SPOTS.W.at, 2, 2, 3, { s: 2, wall: 'town/wall-wood', window: 'town/wall-wood-window-shutters',
    door: 'town/wall-wood-door', roof: 'town/roof-point', roofY: 1.15, doorSide: 'e' });
  const sails = put('town/windmill', 1.25, 0, { s: 1.15, y: 2.45, parent: mill });
  put('town/cart-high', -14.5, 8.5, { s: 1.8, ry: 2.4 });
  for (const z of [-2.5, -0.5]) put('town/fence', -19.5, z, { s: 2, ry: SIDE.w });

  // Watchtower (browser) and Library (memory + search).
  tower(...SPOTS.T.at, 2.6, 4, 'castle/tower-square-top-roof-high-windows', 'castle/tower-square-mid-windows');
  put('castle/flag', ...SPOTS.T.at, { s: 2.4, y: 16.6 });
  hexTower(...SPOTS.L.at, 2.8);
  for (const z of [1.5, 8.5]) put('town/hedge-large', 14, z, { s: 2, ry: SIDE.n });

  // Crossroads fountain, lanterns, trees, rocks, flowers.
  put('town/fountain-round', 0, 8, { s: 2.4 });
  put('town/fountain-center', 0, 8, { s: 2.4 });
  for (const [x, z] of [[-2.8, 13], [2.8, 13], [-4.6, 1.2], [4.6, 1.2], [-5.5, 10], [5.5, 10]]) put('town/lantern', x, z, { s: 1.6 });
  const trees = ['town/tree', 'town/tree-high', 'town/tree-high-round', 'town/tree-crooked', 'town/tree-high-crooked'];
  for (let i = 0, n = 0; i < 400 && n < 34; i++) { // inside the walls, near the edge
    const x = (rnd() - 0.5) * 2 * (WALL - 2.5), z = (rnd() - 0.5) * 2 * (WALL - 2.5);
    if (Math.max(Math.abs(x), Math.abs(z)) < 17 || !free(x, z)) continue;
    put(trees[n++ % trees.length], x, z, { s: 1.8 + rnd() * 0.8, ry: rnd() * TAU });
  }
  for (let i = 0; i < 110; i++) { // forest outside the walls
    const a = rnd() * TAU, r = WALL + 5 + rnd() * 26;
    const x = Math.cos(a) * r, z = Math.sin(a) * r;
    if (z > WALL && Math.abs(x) < 9) continue; // the road out of the gate
    put(i % 5 ? 'castle/tree-large' : trees[i % trees.length], x, z, { s: 2.2 + rnd() * 1.6, ry: rnd() * TAU });
  }
  for (let i = 0; i < 18; i++) {
    const a = rnd() * TAU, r = WALL + 3 + rnd() * 20;
    if (Math.sin(a) * r > WALL && Math.abs(Math.cos(a) * r) < 9) continue;
    put(i % 3 ? 'town/rock-large' : 'town/rock-wide', Math.cos(a) * r, Math.sin(a) * r, { s: 1.5 + rnd() * 1.5, ry: rnd() * TAU });
  }
  const flowers = ['nature/flower_redA', 'nature/flower_yellowA', 'nature/flower_purpleA', 'nature/grass', 'nature/grass_large'];
  for (let i = 0, n = 0; i < 600 && n < 80; i++) {
    const x = (rnd() - 0.5) * 2 * (WALL - 2), z = (rnd() - 0.5) * 2 * (WALL - 2);
    if (free(x, z)) put(flowers[n++ % flowers.length], x, z, { s: 2.2, ry: rnd() * TAU });
  }

  // Clan camps: a tent in clan colour, a campfire and a banner with the clan name.
  const fires = [];
  campAt.forEach(([x, z], i) => {
    const c = clans[i];
    const cloth = new THREE.MeshStandardMaterial({ color: c.color, roughness: 0.8 });
    put('nature/tent_detailedOpen', x, z - 1.6, { s: 3.2, ry: Math.PI,
      fix: t => t.traverse(o => { if (o.material?.name === 'colorRed') o.material = cloth; }) });
    put('nature/campfire_stones', x, z + 0.4, { s: 2.6 });
    put('nature/campfire_logs', x, z + 0.4, { s: 2.6 });
    const flame = new THREE.Mesh(new THREE.ConeGeometry(0.28, 0.85, 7), new THREE.MeshBasicMaterial({ color: 0xff7a1a }));
    const core = new THREE.Mesh(new THREE.ConeGeometry(0.15, 0.55, 7), new THREE.MeshBasicMaterial({ color: 0xffe066 }));
    core.position.y = -0.1;
    flame.add(core);
    flame.position.set(x, 0.48, z + 0.4);
    scene.add(flame);
    fires.push(flame);
    scene.add(banner(c, x + 1.8, z - 2.2));
  });

  // Stats plaza floor.
  const plaza = new THREE.Mesh(new THREE.BoxGeometry(16, 0.16, 11), new THREE.MeshStandardMaterial({ color: 0xd8cdb5, roughness: 0.95 }));
  plaza.position.set(PLAZA[0], 0.08, PLAZA[1]);
  plaza.receiveShadow = true;
  scene.add(plaza);

  return {
    tick(t, bashers) {
      sails.rotation.x -= 0.004 + bashers * 0.0022;
      fires.forEach((f, i) => f.scale.set(1, 0.8 + 0.25 * Math.sin(t * 9 + i * 1.7) + 0.1 * Math.sin(t * 23 + i), 1));
    },
  };

  function tower(x, z, s, mids, top, mid = 'castle/tower-square-mid') {
    put('castle/tower-square-base', x, z, { s });
    for (let i = 0; i < mids; i++) put(mid, x, z, { s, y: s * (1 + i) });
    put(top, x, z, { s, y: s * (1 + mids) });
  }

  function hexTower(x, z, s) {
    put('castle/tower-hexagon-base', x, z, { s });
    let y = s * 1.31;
    for (let i = 0; i < 4; i++, y += s * 0.46) put('castle/tower-hexagon-mid', x, z, { s, y });
    put('castle/tower-hexagon-top', x, z, { s, y });
    put('castle/tower-hexagon-roof', x, z, { s, y: y + s * 0.13 });
  }

  // cols x rows cells, `floors` storeys of wall panels on the outside edges, one scaled pyramid roof on top.
  function house(x, z, cols, rows, floors, o) {
    const g = new THREE.Group();
    g.position.set(x, 0, z); g.scale.setScalar(o.s);
    scene.add(g);
    const wall = o.wall || 'town/wall', win = o.window || 'town/wall-window-shutters', doorSide = o.doorSide || 's';
    const cx = (cols - 1) / 2, cz = (rows - 1) / 2;
    for (let f = 0; f < floors; f++) {
      for (let i = 0; i < cols; i++) for (let j = 0; j < rows; j++) {
        const sides = [i === cols - 1 && 'e', i === 0 && 'w', j === 0 && 'n', j === rows - 1 && 's'].filter(Boolean);
        for (const side of sides) {
          const mid = side === 'e' || side === 'w' ? j === Math.floor(cz) : i === Math.floor(cx);
          const name = f === 0 && side === doorSide && mid ? o.door : (i + j + f) % 2 ? win : wall;
          put(name, i - cx, j - cz, { s: 1, y: f, ry: SIDE[side], parent: g });
        }
      }
    }
    const roof = put(o.roof, 0, 0, { s: 1, y: floors, parent: g });
    roof.scale.set(cols, (o.roofY * Math.max(cols, rows)) / 2, rows);
    return g;
  }
}

// Grass in two tones on a big tile grid (the Clash of Clans look), dirt paths baked into the same texture.
function ground() {
  const N = 2048, S = 140, c = document.createElement('canvas');
  c.width = c.height = N;
  const g = c.getContext('2d'), px = N / S, at = v => (v + S / 2) * px;
  for (let i = 0; i < S / 2; i++) for (let j = 0; j < S / 2; j++) {
    g.fillStyle = (i + j) % 2 ? '#7cc243' : '#86cb4b';
    g.fillRect(i * 2 * px, j * 2 * px, 2 * px, 2 * px);
  }
  const rnd = mulberry(3);
  for (let i = 0; i < 1600; i++) { // speckles
    g.fillStyle = rnd() > 0.5 ? 'rgba(255,255,255,0.06)' : 'rgba(30,80,10,0.08)';
    g.beginPath(); g.arc(rnd() * N, rnd() * N, 2 + rnd() * 5, 0, TAU); g.fill();
  }
  g.lineCap = g.lineJoin = 'round';
  const path = pts => {
    for (const [w, col] of [[4.2, '#b98a4e'], [3.4, '#d8ae6c']]) {
      g.strokeStyle = col; g.lineWidth = w * px; g.beginPath();
      pts.forEach(([x, z], k) => (k ? g.lineTo : g.moveTo).call(g, at(x), at(z)));
      g.stroke();
    }
  };
  path([[0, 70], [0, 8], [0, -6]]);
  path([[0, 8], [-6, 5], [-13, 3.5]]);
  path([[0, 3], [9, -1], [15, -7]]);
  path([[0, 8], [9, 7], [15, 5]]);
  path([[0, -1], [-7, -4], [-10, -6]]);
  path([[-21, 17.2], [21, 17.2]]);
  g.fillStyle = '#d8ae6c'; g.beginPath(); g.arc(at(0), at(8), 5 * px, 0, TAU); g.fill();

  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  const m = new THREE.Mesh(new THREE.PlaneGeometry(S, S), new THREE.MeshStandardMaterial({ map: tex, roughness: 1 }));
  m.rotation.x = -Math.PI / 2;
  m.receiveShadow = true;
  const far = new THREE.Mesh(new THREE.CircleGeometry(400, 48), new THREE.MeshStandardMaterial({ color: 0x7cc243, roughness: 1 }));
  far.rotation.x = -Math.PI / 2; far.position.y = -0.02;
  const g2 = new THREE.Group(); g2.add(m, far);
  return g2;
}

function banner(clan, x, z) {
  const c = document.createElement('canvas');
  c.width = 256; c.height = 160;
  const g = c.getContext('2d');
  g.fillStyle = clan.color; g.fillRect(0, 0, 256, 160);
  g.fillStyle = 'rgba(255,255,255,0.18)'; g.fillRect(0, 128, 256, 32);
  g.fillStyle = '#fff'; g.strokeStyle = 'rgba(0,0,0,0.55)'; g.lineWidth = 8;
  g.textAlign = 'center'; g.textBaseline = 'middle';
  let size = 64;
  do g.font = `${size}px "Lilita One", sans-serif`; while (g.measureText(clan.name).width > 220 && (size -= 4) > 20);
  g.strokeText(clan.name, 128, 68); g.fillText(clan.name, 128, 68);
  const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
  const grp = new THREE.Group();
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.08, 4.2, 8), new THREE.MeshStandardMaterial({ color: 0x7a4b2a }));
  pole.position.y = 2.1;
  const geo = new THREE.PlaneGeometry(1.9, 1.2, 8, 1);
  const pos = geo.attributes.position; // a little wave
  for (let i = 0; i < pos.count; i++) pos.setZ(i, Math.sin((pos.getX(i) + 0.95) * 2.6) * 0.12);
  geo.computeVertexNormals();
  const back = tex.clone(); // the back face gets a mirrored copy, so the name reads right from both sides
  back.wrapS = THREE.RepeatWrapping; back.repeat.x = -1; back.offset.x = 1;
  for (const [map, side] of [[tex, THREE.FrontSide], [back, THREE.BackSide]]) {
    const cloth = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ map, side, roughness: 0.9 }));
    cloth.position.set(0.98, 3.45, 0);
    grp.add(cloth);
  }
  grp.add(pole);
  grp.position.set(x, 0, z);
  grp.traverse(o => { o.castShadow = true; });
  return grp;
}

export function mulberry(a) {
  return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
