// Xenon Holo mode: PERIODIC LAB. A curved glass periodic table of all 118
// elements. Point at a tile to read it, quick-pinch to drop an atom into the mix
// tray (12 max), pinch a chip to take it back out. Clap (or pinch MIX): a real
// molecule unfolds as a ball-and-stick hologram; anything else is fused into a
// brand new (entirely invented) element. Clap with an empty tray for the table.

import {ELEMENTS, BY_SYM, CATEGORY_NAMES, matchMolecule} from './elements.js';

// ---- pure helpers (exported for tests) -----------------------------------------------
export const LAYOUT = {px: .325, py: .37, tw: .29, th: .29 * 152 / 128, depth: .018, R: 6.5, cx: 9.5, cy: 4.3};
const isF = e => e.f !== null && e.f !== undefined;
export function tileSpot(e) {
  const {px, py, R, cx, cy} = LAYOUT;
  const col = isF(e) ? e.f + 3 : e.group;
  const row = isF(e) ? (e.period === 6 ? 7.6 : 8.6) : e.period - 1;
  const fx = (col - cx) * px, th = fx / R;           // wrap round a cylinder that faces the viewer
  return {col, row, x: R * Math.sin(th), y: (cy - row) * py, z: R * (1 - Math.cos(th)), ry: -th};
}
const SUB = '₀₁₂₃₄₅₆₇₈₉';
export const subscript = s => String(s).replace(/\d/g, d => SUB[+d]);
export function hillOrder(counts) {
  const keys = Object.keys(counts).filter(k => counts[k] > 0);
  const c = keys.includes('C');
  return keys.sort((a, b) => {
    const r = s => c ? (s === 'C' ? 0 : s === 'H' ? 1 : 2) : 2;
    return r(a) - r(b) || (a < b ? -1 : a > b ? 1 : 0);
  });
}
export const formulaText = counts => hillOrder(counts).map(k => k + (counts[k] > 1 ? subscript(counts[k]) : '')).join(' · ');
export function countTray(syms) { const c = {}; for (const s of syms) c[s] = (c[s] || 0) + 1; return c; }
export function massOf(counts) { let m = 0; for (const [s, n] of Object.entries(counts)) m += ((BY_SYM[s] || {}).mass || 0) * n; return Math.round(m * 1000) / 1000; }

const VOW = 'aeiouy';
export function rootOf(name) {
  const s = String(name).toLowerCase(); let i = 0;
  while (i < s.length && !VOW.includes(s[i])) i++;
  while (i < s.length && VOW.includes(s[i])) i++;
  return s.slice(0, Math.min(s.length, i + 1));
}
const PREFIX = ['', '', 'di', 'tri', 'tetra', 'penta', 'hexa', 'hepta', 'octa', 'nona', 'deca', 'undeca', 'dodeca'];
function hash(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
const REAL_SYMS = new Set(ELEMENTS.map(e => e.sym));

// Fuse a tray into an invented element: name from the element roots, z > 118, mass = sum.
export function inventElement(syms) {
  const counts = countTray(syms), order = [];
  for (const s of syms) if (!order.includes(s) && BY_SYM[s]) order.push(s);
  if (!order.length) return null;
  const total = order.reduce((n, s) => n + counts[s], 0);
  const roots = order.slice(0, 3).map(s => rootOf(BY_SYM[s].name));
  let stem = order.length === 1 ? PREFIX[Math.min(12, counts[order[0]])] : '';
  for (const r of roots) stem += stem && stem[stem.length - 1] === r[0] ? r.slice(1) : r;
  while (stem.length > 1 && VOW.includes(stem[stem.length - 1])) stem = stem.slice(0, -1);
  if (order.length > 3) stem += 'ex';
  const name = stem[0].toUpperCase() + stem.slice(1) + 'ium';
  const key = hillOrder(counts).map(k => k + counts[k]).join(''), h = hash(key);
  const low = name.toLowerCase();
  let sym = null;
  for (let i = 1; i < low.length && !sym; i++) { const c = low[0].toUpperCase() + low[i]; if (!REAL_SYMS.has(c) && /[a-z]/.test(low[i])) sym = c; }
  if (!sym) sym = low[0].toUpperCase() + low[1] + low[2];
  const halfLife = ['0.8 ms', '41 ns', '3.2 s', '12 µs', '0.6 s', '270 ms', '9 min', '1.5 h'][h % 8];
  return {name, sym, z: 119 + (h % 81), mass: massOf(counts), counts, total, order, halfLife};
}

// ---- the mode --------------------------------------------------------------------------
export default function create(ctx) {
  const {THREE} = ctx;
  const HOLO = 0x6ff3ff, GOLD = 0xffd27a, WHITE = 0xffffff;
  const MONO = 'ui-monospace,Consolas,monospace';
  const MAX_TRAY = 12, TW = 128, TH = 152, BASE_TILT = -.1;
  const labelsOn = () => { try { return ctx.labels ? !!ctx.labels() : true; } catch (e) { return true; } };
  const safe = fn => (...a) => { try { return fn(...a); } catch (e) { console.error('[table]', e); } };

  // colours: each category a soft hue pulled toward the cyan, so it still reads as one hologram
  const HUES = {alkali: 0xff8fb0, alkaline: 0xffa98f, transition: 0x7fc8ff, 'post-transition': 0x8ff0d0, metalloid: 0xb8f08f,
    nonmetal: 0x6ff3ff, halogen: 0xb69cff, noble: 0xe49cff, lanthanide: 0x8faeff, actinide: 0xc29cff};
  const mixHex = (a, b, k) => { const ca = new THREE.Color(a), cb = new THREE.Color(b); return ca.lerp(cb, k).getHex(); };
  const CAT = {}; for (const [c, h] of Object.entries(HUES)) CAT[c] = mixHex(HOLO, h, .5);
  const css = (hex, a) => `rgba(${hex >> 16 & 255},${hex >> 8 & 255},${hex & 255},${a})`;

  const group = new THREE.Group();
  const tableGroup = new THREE.Group(); group.add(tableGroup);
  const resultGroup = new THREE.Group(); group.add(resultGroup);

  function canvasTex(w, h) {
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
    return {c, g: c.getContext('2d'), tex};
  }
  const glow = canvasTex(128, 128);
  { const g = glow.g, gr = g.createRadialGradient(64, 64, 0, 64, 64, 64); gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(.3, 'rgba(255,255,255,.35)'); gr.addColorStop(1, 'rgba(255,255,255,0)'); g.fillStyle = gr; g.fillRect(0, 0, 128, 128); glow.tex.needsUpdate = true; }
  const glowSprite = (color, opacity) => new THREE.Sprite(new THREE.SpriteMaterial({map: glow.tex, color, transparent: true, opacity, depthWrite: false, blending: THREE.AdditiveBlending}));
  function fit(g, text, max, size, weight, family) { let s = size; g.font = `${weight} ${s}px ${family}`; while (g.measureText(text).width > max && s > 6) { s--; g.font = `${weight} ${s}px ${family}`; } }
  function ticks(g, w, h, col, len = 14) {
    g.fillStyle = col; [[4, 4], [w - 4 - len, 4], [4, h - 6], [w - 4 - len, h - 6]].forEach(([x, y]) => g.fillRect(x, y, len, 2));
  }

  // ---- tiles ------------------------------------------------------------------------
  const {tw, th, depth} = LAYOUT;
  const tileGeo = new THREE.BoxGeometry(tw, th, depth), edgeGeo = new THREE.EdgesGeometry(tileGeo);
  const sideMat = new THREE.MeshBasicMaterial({color: HOLO, transparent: true, opacity: .25, depthWrite: false});
  const backMat = new THREE.MeshBasicMaterial({color: 0x0a2230, transparent: true, opacity: .3, depthWrite: false});
  const tiles = [], tileMeshes = [];
  function drawTile(t) {
    const g = t.g, e = t.e, hot = t.hot, col = hot ? GOLD : t.color;
    g.clearRect(0, 0, TW, TH);
    const gr = g.createLinearGradient(0, 0, 0, TH); gr.addColorStop(0, css(col, hot ? .38 : .2)); gr.addColorStop(1, css(col, hot ? .14 : .05));
    g.fillStyle = gr; g.fillRect(0, 0, TW, TH);
    g.strokeStyle = css(col, hot ? 1 : .55); g.lineWidth = hot ? 3 : 1.5; g.strokeRect(2, 2, TW - 4, TH - 4);
    ticks(g, TW, TH, css(col, hot ? 1 : .9), 12);
    g.textBaseline = 'alphabetic'; try { g.letterSpacing = '0px'; } catch (er) {}
    g.textAlign = 'left'; g.font = `600 17px ${MONO}`; g.fillStyle = css(col, 1); g.fillText(String(e.z), 10, 26);
    g.textAlign = 'right'; g.font = `400 11px ${MONO}`; g.fillStyle = css(col, .75); g.fillText(e.mass.toFixed(e.mass < 100 ? 2 : 1), TW - 9, 24);
    g.textAlign = 'center'; fit(g, e.sym, TW - 16, 54, 600, MONO); g.fillStyle = hot ? '#fff6e2' : '#ecfdff'; g.fillText(e.sym, TW / 2, 96);
    try { g.letterSpacing = '1.5px'; } catch (er) {}
    fit(g, e.name.toUpperCase(), TW - 14, 12, 500, MONO); g.fillStyle = css(col, .9); g.fillText(e.name.toUpperCase(), TW / 2, 132);
    try { g.letterSpacing = '0px'; } catch (er) {}
    g.textAlign = 'left'; t.tex.needsUpdate = true;
  }
  for (const e of ELEMENTS) {
    const color = CAT[e.cat] || HOLO, {c, g, tex} = canvasTex(TW, TH);
    const mat = new THREE.MeshBasicMaterial({map: tex, transparent: true, depthWrite: false});
    const mesh = new THREE.Mesh(tileGeo, [sideMat, sideMat, sideMat, sideMat, mat, backMat]);
    const edgeMat = new THREE.LineBasicMaterial({color, transparent: true, opacity: .25, depthWrite: false});
    const edges = new THREE.LineSegments(edgeGeo, edgeMat); edges.scale.setScalar(1.002); edges.raycast = () => {}; mesh.add(edges);  // lines pick with a 1-unit threshold: keep them out of the raycast
    const home = tileSpot(e);
    const t = {e, c, g, tex, mat, edgeMat, mesh, home, color, baseColor: new THREE.Color(color), hot: false, lift: 0, flash: 0,
      d: Math.min(1, Math.hypot(home.x / 3, (home.y - .1) / 1.7))};
    mesh.userData.target = true; mesh.userData.tile = t;
    tableGroup.add(mesh); tiles.push(t); tileMeshes.push(mesh); drawTile(t);
  }
  const flashMesh = new THREE.Mesh(new THREE.PlaneGeometry(tw, th), new THREE.MeshBasicMaterial({color: WHITE, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending}));
  flashMesh.visible = false; tableGroup.add(flashMesh);
  let flashTile = null;

  // title + legend (labels toggle)
  const title = canvasTex(1024, 110);
  { const g = title.g; g.textAlign = 'center'; try { g.letterSpacing = '14px'; } catch (er) {}
    g.font = `500 44px ${MONO}`; g.fillStyle = '#6ff3ff'; g.fillText('PERIODIC LAB', 512, 52);
    try { g.letterSpacing = '4px'; } catch (er) {}
    g.font = `400 17px ${MONO}`; g.fillStyle = 'rgba(190,240,250,.7)'; g.fillText('118 ELEMENTS  ·  POINT TO READ  ·  PINCH TO ADD  ·  CLAP TO MIX', 512, 92);
    g.fillStyle = 'rgba(111,243,255,.35)'; g.fillRect(212, 66, 600, 1); title.tex.needsUpdate = true; }
  const titleSprite = new THREE.Sprite(new THREE.SpriteMaterial({map: title.tex, transparent: true, depthWrite: false}));
  titleSprite.scale.set(3.6, 3.6 * 110 / 1024, 1); titleSprite.position.set(0, (LAYOUT.cy + 1.35) * LAYOUT.py, .3); tableGroup.add(titleSprite);
  const legend = canvasTex(1536, 64);
  { const g = legend.g, cats = Object.keys(HUES), step = 1536 / cats.length; g.textAlign = 'left'; try { g.letterSpacing = '2px'; } catch (er) {}
    cats.forEach((c, i) => { const x = i * step + 14; g.fillStyle = css(CAT[c], 1); g.fillRect(x, 26, 12, 12); g.font = `400 15px ${MONO}`; g.fillStyle = css(CAT[c], .9);
      let s = CATEGORY_NAMES[c] || c.toUpperCase(); if (s.length > 15) s = s.replace('POST-TRANSITION METAL', 'POST-TRANSITION').replace('TRANSITION METAL', 'TRANSITION').replace('ALKALINE EARTH', 'ALKALINE'); g.fillText(s, x + 20, 37); });
    legend.tex.needsUpdate = true; }
  const legendSprite = new THREE.Sprite(new THREE.SpriteMaterial({map: legend.tex, transparent: true, depthWrite: false}));
  legendSprite.scale.set(5.8, 5.8 * 64 / 1536, 1); legendSprite.position.set(0, (LAYOUT.cy - 9.45) * LAYOUT.py, .45); tableGroup.add(legendSprite);

  // floating info label above whatever is pointed at
  const info = canvasTex(768, 150);
  const infoSprite = new THREE.Sprite(new THREE.SpriteMaterial({map: info.tex, transparent: true, depthWrite: false, depthTest: false, opacity: 0}));
  infoSprite.scale.set(2.3, 2.3 * 150 / 768, 1); infoSprite.renderOrder = 20; group.add(infoSprite);
  let infoK = 0, infoAnchor = null;
  function drawInfo(head, sub, color = GOLD) {
    const g = info.g, W = 768, H = 150; g.clearRect(0, 0, W, H);
    g.fillStyle = 'rgba(6,18,26,.72)'; g.fillRect(0, 0, W, H);
    g.strokeStyle = css(color, .9); g.lineWidth = 2; g.strokeRect(3, 3, W - 6, H - 6); ticks(g, W, H, css(color, 1), 26);
    g.textAlign = 'left'; try { g.letterSpacing = '6px'; } catch (er) {}
    fit(g, head, W - 60, 44, 600, MONO); g.fillStyle = '#fff6e2'; g.fillText(head, 30, 66);
    try { g.letterSpacing = '3px'; } catch (er) {}
    fit(g, sub, W - 60, 19, 400, MONO); g.fillStyle = css(color, .95); g.fillText(sub, 30, 112);
    try { g.letterSpacing = '0px'; } catch (er) {}
    info.tex.needsUpdate = true;
  }
  const elementSub = e => `${e.mass} u  ·  ${CATEGORY_NAMES[e.cat] || e.cat}  ·  ${isF(e) ? 'F-BLOCK' : 'GROUP ' + e.group}  ·  PERIOD ${e.period}`;

  // atoms that fly from a tile down to the tray
  const flies = [];
  for (let i = 0; i < MAX_TRAY; i++) { const s = glowSprite(WHITE, 0); s.visible = false; group.add(s); flies.push({s, k: 1, from: new THREE.Vector3(), to: new THREE.Vector3(), on: false}); }
  function fly(fromWorld, color) {
    const f = flies.find(x => !x.on) || flies[0];
    f.from.copy(group.worldToLocal(fromWorld.clone())); f.to.set(0, -.55, 2.2); f.k = 0; f.on = true;
    f.s.material.color.set(color); f.s.visible = true;
  }
  const burst = glowSprite(WHITE, 0); burst.position.set(0, 1.3, .4); burst.visible = false; group.add(burst);
  let burstK = 0;

  // ---- results: molecule or new element ---------------------------------------------------
  const sphereGeo = new THREE.SphereGeometry(1, 28, 18), wireGeo = new THREE.IcosahedronGeometry(1.03, 1);
  const cylGeo = new THREE.CylinderGeometry(1, 1, 1, 14, 1, true);
  const ATOM = {H: [0xeaf6ff, .24], C: [0x7d98a6, .38], N: [0x6f9bff, .36], O: [0xff7b8a, .35], Cl: [0x8fffb4, .44], Na: [0xb49bff, .48], S: [0xd8ff8f, .44]};
  const atomStyle = s => ATOM[s] || [CAT[(BY_SYM[s] || {}).cat] || HOLO, .4];
  let result = null, resultK = 0, resultTarget = 0;
  function symbolSprite(sym, color) {
    const {g, tex} = canvasTex(128, 64); g.textAlign = 'center'; try { g.letterSpacing = '3px'; } catch (er) {}
    g.font = `600 40px ${MONO}`; g.fillStyle = css(color, 1); g.fillText(sym, 64, 46); tex.needsUpdate = true;
    const s = new THREE.Sprite(new THREE.SpriteMaterial({map: tex, transparent: true, depthWrite: false, depthTest: false}));
    s.scale.set(.36, .18, 1); s.renderOrder = 10; return s;
  }
  function newResult(kind) {
    const appear = new THREE.Group(), spin = new THREE.Group(); appear.add(spin); resultGroup.add(appear);
    return {kind, appear, spin, pickables: [], atoms: [], labels: [], extra: [], own: []};
  }
  function buildMolecule(m) {
    const r = newResult('molecule');
    const pts = m.atoms.map(a => new THREE.Vector3(a[1], a[2], a[3]));
    const c = pts.reduce((acc, p) => acc.add(p), new THREE.Vector3()).multiplyScalar(1 / Math.max(1, pts.length));
    pts.forEach(p => p.sub(c));
    const maxR = Math.max(.5, ...pts.map(p => p.length())), k = Math.min(.85, 1.55 / maxR);
    pts.forEach(p => p.multiplyScalar(k));
    const bondMat = new THREE.MeshBasicMaterial({color: HOLO, transparent: true, opacity: .6, depthWrite: false});
    r.own.push(bondMat);
    m.atoms.forEach((a, i) => {
      const [col, rad] = atomStyle(a[0]), radius = rad * Math.min(1, k / .7);
      const mat = new THREE.MeshStandardMaterial({color: col, emissive: col, emissiveIntensity: .35, roughness: .25, metalness: .1, transparent: true, opacity: .85});
      const mesh = new THREE.Mesh(sphereGeo, mat); mesh.scale.setScalar(radius); mesh.position.copy(pts[i]);
      const wire = new THREE.Mesh(wireGeo, new THREE.MeshBasicMaterial({color: HOLO, wireframe: true, transparent: true, opacity: .14, depthWrite: false})); mesh.add(wire);
      const halo = glowSprite(col, .32); halo.scale.setScalar(radius * 3.4); halo.position.copy(pts[i]); r.spin.add(halo);
      const lab = symbolSprite(a[0], col); lab.position.copy(pts[i]).add(new THREE.Vector3(0, radius + .14, 0)); r.spin.add(lab); r.labels.push(lab);
      mesh.userData.target = true; mesh.userData.atom = {sym: a[0], mat, wire, base: new THREE.Color(col)};
      r.spin.add(mesh); r.pickables.push(mesh); r.atoms.push(mesh); r.own.push(mat, wire.material, halo.material, lab.material, lab.material.map);
    });
    const up = new THREE.Vector3(0, 1, 0);
    for (const [i, j, order] of m.bonds) {
      const a = pts[i], b = pts[j]; if (!a || !b) continue;
      const dir = b.clone().sub(a), len = dir.length(); if (len < 1e-4) continue; dir.normalize();
      let perp = new THREE.Vector3().crossVectors(dir, new THREE.Vector3(0, 0, 1));
      if (perp.lengthSq() < 1e-4) perp.crossVectors(dir, up); perp.normalize();
      const n = Math.max(1, Math.min(3, order | 0)), rad = n === 1 ? .05 : .034, gap = .1;
      for (let q = 0; q < n; q++) {
        const cyl = new THREE.Mesh(cylGeo, bondMat), off = (q - (n - 1) / 2) * gap;
        cyl.position.copy(a).add(b).multiplyScalar(.5).addScaledVector(perp, off);
        cyl.quaternion.setFromUnitVectors(up, dir); cyl.scale.set(rad, len, rad); r.spin.add(cyl);
      }
    }
    r.spin.rotation.set(.35, 0, 0);
    return r;
  }
  function drawBig(g, el, hot) {
    const W = 512, H = 608, col = hot ? GOLD : HOLO; g.clearRect(0, 0, W, H);
    const gr = g.createLinearGradient(0, 0, 0, H); gr.addColorStop(0, css(col, .3)); gr.addColorStop(1, css(col, .06)); g.fillStyle = gr; g.fillRect(0, 0, W, H);
    g.strokeStyle = css(col, .9); g.lineWidth = 3; g.strokeRect(4, 4, W - 8, H - 8); ticks(g, W, H, css(col, 1), 34);
    g.textAlign = 'left'; g.font = `600 54px ${MONO}`; g.fillStyle = css(col, 1); g.fillText(String(el.z), 30, 78);
    g.textAlign = 'right'; g.font = `400 24px ${MONO}`; g.fillStyle = css(col, .8); g.fillText(el.real ? 'ELEMENT' : 'NEW', W - 30, 70);
    g.textAlign = 'center'; fit(g, el.sym, W - 60, 220, 600, MONO); g.fillStyle = '#f4feff'; g.fillText(el.sym, W / 2, 360);
    try { g.letterSpacing = '8px'; } catch (er) {}
    fit(g, el.name.toUpperCase(), W - 50, 40, 500, MONO); g.fillStyle = css(col, 1); g.fillText(el.name.toUpperCase(), W / 2, 452);
    try { g.letterSpacing = '4px'; } catch (er) {}
    g.font = `400 24px ${MONO}`; g.fillStyle = css(col, .85); g.fillText(`${el.mass} u`, W / 2, 506);
    fit(g, el.from || '', W - 60, 20, 400, MONO); g.fillStyle = 'rgba(220,245,250,.6)'; g.fillText(el.from || '', W / 2, 556);
    try { g.letterSpacing = '0px'; } catch (er) {}
    g.textAlign = 'left';
  }
  function buildElement(el) {
    const r = newResult('element');
    const geo = new THREE.BoxGeometry(1.5, 1.5 * 608 / 512, .05), eg = new THREE.EdgesGeometry(geo);
    const {g, tex} = canvasTex(512, 608); drawBig(g, el, false); tex.needsUpdate = true;
    const front = new THREE.MeshBasicMaterial({map: tex, transparent: true, depthWrite: false});
    const side = new THREE.MeshBasicMaterial({color: HOLO, transparent: true, opacity: .35, depthWrite: false});
    const back = new THREE.MeshBasicMaterial({color: 0x0a2230, transparent: true, opacity: .4, depthWrite: false});
    const mesh = new THREE.Mesh(geo, [side, side, side, side, front, back]);
    const edgeMat = new THREE.LineBasicMaterial({color: HOLO, transparent: true, opacity: .8, depthWrite: false});
    { const ln = new THREE.LineSegments(eg, edgeMat); ln.raycast = () => {}; mesh.add(ln); }
    const halo = glowSprite(HOLO, .45); halo.scale.setScalar(3.6); halo.position.z = -.2;
    r.spin.add(halo, mesh);
    const ringMat = new THREE.MeshBasicMaterial({color: HOLO, transparent: true, opacity: .45, depthWrite: false, blending: THREE.AdditiveBlending});
    const ringGeo = new THREE.TorusGeometry(1.28, .006, 6, 160);
    const rings = [0, 1].map(i => { const ring = new THREE.Group(), tor = new THREE.Mesh(ringGeo, ringMat); ring.add(tor); ring.rotation.set(1.2 + i * .5, i * .9, 0); r.appear.add(ring); return ring; });
    const electrons = [];
    const nE = Math.min(12, Math.max(2, el.total || 2));
    for (let i = 0; i < nE; i++) { const s = glowSprite(i % 3 ? HOLO : WHITE, .9); s.scale.setScalar(.16); rings[i % 2].add(s); electrons.push({s, a: i / nE * Math.PI * 2, ring: i % 2}); r.own.push(s.material); }
    mesh.userData.target = true; mesh.userData.big = {el, g, tex, edgeMat, side};
    r.pickables.push(mesh); r.extra = {rings, electrons, halo};
    r.own.push(geo, eg, ringGeo, front, side, back, edgeMat, halo.material, ringMat, tex);
    return r;
  }
  function clearResultNow() {
    if (!result) return;
    try { resultGroup.remove(result.appear); result.own.forEach(o => o && o.dispose && o.dispose()); } catch (e) {}
    if (infoAnchor && infoAnchor.result) infoAnchor = null;
    result = null; resultK = 0; resultTarget = 0;
  }

  // ---- tray + actions --------------------------------------------------------------------
  let tray = [], tableK = 0, tableTarget = 1, hotTile = null, hotAtom = null, hotBig = null;
  function syncTray() { try { ctx.setTray(tray.slice(), mix); } catch (e) {} }
  function addAtom(t) {
    if (tray.length >= MAX_TRAY) { ctx.toast(`The mix tray is full (${MAX_TRAY} atoms) · pinch MIX or a chip`); return; }
    tray.push(t.e.sym); syncTray();
    t.flash = 1; flashTile = t;
    fly(t.mesh.getWorldPosition(new THREE.Vector3()), t.color);
  }
  function setHotTile(t) {
    if (hotTile === t) return;
    if (hotTile) { hotTile.hot = false; drawTile(hotTile); }
    hotTile = t || null;
    if (hotTile) { hotTile.hot = true; drawTile(hotTile); const e = hotTile.e; drawInfo(`${e.z}  ${e.sym}  ${e.name.toUpperCase()}`, elementSub(e)); infoAnchor = {mesh: hotTile.mesh, dy: .4}; }
  }
  function setHotAtom(mesh) {
    if (hotAtom === mesh) return;
    if (hotAtom) { const a = hotAtom.userData.atom; a.mat.emissive.copy(a.base); a.mat.emissiveIntensity = .35; a.wire.material.color.set(HOLO); a.wire.material.opacity = .14; }
    hotAtom = mesh || null;
    if (hotAtom) {
      const a = hotAtom.userData.atom, e = BY_SYM[a.sym];
      a.mat.emissive.set(GOLD); a.mat.emissiveIntensity = .8; a.wire.material.color.set(GOLD); a.wire.material.opacity = .5;
      if (e) { drawInfo(`${e.sym}  ${e.name.toUpperCase()}`, `${e.mass} u  ·  ${CATEGORY_NAMES[e.cat]}  ·  Z ${e.z}`); infoAnchor = {mesh: hotAtom, dy: .45, result: true}; }
    }
  }
  function setHotBig(mesh) {
    if (hotBig === mesh) return;
    if (hotBig) { const b = hotBig.userData.big; b.edgeMat.color.set(HOLO); b.side.color.set(HOLO); drawBig(b.g, b.el, false); b.tex.needsUpdate = true; }
    hotBig = mesh || null;
    if (hotBig) { const b = hotBig.userData.big; b.edgeMat.color.set(GOLD); b.side.color.set(GOLD); drawBig(b.g, b.el, true); b.tex.needsUpdate = true; }
  }
  function clearHover() {
    setHotTile(null); try { setHotAtom(null); } catch (e) { hotAtom = null; } try { setHotBig(null); } catch (e) { hotBig = null; }
    infoAnchor = null;
  }

  function showTable() {
    try {
      clearHover();
      try { ctx.closeCard(); } catch (e) {}
      tableTarget = 1; resultTarget = 0;
      // back where it belongs, whatever fist-move or resize did to it
      tableGroup.position.set(0, 0, 0); tableGroup.scale.setScalar(1); delete tableGroup.userData._base;
    } catch (e) { console.error('[table]', e); }
  }
  function showElementCard(e) {
    ctx.card({kicker: 'ELEMENT ' + e.z, title: e.name, rows: [['SYMBOL', e.sym], ['ATOMIC NO.', e.z], ['MASS', e.mass + ' u'],
      ['CATEGORY', CATEGORY_NAMES[e.cat]], ['POSITION', isF(e) ? `F-BLOCK · PERIOD ${e.period}` : `GROUP ${e.group} · PERIOD ${e.period}`]],
      body: 'Quick-pinch the tile to add an atom to the mix.'});
  }
  function mix() {
    try {
      if (!tray.length) { if (result) showTable(); else ctx.toast('The tray is empty · pinch a tile to add an atom'); return; }
      const syms = tray.slice(), counts = countTray(syms), m = matchMolecule(counts);
      clearHover(); clearResultNow();
      if (m) {
        result = buildMolecule(m);
        ctx.card({kicker: 'MOLECULE · MATCHED', title: m.name, rows: [['FORMULA', subscript(m.formula)], ['ATOMS', m.atoms.length],
          ['BONDS', m.bonds.length], ['MASS', massOf(counts) + ' u']], body: m.fact || ''});
      } else {
        let el;
        if (syms.length === 1) { const e = BY_SYM[syms[0]]; el = {...e, real: true, total: 1, from: 'A SINGLE ATOM'}; }
        else { el = inventElement(syms); if (!el) { ctx.toast('Nothing to mix'); return; } el.from = 'FUSED FROM ' + formulaText(counts); }
        result = buildElement(el);
        if (el.real) ctx.card({kicker: 'SINGLE ATOM', title: el.name, rows: [['SYMBOL', el.sym], ['ATOMIC NO.', el.z], ['MASS', el.mass + ' u'], ['CATEGORY', CATEGORY_NAMES[el.cat]]],
          body: 'One atom on its own is just itself. Add a partner and mix again.'});
        else ctx.card({kicker: 'NEW ELEMENT · UNCONFIRMED', title: el.name, rows: [['SYMBOL', el.sym], ['ATOMIC NO.', el.z], ['MASS', el.mass + ' u'],
          ['FUSED FROM', formulaText(counts)], ['HALF-LIFE', el.halfLife]], body: 'Not a real molecule, so the lab invented an element. Purely a hologram: nothing like it exists.'});
      }
      result.appear.scale.setScalar(.001);
      resultK = 0; resultTarget = 1; tableTarget = 0; burstK = 1; burst.visible = true;
      tray = []; syncTray();
      resultGroup.position.set(0, 1.3, .3); resultGroup.rotation.set(0, 0, 0); resultGroup.scale.setScalar(1);
    } catch (e) { console.error('[table] mix', e); }
  }
  const removeChip = i => { if (i >= 0 && i < tray.length) { tray.splice(i, 1); syncTray(); } };

  // ---- per-frame --------------------------------------------------------------------------
  const v3 = new THREE.Vector3(), WHITE_C = new THREE.Color(WHITE);
  const ease = x => x * x * (3 - 2 * x);
  function update(dt, t) {
    const lab = labelsOn();
    tableK += (tableTarget - tableK) * Math.min(1, dt * 2.6);
    if (Math.abs(tableTarget - tableK) < .002) tableK = tableTarget;
    tableGroup.visible = tableK > .003;
    if (tableGroup.visible) {
      // keep the table readable: gently settle back after fist/flick
      const s = Math.min(1, dt * .8);
      tableGroup.rotation.x += (BASE_TILT - tableGroup.rotation.x) * s; tableGroup.rotation.y += (0 - tableGroup.rotation.y) * s; tableGroup.rotation.z += (0 - tableGroup.rotation.z) * s * 2;
      sideMat.opacity = .25 * tableK; backMat.opacity = .3 * tableK;
      const sweep = (t * 3.2) % 34 - 8;
      for (const tl of tiles) {
        const h = tl.home, k = ease(Math.min(1, Math.max(0, tableK * 1.5 - tl.d * .5)));
        tl.lift += ((tl.hot ? 1 : 0) - tl.lift) * Math.min(1, dt * 10);
        tl.flash = Math.max(0, tl.flash - dt * 2.8);
        const fx = h.x * .18, fy = h.y * .18 + .1, fz = -1.2, push = tl.lift * .2 + tl.flash * .08;
        tl.mesh.position.set(fx + (h.x - fx) * k + Math.sin(h.ry) * push, fy + (h.y - fy) * k, fz + (h.z - fz) * k + Math.cos(h.ry) * push);
        tl.mesh.rotation.set((1 - k) * 1.3, h.ry, 0);
        tl.mesh.scale.setScalar((.25 + .75 * k) * (1 + .16 * tl.lift));
        tl.mesh.renderOrder = tl.hot ? 5 : 0;
        tl.mat.opacity = k;
        const glowK = Math.exp(-((h.col - sweep) ** 2) / 3);
        tl.edgeMat.opacity = (.2 + glowK * .35 + tl.lift * .7 + tl.flash) * k;
        if (tl.hot) tl.edgeMat.color.set(GOLD); else tl.edgeMat.color.copy(tl.baseColor).lerp(WHITE_C, Math.min(1, tl.flash));
      }
      if (flashTile && flashTile.flash > 0) {
        flashMesh.visible = true; flashMesh.position.copy(flashTile.mesh.position); flashMesh.rotation.copy(flashTile.mesh.rotation);
        flashMesh.translateZ(depth / 2 + .004); flashMesh.scale.copy(flashTile.mesh.scale); flashMesh.material.opacity = flashTile.flash * .7;
      } else { flashMesh.visible = false; flashTile = null; }
      titleSprite.material.opacity = legendSprite.material.opacity = lab ? tableK : 0;
      titleSprite.visible = legendSprite.visible = lab;
    }
    // result
    if (result) {
      resultK += (resultTarget - resultK) * Math.min(1, dt * 3.2);
      const k = ease(Math.min(1, Math.max(0, resultK)));
      result.appear.scale.setScalar(Math.max(.001, k));
      result.appear.rotation.y = (1 - k) * 1.6;
      result.spin.rotation.y += dt * .35;
      result.appear.position.y = Math.sin(t * .9) * .05;
      result.labels.forEach(s => { s.visible = lab; s.material.opacity = k; });
      if (result.kind === 'element' && result.extra.rings) {
        const {rings, electrons, halo} = result.extra;
        rings[0].rotation.z += dt * .5; rings[1].rotation.z -= dt * .35;
        electrons.forEach(e => { e.a += dt * (e.ring ? 1.1 : .8); e.s.position.set(Math.cos(e.a) * 1.28, Math.sin(e.a) * 1.28, 0); });
        halo.material.opacity = (.35 + .12 * Math.sin(t * 2.4)) * k;
        result.spin.rotation.y = Math.sin(t * .5) * .35;
      }
      if (resultTarget === 0 && resultK < .01) clearResultNow();
    }
    // burst flash on mix
    if (burstK > 0) { burstK = Math.max(0, burstK - dt * 1.8); burst.material.opacity = burstK * .9; burst.scale.setScalar(1 + (1 - burstK) * 5); if (!burstK) burst.visible = false; }
    // flying atoms
    for (const f of flies) {
      if (!f.on) continue;
      f.k = Math.min(1, f.k + dt / .7); const k = f.k, ik = 1 - k;
      const mx = (f.from.x + f.to.x) / 2, my = Math.max(f.from.y, f.to.y) + .6, mz = (f.from.z + f.to.z) / 2 + .5;
      f.s.position.set(ik * ik * f.from.x + 2 * ik * k * mx + k * k * f.to.x, ik * ik * f.from.y + 2 * ik * k * my + k * k * f.to.y, ik * ik * f.from.z + 2 * ik * k * mz + k * k * f.to.z);
      f.s.scale.setScalar(.45 - .3 * k); f.s.material.opacity = k < .8 ? .95 : (1 - k) * 4.75;
      if (k >= 1) { f.on = false; f.s.visible = false; }
    }
    // info label follows its anchor
    const want = lab && infoAnchor && infoAnchor.mesh && infoAnchor.mesh.parent ? 1 : 0;
    infoK += (want - infoK) * Math.min(1, dt * 9);
    infoSprite.visible = infoK > .01; infoSprite.material.opacity = infoK;
    if (want) {
      infoAnchor.mesh.getWorldPosition(v3); group.worldToLocal(v3);
      infoSprite.position.set(Math.max(-2.3, Math.min(2.3, v3.x)), v3.y + infoAnchor.dy, v3.z + .35);
    }
  }

  function resetAll() {
    clearHover(); clearResultNow();
    tray = []; tableK = 0; tableTarget = 1; burstK = 0; burst.visible = false; infoK = 0; infoSprite.visible = false;
    flies.forEach(f => { f.on = false; f.s.visible = false; });
    tiles.forEach(tl => { if (tl.hot) { tl.hot = false; drawTile(tl); } tl.lift = 0; tl.flash = 0; });   // only redraw dirty tiles: 118 canvas re-uploads per mode switch caused a hitch
    flashTile = null; flashMesh.visible = false;
    tableGroup.position.set(0, 1.25, 0); tableGroup.rotation.set(BASE_TILT, 0, 0); tableGroup.scale.setScalar(1);
    resultGroup.position.set(0, 1.3, .3); resultGroup.rotation.set(0, 0, 0); resultGroup.scale.setScalar(1);
  }

  return {
    id: 'table', label: 'PERIODIC LAB', group,
    hint: 'Point to read an element · quick pinch adds an atom · clap or MIX to combine · clap with an empty tray for the table',
    enter: safe(() => { resetAll(); syncTray(); }),
    exit: safe(() => { try { ctx.setTray(null); } catch (e) {} resetAll(); tableK = 0; }),
    update: safe(update),
    targets() {
      if (result && resultTarget === 1) return result.pickables;
      if (tableTarget === 1 && tableK > .6) return tileMeshes;
      return [];
    },
    hover: safe(obj => {
      const ud = (obj && obj.userData) || {};
      setHotTile(ud.tile || null); setHotAtom(ud.atom ? obj : null); setHotBig(ud.big ? obj : null);
      if (!ud.tile && !ud.atom) infoAnchor = null;
    }),
    press: safe(obj => {
      const ud = (obj && obj.userData) || {};
      if (ud.tile && tableTarget === 1) addAtom(ud.tile);
      else if (ud.atom || ud.big) { burstK = Math.max(burstK, .35); burst.visible = true; }
    }),
    hold: safe(obj => {
      const ud = (obj && obj.userData) || {};
      if (ud.tile) showElementCard(ud.tile.e);
      else if (ud.atom && BY_SYM[ud.atom.sym]) showElementCard(BY_SYM[ud.atom.sym]);
    }),
    manipulable: () => (result && resultTarget === 1 ? resultGroup : tableGroup),
    clap: safe(() => { if (tray.length) mix(); else showTable(); }),
    chip: safe(i => removeChip(+i)),
    key: k => {
      try {
        if (k === 'x') { mix(); return true; }
        if (k === 't') { showTable(); return true; }
        if (k === 'backspace') { removeChip(tray.length - 1); return true; }
      } catch (e) { console.error('[table]', e); }
      return false;
    },
    menu: () => [
      {label: 'PERIODIC TABLE', run: showTable},
      {label: 'MIX', run: mix},
      {label: 'CLEAR TRAY', run: () => { tray = []; syncTray(); }},
    ],
  };
}
