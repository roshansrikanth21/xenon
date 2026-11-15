// Xenon Holo mode: SHARDS. Six crystal shards of Xenon float in an arc:
// MUSIC, MEMORY, VOICE, VISION, TIME, POWER. Pinch one to take it out (it
// flies to the front and opens its file with live Xenon data), flip your palm
// up to morph it into its artifact, clap to gather all six into the Xenon
// core, clap again to release them.
//
// The pure helpers (data, layout maths, card + menu content) are named exports
// so they can be tested without a renderer.

// ---- data ----------------------------------------------------------------------------
export const SHARDS = [
  {id: 'music',  label: 'MUSIC',  color: 0x6fc8ff, sides: 6, r: .17, top: .36, bot: .28, artifact: 'VINYL'},
  {id: 'memory', label: 'MEMORY', color: 0xae94ff, sides: 5, r: .16, top: .40, bot: .26, artifact: 'DATA PLATES'},
  {id: 'voice',  label: 'VOICE',  color: 0x6ff3ff, sides: 7, r: .18, top: .33, bot: .30, artifact: 'WAVEFORM RING'},
  {id: 'vision', label: 'VISION', color: 0x7dffc8, sides: 8, r: .17, top: .35, bot: .27, artifact: 'IRIS LENS'},
  {id: 'time',   label: 'TIME',   color: 0x93a9ff, sides: 6, r: .15, top: .42, bot: .30, artifact: 'HOURGLASS'},
  {id: 'power',  label: 'POWER',  color: 0xff92b4, sides: 5, r: .19, top: .34, bot: .26, artifact: 'REACTOR KNOT'},
];
export const CORE = {x: 0, y: 1.35, z: 0};
export const FRONT = {x: 0, y: 1.15, z: 1.4};
export const GESTURES = 'Pinch a shard to take it out or put it back · palm up = artifact, palm down = shard · twist to switch shard · clap to gather or release · fist to move · pinch-hold empty space for the menu';

// Slot i of n on an upward arc (a rainbow over the stage).
export function arcSlot(i, n = SHARDS.length) {
  const span = 124 * Math.PI / 180, a = n > 1 ? -span / 2 + span * i / (n - 1) : 0, R = 2.6;
  return {x: Math.sin(a) * R, y: .15 + Math.cos(a) * R * .72, z: -.25 - Math.cos(a) * .35};
}

// Point along a spiral from `from` into `core`; p = 0 is exactly `from`, p = 1 is on the inner orbit.
export function spiralPoint(from, core, p, spin = 0, rIn = .2) {
  const dx = from.x - core.x, dy = from.y - core.y, r0 = Math.hypot(dx, dy), a0 = Math.atan2(dy, dx);
  const r = r0 * (1 - p) + rIn * p, a = a0 + p * Math.PI * 2.5 + spin * p;
  return {x: core.x + Math.cos(a) * r, y: core.y + Math.sin(a) * r * (1 - .35 * p), z: from.z * (1 - p) + core.z * p + Math.sin(a + 1.1) * .18 * p};
}

const smooth = x => { x = Math.min(1, Math.max(0, x)); return x * x * (3 - 2 * x); };
const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const pct = v => (typeof v === 'number' && isFinite(v) ? Math.round(v) + '%' : '--');

// The FILE card for a shard, from live Xenon status.
export function shardFile(id, st = {}, handCount = 0) {
  st = st || {};
  const top = Array.isArray(st.top) ? st.top.filter(Boolean).slice(0, 5) : [];
  const def = SHARDS.find(s => s.id === id); if (!def) return null;
  const kicker = `SHARD FILE · ${def.label}`;
  switch (id) {
    case 'music': return {kicker, title: esc(st.track || 'Nothing playing'), rows: [
      ['ARTIST', esc(st.artist || '--')], ['STATE', st.playing ? 'PLAYING' : 'PAUSED'],
      ...top.slice(0, 3).map((a, i) => [`TOP ${i + 1}`, esc(a)])],
      body: 'Flip your palm up to hold the vinyl. Menu: PLAY / PAUSE.'};
    case 'memory': return {kicker, title: 'What Xenon has learned', rows: top.length ? top.map((a, i) => [`#${i + 1}`, esc(a)]) : [['TASTE', 'Still learning']],
      body: 'Everything Xenon learns about you stays local, on this laptop. Menu: MY TASTE.'};
    case 'voice': return {kicker, title: 'Xenon voice', rows: [['STATE', esc(String(st.orb || 'idle').toUpperCase())], ['WAKE', 'say "Xenon"']],
      body: 'Say Xenon or hold an open palm and Xenon listens. Menu: TALK.'};
    case 'vision': return {kicker, title: 'Hand control', rows: [['TRACKED', `${handCount} hand${handCount === 1 ? '' : 's'} tracked`], ['MAX', '2 hands']],
      body: esc(GESTURES)};
    case 'time': return {kicker, title: esc(st.time || '--:--'), rows: [['SOURCE', 'Laptop clock']], body: 'Menu: WHAT TIME asks Xenon out loud.'};
    case 'power': return {kicker, title: 'System power', rows: [['CPU', pct(st.cpu)], ['RAM', pct(st.ram)],
      ['MODE', st.xtreme ? 'XTREME' : st.game ? 'GAME MODE' : 'NORMAL']], body: 'Menu: GAME MODE toggles it.'};
  }
  return null;
}

// The per-shard menu action: {label, cmd} asks Xenon, {label, toast} just shows text.
export function shardAction(id, st = {}) {
  st = st || {};
  return ({
    music: {label: 'PLAY / PAUSE', cmd: 'media play pause'},
    memory: {label: 'MY TASTE', cmd: 'what do i listen to most'},
    voice: {label: 'TALK', cmd: 'listen now'},
    vision: {label: 'HAND CONTROL', toast: GESTURES},
    time: {label: 'WHAT TIME', cmd: 'what time is it'},
    power: {label: st.game ? 'GAME MODE · OFF' : 'GAME MODE · ON', cmd: st.game ? 'game mode off' : 'game mode on'},
  })[id] || null;
}

// Menu model: kinds 'form' | 'gather' | 'action' | 'back'.
export function menuSpec(state, st) {
  const out = [], id = state.out >= 0 && SHARDS[state.out] ? SHARDS[state.out].id : null;
  if (id) out.push({kind: 'form', label: state.artifact ? 'SHARD' : 'ARTIFACT'});
  out.push({kind: 'gather', label: state.gathered ? 'RELEASE' : 'GATHER'});
  if (id) { const a = shardAction(id, st); if (a) out.push({kind: 'action', label: a.label, action: a}); out.push({kind: 'back', label: 'PUT BACK'}); }
  return out;
}

// ---- the mode ------------------------------------------------------------------------
export default function create(ctx) {
  const {THREE} = ctx;
  const group = new THREE.Group();
  const add = THREE.AdditiveBlending, DS = THREE.DoubleSide;
  const GOLD = new THREE.Color(0xffd27a), WHITE = new THREE.Color(0xffffff), tmpC = new THREE.Color();
  const IDQ = new THREE.Quaternion(), ONE = new THREE.Vector3(1, 1, 1), ZERO = new THREE.Vector3();
  const warn = e => { try { console.error('[shards]', e); } catch (_) {} };

  // ---- textures --------------------------------------------------------------------------
  function canvas(w, h) { const c = document.createElement('canvas'); c.width = w; c.height = h; return c; }
  const glowTex = (() => { const c = canvas(128, 128), g = c.getContext('2d'), gr = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(.25, 'rgba(255,255,255,.45)'); gr.addColorStop(.6, 'rgba(255,255,255,.08)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.fillRect(0, 0, 128, 128); return new THREE.CanvasTexture(c); })();
  function labelTex(text) {
    const c = canvas(512, 96), g = c.getContext('2d'), sp = 12;
    g.font = '500 34px ui-monospace,Consolas,monospace'; g.textBaseline = 'middle';
    const ws = [...text].map(ch => g.measureText(ch).width), total = ws.reduce((a, b) => a + b, 0) + sp * (ws.length - 1);
    let x = (512 - total) / 2; g.shadowColor = 'rgba(111,243,255,.9)'; g.shadowBlur = 10; g.fillStyle = '#dffbff';
    [...text].forEach((ch, i) => { g.fillText(ch, x, 48); x += ws[i] + sp; });
    g.shadowBlur = 0; g.fillStyle = 'rgba(111,243,255,.55)'; g.fillRect(256 - 40, 80, 80, 2);
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
  }

  // ---- material helpers (each registers its base opacity so fades are cheap) -------------
  function glass(list, col, op = .36) {
    const m = new THREE.MeshPhysicalMaterial({color: col, emissive: col, emissiveIntensity: .22, roughness: .08, metalness: .15, clearcoat: 1, clearcoatRoughness: .1,
      transparent: true, opacity: op, depthWrite: false, side: DS, flatShading: true});
    list.push({m, base: op}); return m;
  }
  function glow(list, col, op = .5, blend = add) {
    const m = new THREE.MeshBasicMaterial({color: col, transparent: true, opacity: op, depthWrite: false, blending: blend, side: DS});
    list.push({m, base: op}); return m;
  }
  function lines(list, accents, col, op = .5) {
    const m = new THREE.LineBasicMaterial({color: col, transparent: true, opacity: op, depthWrite: false, blending: add});
    list.push({m, base: op}); accents.push(m); return m;
  }
  const edges = (geo, mat) => new THREE.LineSegments(new THREE.EdgesGeometry(geo, 1), mat);

  // A faceted bipyramid with a shoulder band, from a lathe profile.
  function crystalGeo(d, k = 1) {
    const V = (x, y) => new THREE.Vector2(x * k, y * k);
    const g = new THREE.LatheGeometry([V(0, -d.bot), V(d.r * .55, -d.bot * .55), V(d.r, -.02), V(d.r * .92, .07), V(d.r * .35, d.top * .78), V(0, d.top)], d.sides);
    g.computeVertexNormals(); return g;
  }

  // ---- artifacts --------------------------------------------------------------------------
  // Each returns {obj, tick(dt, t, st)} and registers its materials into s.artMats / s.accents.
  const ARTIFACTS = {
    music(s, col) {
      const o = new THREE.Group(), spin = new THREE.Group(); o.add(spin); o.rotation.x = -.38;
      const disc = new THREE.CylinderGeometry(.36, .36, .014, 64); disc.rotateX(Math.PI / 2);
      spin.add(new THREE.Mesh(disc, glass(s.artMats, 0x0c2330, .62)));
      const gm = glow(s.artMats, col, .22);
      for (let r = .15; r < .35; r += .028) { const ring = new THREE.Mesh(new THREE.RingGeometry(r, r + .0035, 72), gm); ring.position.z = .009; spin.add(ring); }
      const label = new THREE.Mesh(new THREE.CircleGeometry(.105, 48), glow(s.artMats, col, .55)); label.position.z = .01; spin.add(label);
      const mark = new THREE.Mesh(new THREE.PlaneGeometry(.012, .09), glow(s.artMats, 0xffffff, .7)); mark.position.set(0, .05, .012); spin.add(mark);
      spin.add(edges(new THREE.CylinderGeometry(.36, .36, .014, 48).rotateX(Math.PI / 2), lines(s.artMats, s.accents, col, .35)));
      const sheen = new THREE.Mesh(new THREE.RingGeometry(.12, .35, 48, 1, .35, .7), glow(s.artMats, 0xffffff, .08)); sheen.position.z = .016; o.add(sheen);
      return {obj: o, tick(dt, t, st) { spin.rotation.z -= dt * (st.playing ? 3.5 : 1.1); }};
    },
    memory(s, col) {
      const o = new THREE.Group(); o.rotation.x = .42; const plates = [];
      const geo = new THREE.BoxGeometry(.56, .022, .36), pm = glass(s.artMats, col, .3), lm = lines(s.artMats, s.accents, col, .6), eg = new THREE.EdgesGeometry(geo);
      const cm = glow(s.artMats, col, .5);
      for (let i = 0; i < 5; i++) {
        const p = new THREE.Group(); p.add(new THREE.Mesh(geo, pm)); p.add(new THREE.LineSegments(eg, lm));
        for (let j = 0; j < 3; j++) { const cell = new THREE.Mesh(new THREE.PlaneGeometry(.11 + j * .05, .02), cm); cell.rotation.x = -Math.PI / 2; cell.position.set(-.16 + j * .1, .013, -.1 + j * .08); p.add(cell); }
        o.add(p); plates.push(p);
      }
      const scan = new THREE.Mesh(new THREE.PlaneGeometry(.62, .006), glow(s.artMats, 0xffffff, .5)); o.add(scan);
      return {obj: o, tick(dt, t) {
        plates.forEach((p, i) => { p.position.y = (i - 2) * (.1 + .018 * Math.sin(t * 1.6 + i)); p.position.x = Math.sin(t * .7 + i * .9) * .025; });
        scan.position.y = Math.sin(t * 1.3) * .24; o.rotation.y += dt * .25;
      }};
    },
    voice(s, col) {
      const o = new THREE.Group(), N = 40, bars = [];
      const geo = new THREE.BoxGeometry(.016, 1, .016); geo.translate(0, .5, 0);
      const bm = glow(s.artMats, col, .85);
      for (let i = 0; i < N; i++) {
        const a = i / N * Math.PI * 2, b = new THREE.Mesh(geo, bm);
        b.position.set(Math.cos(a) * .2, Math.sin(a) * .2, 0); b.rotation.z = a - Math.PI / 2; o.add(b); bars.push(b);
      }
      o.add(new THREE.Mesh(new THREE.TorusGeometry(.19, .004, 8, 96), glow(s.artMats, 0xffffff, .55)));
      o.add(new THREE.Mesh(new THREE.TorusGeometry(.42, .003, 8, 96), glow(s.artMats, col, .3)));
      const dot = new THREE.Mesh(new THREE.SphereGeometry(.07, 24, 16), glass(s.artMats, col, .45)); o.add(dot);
      o.add(edges(new THREE.IcosahedronGeometry(.075, 1), lines(s.artMats, s.accents, col, .4)));
      return {obj: o, tick(dt, t, st) {
        const live = {speaking: 1, listening: .8, heard: .9, thinking: .5}[st.orb] ?? .28;
        bars.forEach((b, i) => { const h = .03 + live * .17 * (.5 + .5 * Math.sin(t * 6 + i * .7) * Math.sin(t * 2.3 + i * .31)); b.scale.y = Math.max(.01, h); });
        o.rotation.z += dt * .2;
      }};
    },
    vision(s, col) {
      const o = new THREE.Group();
      o.add(new THREE.Mesh(new THREE.TorusGeometry(.32, .012, 12, 96), glow(s.artMats, col, .7)));
      const iris = new THREE.Mesh(new THREE.RingGeometry(.1, .25, 64), glow(s.artMats, col, .25)); o.add(iris);
      const pts = []; for (let i = 0; i < 36; i++) { const a = i / 36 * Math.PI * 2, r1 = .11, r2 = .24 - (i % 3) * .025; pts.push(Math.cos(a) * r1, Math.sin(a) * r1, .002, Math.cos(a + .08) * r2, Math.sin(a + .08) * r2, .002); }
      const rg = new THREE.BufferGeometry(); rg.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
      const fibres = new THREE.LineSegments(rg, lines(s.artMats, s.accents, col, .6)); o.add(fibres);
      const pupil = new THREE.Mesh(new THREE.CircleGeometry(.075, 40), glow(s.artMats, 0x02080c, .85, THREE.NormalBlending)); pupil.position.z = .004; o.add(pupil);
      const lensGeo = new THREE.SphereGeometry(.26, 32, 16); lensGeo.scale(1, 1, .32);
      const lens = new THREE.Mesh(lensGeo, glass(s.artMats, 0xd8fbff, .18)); o.add(lens);
      const glint = new THREE.Mesh(new THREE.CircleGeometry(.022, 16), glow(s.artMats, 0xffffff, .9)); glint.position.set(-.08, .09, .09); o.add(glint);
      const orbit = new THREE.Mesh(new THREE.TorusGeometry(.4, .0025, 6, 96), glow(s.artMats, col, .35)); orbit.rotation.x = 1.2; o.add(orbit);
      return {obj: o, tick(dt, t) {
        pupil.scale.setScalar(1 + .25 * Math.sin(t * 1.4)); fibres.rotation.z += dt * .15; iris.rotation.z -= dt * .1;
        orbit.rotation.z += dt * .6; o.rotation.y = Math.sin(t * .6) * .35; o.rotation.x = Math.sin(t * .45) * .15;
      }};
    },
    time(s, col) {
      const o = new THREE.Group();
      const cg = new THREE.ConeGeometry(.2, .3, 24, 1, true), gm = glass(s.artMats, 0xd8fbff, .22);
      const top = new THREE.Mesh(cg, gm); top.rotation.x = Math.PI; top.position.y = .15; o.add(top);
      const bot = new THREE.Mesh(cg, gm); bot.position.y = -.15; o.add(bot);
      const lm = lines(s.artMats, s.accents, col, .5), eg = new THREE.EdgesGeometry(new THREE.ConeGeometry(.2, .3, 12, 1, true), 1);
      const te = new THREE.LineSegments(eg, lm); te.rotation.x = Math.PI; te.position.y = .15; o.add(te);
      const be = new THREE.LineSegments(eg, lm); be.position.y = -.15; o.add(be);
      const capG = new THREE.CylinderGeometry(.25, .25, .025, 40), capM = glass(s.artMats, col, .55);
      [.31, -.31].forEach(y => { const c = new THREE.Mesh(capG, capM); c.position.y = y; o.add(c); });
      const postG = new THREE.CylinderGeometry(.007, .007, .62, 6), postM = glow(s.artMats, col, .6);
      for (let i = 0; i < 3; i++) { const a = i / 3 * Math.PI * 2, p = new THREE.Mesh(postG, postM); p.position.set(Math.cos(a) * .23, 0, Math.sin(a) * .23); o.add(p); }
      const sandM = glow(s.artMats, col, .75), pileG = new THREE.ConeGeometry(.16, .12, 24);
      const upper = new THREE.Mesh(pileG, sandM); upper.rotation.x = Math.PI; upper.position.y = .2; o.add(upper);
      const lower = new THREE.Mesh(pileG, sandM); lower.position.y = -.24; o.add(lower);
      const NP = 40, pos = new Float32Array(NP * 3), pg = new THREE.BufferGeometry(); pg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      const pm = new THREE.PointsMaterial({color: col, size: .012, transparent: true, opacity: .9, depthWrite: false, blending: add}); s.artMats.push({m: pm, base: .9});
      const stream = new THREE.Points(pg, pm); o.add(stream);
      return {obj: o, tick(dt, t) {
        const ph = (t % 12) / 12, k = Math.max(.02, 1 - ph);
        upper.scale.set(k, k, k); upper.position.y = .03 + .17 * Math.cbrt(k) * .9 + .02;
        const l = Math.max(.02, ph); lower.scale.set(l, l, l); lower.position.y = -.3 + .06 * l;
        for (let i = 0; i < NP; i++) { const f = ((t * .9 + i / NP) % 1); pos[i * 3] = Math.sin(i * 12.9) * .004; pos[i * 3 + 1] = .01 - f * (.27 - .06 * l); pos[i * 3 + 2] = Math.cos(i * 7.1) * .004; }
        pg.attributes.position.needsUpdate = true; o.rotation.y += dt * .35;
      }};
    },
    power(s, col) {
      const o = new THREE.Group();
      const knot = new THREE.Mesh(new THREE.TorusKnotGeometry(.17, .045, 160, 16, 2, 3), glass(s.artMats, col, .45)); o.add(knot);
      const wire = new THREE.Mesh(new THREE.TorusKnotGeometry(.172, .05, 64, 6, 2, 3), new THREE.MeshBasicMaterial({color: col, wireframe: true, transparent: true, opacity: .18, depthWrite: false, blending: add}));
      s.artMats.push({m: wire.material, base: .18}); s.accents.push(wire.material); o.add(wire);
      const heart = new THREE.Sprite(new THREE.SpriteMaterial({map: glowTex, color: col, transparent: true, opacity: .8, depthWrite: false, blending: add})); s.artMats.push({m: heart.material, base: .8});
      heart.scale.setScalar(.45); o.add(heart);
      const r1 = new THREE.Mesh(new THREE.TorusGeometry(.38, .004, 8, 96), glow(s.artMats, col, .45)), r2 = new THREE.Mesh(new THREE.TorusGeometry(.33, .003, 8, 96), glow(s.artMats, 0xffffff, .35));
      r1.rotation.x = 1.25; r2.rotation.y = 1.1; o.add(r1, r2);
      return {obj: o, tick(dt, t, st) {
        const load = Math.min(1, ((st.cpu || 0) / 100)), sp = .8 + load * 2.2;
        knot.rotation.x += dt * sp * .6; knot.rotation.y += dt * sp; wire.rotation.copy(knot.rotation);
        r1.rotation.z += dt * sp * .7; r2.rotation.x += dt * sp * .5; heart.scale.setScalar(.38 + .08 * Math.sin(t * (4 + load * 6)));
      }};
    },
  };

  // ---- shards ---------------------------------------------------------------------------
  const field = new THREE.Group(); group.add(field);
  const hitGeo = new THREE.SphereGeometry(.4, 12, 8), hitMat = new THREE.MeshBasicMaterial({visible: false});
  const shards = SHARDS.map((d, i) => {
    const s = {def: d, i, crystalMats: [], artMats: [], accents: [], morph: 0, morphT: 0, dim: 1, hoverK: 0, flash: 0, slot: arcSlot(i), scaleNow: 1};
    s.root = new THREE.Group(); s.root.userData.target = true; s.root.userData.shard = s; field.add(s.root);
    s.holder = new THREE.Group(); s.root.add(s.holder);
    s.body = new THREE.Group(); s.holder.add(s.body);
    s.hit = new THREE.Mesh(hitGeo, hitMat); s.root.add(s.hit);
    // crystal
    s.crystal = new THREE.Group(); s.body.add(s.crystal);
    const cg = crystalGeo(d);
    s.crystal.add(new THREE.Mesh(cg, glass(s.crystalMats, d.color, .38)));
    s.crystal.add(new THREE.Mesh(crystalGeo(d, .45), glow(s.crystalMats, d.color, .55)));
    s.crystal.add(edges(cg, lines(s.crystalMats, s.accents, d.color, .55)));
    const facet = new THREE.Mesh(crystalGeo(d, 1.12), new THREE.MeshBasicMaterial({color: d.color, wireframe: true, transparent: true, opacity: .06, depthWrite: false, blending: add}));
    s.crystalMats.push({m: facet.material, base: .06}); s.crystal.add(facet);
    // glow + label live on the holder so they follow the shard in either form
    s.glow = new THREE.Sprite(new THREE.SpriteMaterial({map: glowTex, color: d.color, transparent: true, opacity: .5, depthWrite: false, blending: add}));
    s.glow.scale.setScalar(.9); s.holder.add(s.glow);
    s.label = new THREE.Sprite(new THREE.SpriteMaterial({map: labelTex(d.label), transparent: true, opacity: .9, depthWrite: false}));
    s.label.scale.set(.9, .169, 1); s.label.position.y = -.5; s.root.add(s.label);
    // artifact
    try { const a = ARTIFACTS[d.id](s, d.color); s.artifact = a.obj; s.tick = a.tick; } catch (e) { warn(e); s.artifact = new THREE.Group(); s.tick = () => {}; }
    s.artifact.visible = false; s.body.add(s.artifact);
    s.root.position.set(s.slot.x, s.slot.y, s.slot.z);
    return s;
  });

  // ---- the Xenon core (only while gathered) ----------------------------------------------
  const core = new THREE.Group(); core.position.set(CORE.x, CORE.y, CORE.z); core.userData.target = true; core.userData.core = true; core.visible = false; group.add(core);
  const coreU = {uCharge: {value: 0}, uTime: {value: 0}, uCol: {value: new THREE.Color(0x6ff3ff)}};
  const coreMat = new THREE.ShaderMaterial({uniforms: coreU, transparent: true, depthWrite: false, blending: add,
    vertexShader: 'varying vec3 n;varying vec3 v;void main(){n=normalize(normalMatrix*normal);vec4 p=modelViewMatrix*vec4(position,1.);v=normalize(-p.xyz);gl_Position=projectionMatrix*p;}',
    fragmentShader: 'uniform float uCharge;uniform float uTime;uniform vec3 uCol;varying vec3 n;varying vec3 v;void main(){float d=max(dot(n,v),0.);float f=pow(1.-d,1.8);float c=pow(d,4.);vec3 col=mix(uCol,vec3(1.),c*.6);gl_FragColor=vec4(col*(f*.7+c*.35)*uCharge*(.95+.05*sin(uTime*9.)),1.);}'});
  core.add(new THREE.Mesh(new THREE.SphereGeometry(.46, 40, 28), coreMat));
  const coreWire = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(.5, 1)), new THREE.LineBasicMaterial({color: 0x6ff3ff, transparent: true, opacity: .2, depthWrite: false, blending: add}));
  core.add(coreWire);
  const coreHalo = new THREE.Sprite(new THREE.SpriteMaterial({map: glowTex, color: 0x9ff6ff, transparent: true, opacity: .5, depthWrite: false, blending: add})); coreHalo.scale.setScalar(2); core.add(coreHalo);
  const motes = SHARDS.map((d, i) => { const m = new THREE.Sprite(new THREE.SpriteMaterial({map: glowTex, color: d.color, transparent: true, opacity: .8, depthWrite: false, blending: add})); m.scale.setScalar(.16); m.userData.k = i; core.add(m); return m; });
  const coreHit = new THREE.Mesh(new THREE.SphereGeometry(.55, 12, 8), hitMat); core.add(coreHit);
  const coreLabel = new THREE.Sprite(new THREE.SpriteMaterial({map: labelTex('XENON CORE'), transparent: true, opacity: .9, depthWrite: false})); coreLabel.scale.set(1.1, .206, 1); coreLabel.position.y = -.78; core.add(coreLabel);
  // Only the invisible hit spheres are pickable: the stage's raycaster uses the default
  // Line/Points threshold (1 world unit), so edge lines and the sand stream would otherwise
  // balloon every shard's hit area and let the enlarged front shard swallow nearby picks.
  const noRay = () => {};
  shards.forEach(s => s.root.traverse(o => { if (o !== s.hit) o.raycast = noRay; }));
  core.traverse(o => { if (o !== coreHit) o.raycast = noRay; });
  const CYAN = new THREE.Color(0x6ff3ff); let coreHoverK = 0;

  // ---- state -----------------------------------------------------------------------------
  let out = -1, gathered = false, gp = 0, orbit = 0, hovered = null, lastCardAt = 0, lastSt = null, coreFlash = 0;
  const st = () => { try { return ctx.status() || {}; } catch (e) { return {}; } };
  const handCount = () => { try { return (ctx.hands && ctx.hands() || []).length; } catch (e) { return 0; } };
  const cardOn = () => { try { const el = typeof document !== 'undefined' && document.getElementById('card'); return !!(el && el.classList.contains('on')); } catch (e) { return false; } };
  const say = cmd => { try { Promise.resolve(ctx.xenon(cmd)).then(r => r && ctx.toast(r)).catch(warn); } catch (e) { warn(e); } };

  function showCard() {
    if (out < 0) return;
    try { const c = shardFile(SHARDS[out].id, st(), handCount()); if (c) { ctx.card(c); lastCardAt = performance.now(); lastSt = ctx.status(); } } catch (e) { warn(e); }
  }
  function takeOut(i) {
    if (gathered) release();
    if (out === i) { putBack(); return; }
    if (out >= 0) shards[out].morphT = 0;
    out = i; const s = shards[i]; s.flash = 1; s.morphT = 0; showCard();
  }
  function putBack() {
    if (out < 0) return;
    shards[out].morphT = 0; shards[out].flash = .7; out = -1;
    try { ctx.closeCard(); } catch (e) {}
  }
  function setForm(artifact) {
    if (out < 0) { ctx.toast('Take a shard out first.'); return; }
    const s = shards[out]; if ((s.morphT === 1) === !!artifact) return;
    s.morphT = artifact ? 1 : 0; s.flash = .6;
    ctx.toast(artifact ? `${s.def.label} · ${s.def.artifact}` : `${s.def.label} · SHARD`);
  }
  function gather() {
    putBack(); gathered = true; coreFlash = 1;
    shards.forEach(s => { s.morphT = 0; });
    ctx.toast('GATHERED · THE XENON CORE');
  }
  function release() { gathered = false; coreFlash = .6; ctx.toast('RELEASED'); }
  function resetAll() {
    out = -1; gathered = false; gp = 0; orbit = 0; hovered = null; coreFlash = 0; coreHoverK = 0;
    group.position.set(0, 0, 0); group.rotation.set(0, 0, 0); group.scale.setScalar(1.25);           // a touch larger: shards read small next to real hands
    shards.forEach(s => {
      s.morph = s.morphT = 0; s.dim = 1; s.hoverK = 0; s.flash = 0; s.scaleNow = 1;
      s.root.position.set(s.slot.x, s.slot.y, s.slot.z); s.root.scale.set(1, 1, 1);
      s.holder.position.set(0, 0, 0); s.holder.quaternion.identity(); s.holder.scale.set(1, 1, 1);
      s.body.rotation.set(0, s.i * 1.1, 0); s.crystal.scale.set(1, 1, 1); s.crystal.visible = true; s.artifact.visible = false;
    });
    core.visible = false; core.scale.setScalar(.001);
  }
  const shardOf = obj => { let o = obj; while (o) { if (o.userData && o.userData.shard) return o.userData.shard; if (o.userData && o.userData.core) return 'core'; o = o.parent; } return null; };
  const safe = fn => function (...a) { try { return fn.apply(this, a); } catch (e) { warn(e); } };

  resetAll();
  return {
    id: 'shards', label: 'SHARDS', group,
    hint: 'Pinch a shard to take it out · palm up for its artifact, down for the shard · clap to gather into the core, clap again to release · fist to move',
    enter: safe(() => { resetAll(); }),
    exit: safe(() => { resetAll(); try { ctx.closeCard(); } catch (e) {} }),
    targets: () => { const t = shards.map(s => s.root); if (core.visible && gp > .6) t.push(core); return t; },
    hover: safe(obj => { const s = shardOf(obj); hovered = s && s !== 'core' ? s : s === 'core' ? 'core' : null; }),
    press: safe(obj => {
      const s = shardOf(obj); if (!s) return;
      if (s === 'core' || gathered) { release(); return; }
      takeOut(s.i);
    }),
    hold: safe(obj => { const s = shardOf(obj); if (s && s !== 'core' && s.i === out) setForm(shards[out].morphT !== 1); }),
    manipulable: obj => { try { if (out >= 0) return shards[out].holder; } catch (e) {} return group; },
    clap: safe(() => { gathered ? release() : gather(); }),
    palmFlip: safe(up => { if (out >= 0 && !gathered) setForm(!!up); }),
    twist: safe(dir => { if (out < 0 || gathered) return; takeOut((out + (dir > 0 ? 1 : -1) + shards.length) % shards.length); }),
    key: k => {
      try {
        if (k >= '1' && k <= '6') { takeOut(+k - 1); return true; }
        if (k === 'g') { gathered ? release() : gather(); return true; }
        if (k === 'a' && out >= 0) { setForm(shards[out].morphT !== 1); return true; }
      } catch (e) { warn(e); }
      return false;
    },
    menu() {
      try {
        return menuSpec({out, artifact: out >= 0 && shards[out].morphT === 1, gathered}, st()).map(m => ({label: m.label, run: safe(() => {
          if (m.kind === 'form') setForm(shards[out] && shards[out].morphT !== 1);
          else if (m.kind === 'gather') gathered ? release() : gather();
          else if (m.kind === 'back') putBack();
          else if (m.kind === 'action') { if (m.action.cmd) say(m.action.cmd); else ctx.toast(m.action.toast); if (out >= 0) shards[out].flash = 1; }
        })}));
      } catch (e) { warn(e); return []; }
    },
    update: safe((dt, t) => {
      const S = st(), labelsOn = (() => { try { return !!ctx.labels(); } catch (e) { return true; } })();
      gp = Math.min(1, Math.max(0, gp + (gathered ? dt : -dt) * .85)); const e = smooth(gp);
      orbit += dt * (1.1 + e * .6);
      const kPos = 1 - Math.exp(-dt * 6), kFast = 1 - Math.exp(-dt * 9);
      // core
      core.visible = e > .01; core.scale.setScalar(Math.max(.001, e));
      coreFlash = Math.max(0, coreFlash - dt * 1.5);
      coreU.uCharge.value = e * (.75 + .15 * Math.sin(t * 2.2)) + coreFlash * .8; coreU.uTime.value = t;
      coreU.uCol.value.setHSL(.5 + .04 * Math.sin(t * .7), .9, .62);
      coreHalo.material.opacity = e * (.45 + .25 * Math.sin(t * 1.7)) + coreFlash * .5; coreHalo.scale.setScalar(1.9 + coreFlash * 1.2);
      coreWire.rotation.y += dt * .3; coreWire.rotation.x += dt * .12;
      coreHoverK += ((hovered === 'core' ? 1 : 0) - coreHoverK) * kFast;
      coreWire.material.color.copy(CYAN).lerp(GOLD, coreHoverK * .9); coreWire.material.opacity = .2 + coreHoverK * .4;
      coreLabel.material.color.copy(GOLD).lerp(WHITE, 1 - coreHoverK);
      coreLabel.visible = labelsOn;
      motes.forEach((m, i) => { const a = orbit * 1.3 + i / motes.length * Math.PI * 2, tilt = i * .52;
        m.position.set(Math.cos(a) * .3, Math.sin(a) * .3 * Math.cos(tilt), Math.sin(a) * .3 * Math.sin(tilt)); m.material.opacity = .85 * e; });
      // shards
      for (const s of shards) {
        const isOut = s.i === out && !gathered;
        const sp = spiralPoint(s.slot, CORE, e, orbit + s.i / shards.length * Math.PI * 2 * e);
        const tgt = isOut ? FRONT : sp, bob = Math.sin(t * .9 + s.i * 1.3) * .06 * (1 - e);
        s.root.position.x += (tgt.x - s.root.position.x) * (gathered || gp > 0 ? kFast : kPos);
        s.root.position.y += (tgt.y + bob - s.root.position.y) * (gathered || gp > 0 ? kFast : kPos);
        s.root.position.z += (tgt.z - s.root.position.z) * (gathered || gp > 0 ? kFast : kPos);
        const wantScale = isOut ? 2.05 : 1 - .68 * e;
        s.scaleNow += (wantScale - s.scaleNow) * kPos; s.root.scale.setScalar(Math.max(.01, s.scaleNow));
        if (s.i !== out) {   // ease any fist/resize manipulation back out
          s.holder.position.lerp(ZERO, kPos); s.holder.quaternion.slerp(IDQ, kPos); s.holder.scale.lerp(ONE, kPos);
        }
        s.body.rotation.y += dt * (isOut ? .5 : .35 + e * 2);
        s.body.rotation.z = Math.sin(t * .6 + s.i) * .08 * (1 - e);
        // morph shard <-> artifact
        s.morph += (s.morphT - s.morph) * (1 - Math.exp(-dt * 3.2)); if (Math.abs(s.morphT - s.morph) < .002) s.morph = s.morphT;
        const m = smooth(s.morph);
        s.crystal.visible = m < .995; s.artifact.visible = m > .005;
        s.crystal.scale.setScalar(Math.max(.001, 1 - m * .85)); s.crystal.rotation.y = m * Math.PI * 1.5;
        s.artifact.scale.setScalar(Math.max(.001, .35 + m * .65)); s.artifact.rotation.y = (1 - m) * -Math.PI;
        if (s.artifact.visible) { try { s.tick(dt, t, S); } catch (err) { warn(err); s.tick = () => {}; } }
        // dim / hover / flash
        const wantDim = (out >= 0 && !isOut && !gathered) ? .22 : 1;
        s.dim += (wantDim - s.dim) * kPos;
        const hk = hovered === s ? 1 : 0; s.hoverK += (hk - s.hoverK) * kFast;
        s.flash = Math.max(0, s.flash - dt * 2.2);
        for (const x of s.crystalMats) x.m.opacity = x.base * (1 - m) * s.dim * (1 + s.hoverK * .4 + s.flash * .8);
        for (const x of s.artMats) x.m.opacity = Math.min(1, x.base * m * s.dim * (1 + s.hoverK * .3 + s.flash * .6));
        tmpC.setHex(s.def.color).lerp(GOLD, s.hoverK * .9).lerp(WHITE, s.flash * .8);
        for (const a of s.accents) a.color.copy(tmpC);
        s.glow.material.color.copy(tmpC);
        s.glow.material.opacity = (.42 + .12 * Math.sin(t * 2 + s.i)) * s.dim + s.hoverK * .35 + s.flash * .6;
        s.glow.scale.setScalar((.85 + s.hoverK * .35 + s.flash * .7) * (1 + m * .4));
        s.label.visible = labelsOn && e < .5; s.label.material.opacity = .9 * s.dim * (1 - e * 2);
        s.label.material.color.copy(GOLD).lerp(WHITE, 1 - s.hoverK);
      }
      // keep the open file live (only while the card is still open)
      if (out >= 0 && !gathered) {
        const now = performance.now(), cur = (() => { try { return ctx.status(); } catch (err) { return null; } })();
        if (now - lastCardAt > 1800 && (cur !== lastSt || SHARDS[out].id === 'vision') && cardOn()) showCard();
      }
    }),
  };
}
