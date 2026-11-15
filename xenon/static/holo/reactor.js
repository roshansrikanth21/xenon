// Xenon Holo mode: ARC REACTOR. An original procedural toroidal energy cell
// (the "XR-7"), built only from three.js geometry. Sixteen named parts sit
// assembled until you pull two fists apart (or twist a fist counter-clockwise);
// then they float into an exploded view. Pinch-drag a part out of its slot,
// let go near the slot and it snaps home; hold a pinch on a part for its spec
// card. Clap (or twist clockwise) and the cell reassembles.

// ---- pure data + logic (no THREE needed; exported for tests) -----------------------------
const D = a => a * Math.PI / 180;
const BOLT_R = 1.18;
const bolt = (id, name, deg, order) => {
  const c = Math.cos(D(deg)), s = Math.sin(D(deg));
  return {id, name, order, rad: .12, kind: 'bolt', deg,
    home: [c * BOLT_R, s * BOLT_R, .09], homeRot: [0, 0, 0],
    exp: [c * 1.78, s * 1.78, .95], expRot: [D(s > 0 ? -18 : 18), D(c > 0 ? 18 : -18), D(90)],
    lbl: [c * .42, s * .3, 0],
    spec: [['Material', 'Ti-6Al-4V, nitrided'], ['Torque', '24 N·m'], ['Function', 'Clamps housing to chassis'], ['Thread', 'M5 × 0.8, left hand']],
    body: 'One of four mounting bolts. Left-hand threads so the cell\'s own spin tightens them.'};
};

export const PARTS = [
  {id: 'back', name: 'BACK PLATE', order: 15, rad: 1.32, kind: 'back',
    home: [0, 0, -.26], homeRot: [0, 0, 0], exp: [0, 0, -1.55], expRot: [0, 0, D(-8)], lbl: [-.2, 1.55, 0],
    spec: [['Material', 'Borosilicate laminate'], ['Function', 'Rear shield, heat spread'], ['Thickness', '6.2 mm'], ['Rated', '900 °C']],
    body: 'The cell\'s foundation. Engraved index rings align every other layer during assembly.'},
  {id: 'fins', name: 'HEAT SINK FINS', order: 14, rad: 1.3, kind: 'fins',
    home: [0, 0, -.16], homeRot: [0, 0, 0], exp: [0, 0, -.98], expRot: [0, 0, D(12)], lbl: [.2, -1.62, 0],
    spec: [['Material', 'Graphene-aluminium'], ['Function', 'Radiates waste heat'], ['Dissipation', '1.8 kW'], ['Fins', '24 radial']],
    body: 'Twenty-four radial blades pull heat off the housing and shed it backward, away from the user.'},
  {id: 'housing', name: 'HOUSING RING', order: 13, rad: 1.28, kind: 'housing',
    home: [0, 0, 0], homeRot: [0, 0, 0], exp: [0, 0, -.36], expRot: [0, 0, 0], lbl: [-.35, 1.82, 0],
    spec: [['Material', 'Titanium-niobium'], ['Function', 'Structural frame'], ['Notches', '12 alignment tabs'], ['Mass', '412 g']],
    body: 'The outer frame. Its twelve glowing notches key the coil banks into exact angular position.'},
  {id: 'coilN', name: 'COIL BANK · UPPER', order: 10, rad: .3, kind: 'coils', half: 0,
    home: [0, 0, .02], homeRot: [0, 0, 0], exp: [0, .9, .32], expRot: [D(-24), 0, 0], lbl: [1.25, 1.1, 0],
    spec: [['Material', 'Copper / YBCO windings'], ['Function', 'Upper toroidal field'], ['Field', '3.4 T'], ['Coils', '5 × 320 turns']],
    body: 'Five superconducting coils. Together with the lower bank they wind the field into a closed torus.'},
  {id: 'coilS', name: 'COIL BANK · LOWER', order: 11, rad: .3, kind: 'coils', half: 1,
    home: [0, 0, .02], homeRot: [0, 0, 0], exp: [0, -.9, .32], expRot: [D(24), 0, 0], lbl: [-1.25, -1.1, 0],
    spec: [['Material', 'Copper / YBCO windings'], ['Function', 'Lower toroidal field'], ['Field', '3.4 T'], ['Coils', '5 × 320 turns']],
    body: 'The mirror bank. Counter-wound so the two halves cancel stray field outside the housing.'},
  {id: 'contain', name: 'CONTAINMENT RING', order: 9, rad: .66, kind: 'contain',
    home: [0, 0, .1], homeRot: [0, 0, 0], exp: [0, 0, .56], expRot: [0, 0, 0], lbl: [.25, -.98, 0],
    spec: [['Material', 'Sapphire-clad niobium'], ['Function', 'Holds the plasma torus'], ['Tolerance', '± 2 µm']],
    body: 'A polished hoop that keeps the plasma torus centred. The dashed outer halo is its live field read-out.'},
  {id: 'spokes', name: 'CONDUIT SPOKES', order: 8, rad: .62, kind: 'spokes',
    home: [0, 0, .12], homeRot: [0, 0, 0], exp: [0, 0, .84], expRot: [0, 0, D(30)], lbl: [-.2, .96, 0],
    spec: [['Material', 'Silver-carbon'], ['Function', 'Six-way power take-off'], ['Current', '640 A per spoke']],
    body: 'Six conduits carry power from the core seat out to the housing terminals.'},
  {id: 'hex', name: 'HEX CORE PLATE', order: 7, rad: .44, kind: 'hex',
    home: [0, 0, .16], homeRot: [0, 0, 0], exp: [0, 0, 1.12], expRot: [0, 0, D(-30)], lbl: [.2, -.82, 0],
    spec: [['Material', 'Hexagonal boron nitride'], ['Function', 'Core seat, insulator'], ['Rating', '48 kV']],
    body: 'The six-sided seat the energy column stands on. Electrically insulating, thermally conductive.'},
  {id: 'energy', name: 'ENERGY CYLINDER', order: 6, rad: .22, kind: 'energy',
    home: [0, 0, .3], homeRot: [0, 0, 0], exp: [0, 0, 1.42], expRot: [0, 0, 0], lbl: [-.1, .72, 0],
    spec: [['Material', 'Contained plasma column'], ['Output', '3.2 GW (nominal)'], ['Core temp', '1.1 × 10⁷ K'], ['Function', 'Primary energy source']],
    body: 'The heart of the cell. Out of its field it starts to flicker; put it back before it gets lonely.'},
  {id: 'cage', name: 'FLUX CAGE', order: 5, rad: .32, kind: 'cage',
    home: [0, 0, .3], homeRot: [0, 0, 0], exp: [.62, .46, 1.76], expRot: [D(20), D(20), 0], lbl: [.3, .5, 0],
    spec: [['Material', 'Icosahedral lattice'], ['Function', 'Stabilises field lines'], ['Nodes', '42']],
    body: 'A geodesic lattice around the column. Each node pins a field line so the plasma cannot wander.'},
  bolt('boltNE', 'MOUNT BOLT · NE', 45, 4),
  bolt('boltNW', 'MOUNT BOLT · NW', 135, 3),
  bolt('boltSW', 'MOUNT BOLT · SW', 225, 2),
  bolt('boltSE', 'MOUNT BOLT · SE', 315, 1),
  {id: 'bezel', name: 'LENS BEZEL', order: 12, rad: .76, kind: 'bezel',
    home: [0, 0, .44], homeRot: [0, 0, 0], exp: [0, 0, 2.06], expRot: [0, 0, D(-15)], lbl: [-.2, 1.04, 0],
    spec: [['Material', 'Anodised titanium'], ['Function', 'Seals the lens'], ['Index ticks', '72'], ['Seal', 'IP68']],
    body: 'Clamps the lens to the cell. Seventy-two index ticks for calibrating the field read-out.'},
  {id: 'lens', name: 'LENS COVER', order: 0, rad: .64, kind: 'lens',
    home: [0, 0, .45], homeRot: [0, 0, 0], exp: [0, 0, 2.38], expRot: [D(-10), D(14), 0], lbl: [.2, -.9, 0],
    spec: [['Material', 'Fused quartz dome'], ['Function', 'Shields the core'], ['Transmission', '97.4 %']],
    body: 'A thin quartz dome. It lets the light out and keeps fingers, dust and moisture away from the core.'},
];

export const SNAP_RADIUS = .6;
export const easeInOutCubic = u => { u = Math.min(1, Math.max(0, u)); return u < .5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2; };
// Exploding peels front to back (order 0 first); assembling runs the reverse.
export const staggerDelay = (order, n, exploding) => Math.max(0, exploding ? order : n - 1 - order) * .045;
export const dist3 = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
export const shouldSnap = (pos, slot, r = SNAP_RADIUS) => dist3(pos, slot) < r;
// What a gesture means given the current state: 'explode' | 'assemble' | null.
export function gestureAction(exploded, gesture, dir) {
  if (gesture === 'fistsApart' || gesture === 'palmsApart') return exploded ? null : 'explode';
  if (gesture === 'clap') return exploded ? 'assemble' : null;
  if (gesture === 'twist') return dir < 0 ? (exploded ? null : 'explode') : dir > 0 ? (exploded ? 'assemble' : null) : null;
  if (gesture === 'key') return exploded ? 'assemble' : 'explode';
  return null;
}
// Where a label floats for a part (relative to the part's current position) and
// where its leader line starts (on the part's edge, toward the label).
export function labelLayout(p) {
  const [x, y, z] = p.lbl, len = Math.hypot(x, y) || 1;
  const anchor = p.kind === 'bolt' ? [0, 0, 0] : [x / len * p.rad * .92, y / len * p.rad * .92, 0];
  return {pos: [x, y, z], anchor};
}

// ---- the mode -----------------------------------------------------------------------------
export default function create(ctx) {
  const {THREE} = ctx;
  const HOLO = 0x6ff3ff, GOLD = 0xffd27a;
  const cGold = new THREE.Color(GOLD), cWhite = new THREE.Color(0xffffff);
  const group = new THREE.Group();
  const reactor = new THREE.Group();          // fist-move / flick / resize act on this
  const tilt = new THREE.Group();             // mode-owned presentation tilt
  const BASE_POS = [0, 1.2, 0], BASE_SCALE = 1.25;
  reactor.add(tilt); group.add(reactor);
  const noRay = o => { o.raycast = () => {}; return o; };
  const mkCanvas = (w, h) => { const c = document.createElement('canvas'); c.width = w; c.height = h; return c; };

  // shared textures
  const glowTex = (() => {
    const c = mkCanvas(128, 128), g = c.getContext('2d'), gr = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    gr.addColorStop(0, 'rgba(220,252,255,1)'); gr.addColorStop(.25, 'rgba(140,245,255,.5)'); gr.addColorStop(.6, 'rgba(111,243,255,.12)'); gr.addColorStop(1, 'rgba(111,243,255,0)');
    g.fillStyle = gr; g.fillRect(0, 0, 128, 128); return new THREE.CanvasTexture(c);
  })();
  function faceTex(draw, size = 512) {
    const c = mkCanvas(size, size), g = c.getContext('2d'); g.clearRect(0, 0, size, size); draw(g, size / 2);
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
  }
  function spaced(g, text, x, y, sp) {   // letter-spaced text, centred on x
    const w = [...text].reduce((s, ch) => s + g.measureText(ch).width + sp, -sp); let cx = x - w / 2;
    for (const ch of text) { g.fillText(ch, cx, y); cx += g.measureText(ch).width + sp; }
  }

  // material factories (each part gets its own instances so highlights stay local)
  const M = {
    glass: (o = {}) => new THREE.MeshPhysicalMaterial({color: 0x0d2c36, metalness: .35, roughness: .14, clearcoat: 1, clearcoatRoughness: .1,
      emissive: new THREE.Color(0x0b4656), emissiveIntensity: .7, transparent: true, opacity: .4, depthWrite: false, side: THREE.DoubleSide, ...o}),
    metal: (o = {}) => new THREE.MeshStandardMaterial({color: 0xa9c4cc, metalness: .9, roughness: .28, emissive: new THREE.Color(0x0e3540), emissiveIntensity: .8,
      transparent: true, opacity: .88, ...o}),
    copper: (o = {}) => new THREE.MeshStandardMaterial({color: 0xc4845a, metalness: .85, roughness: .32, emissive: new THREE.Color(0x2a1408), emissiveIntensity: .8,
      transparent: true, opacity: .92, ...o}),
    wire: (op = .12) => new THREE.MeshBasicMaterial({color: HOLO, wireframe: true, transparent: true, opacity: op, depthWrite: false}),
    glow: (op = .6, color = HOLO) => new THREE.MeshBasicMaterial({color, transparent: true, opacity: op, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide}),
    face: (map, op = .9) => new THREE.MeshBasicMaterial({map, transparent: true, opacity: op, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide}),
  };

  // ---- part builders ----------------------------------------------------------------------
  const zDisc = (r, h, seg) => new THREE.CylinderGeometry(r, r, h, seg).rotateX(Math.PI / 2);
  const add = (parent, geo, mat, pos, rotZ) => { const m = new THREE.Mesh(geo, mat); if (pos) m.position.set(pos[0], pos[1], pos[2]); if (rotZ) m.rotation.z = rotZ; parent.add(m); return m; };
  const halo = (parent, scale, op) => { const s = noRay(new THREE.Sprite(new THREE.SpriteMaterial({map: glowTex, color: HOLO, transparent: true, opacity: op, blending: THREE.AdditiveBlending, depthWrite: false}))); s.scale.setScalar(scale); parent.add(s); return s; };
  const spinners = [];   // {obj, axis, speed}
  const energyMats = [];
  let coreHalo = null, coreDisc = null;

  const BUILD = {
    back(g) {
      add(g, zDisc(1.32, .06, 72), M.glass({opacity: .34}));
      add(g, zDisc(1.32, .06, 24), M.wire(.07));
      const tex = faceTex((c, h) => {
        c.strokeStyle = 'rgba(111,243,255,.55)'; c.lineWidth = 2;
        [.38, .64, .9].forEach(f => { c.beginPath(); c.arc(h, h, h * f, 0, Math.PI * 2); c.stroke(); });
        c.lineWidth = 1.2; for (let i = 0; i < 60; i++) { const a = i / 60 * Math.PI * 2, r0 = h * (i % 5 ? .92 : .88); c.beginPath(); c.moveTo(h + Math.cos(a) * r0, h + Math.sin(a) * r0); c.lineTo(h + Math.cos(a) * h * .97, h + Math.sin(a) * h * .97); c.stroke(); }
        c.fillStyle = 'rgba(111,243,255,.7)'; c.font = '600 15px ui-monospace,Consolas,monospace'; spaced(c, 'XR-7 TOROIDAL CELL', h, h * 1.52, 5);
      });
      add(g, new THREE.CircleGeometry(1.3, 64), M.face(tex, .55), [0, 0, .035]);
    },
    fins(g) {
      const geo = new THREE.BoxGeometry(.34, .02, .13), mat = M.metal({opacity: .7});
      for (let i = 0; i < 24; i++) { const a = i / 24 * Math.PI * 2; add(g, geo, mat, [Math.cos(a) * 1.1, Math.sin(a) * 1.1, 0], a); }
      add(g, new THREE.TorusGeometry(.93, .012, 6, 72), M.glow(.35));
    },
    housing(g) {
      add(g, new THREE.TorusGeometry(1.18, .078, 14, 96), M.glass({opacity: .5, emissiveIntensity: .9}));
      add(g, new THREE.TorusGeometry(1.18, .084, 6, 48), M.wire(.1));
      add(g, new THREE.TorusGeometry(1.27, .006, 4, 120), M.glow(.55));
      const notch = new THREE.BoxGeometry(.07, .05, .07), nm = M.glow(.85);
      for (let i = 0; i < 12; i++) { const a = (i + .5) / 12 * Math.PI * 2; add(g, notch, nm, [Math.cos(a) * 1.275, Math.sin(a) * 1.275, 0], a); }
      const lug = zDisc(.11, .09, 18), lm = M.glass({opacity: .55});
      [45, 135, 225, 315].forEach(d => add(g, lug, lm, [Math.cos(D(d)) * BOLT_R, Math.sin(D(d)) * BOLT_R, .02]));
    },
    coils(g, p) {
      const spool = new THREE.BoxGeometry(.2, .17, .15), wind = new THREE.TorusGeometry(.078, .014, 6, 20).rotateY(Math.PI / 2),
        plate = new THREE.PlaneGeometry(.12, .06);
      const sm = M.glass({opacity: .45}), cm = M.copper(), pm = M.glow(.7);
      for (let k = 0; k < 5; k++) {
        const i = p.half * 5 + k, a = (i + .5) / 10 * Math.PI * 2, c = new THREE.Group();
        c.position.set(Math.cos(a) * .86, Math.sin(a) * .86, 0); c.rotation.z = a; g.add(c);
        add(c, spool, sm);
        [-.065, 0, .065].forEach(x => add(c, wind, cm, [x, 0, 0]));
        add(c, plate, pm, [0, 0, .08]);
      }
    },
    contain(g) {
      add(g, new THREE.TorusGeometry(.6, .03, 10, 80), M.metal({opacity: .8}));
      add(g, new THREE.TorusGeometry(.6, .05, 8, 80), M.glow(.22));
      const dash = new THREE.Group(); g.add(dash); spinners.push({obj: dash, axis: 'z', speed: .5});
      const dm = M.glow(.75);
      for (let i = 0; i < 8; i++) add(dash, new THREE.RingGeometry(.64, .652, 12, 1, i / 8 * Math.PI * 2, Math.PI / 10), dm);
    },
    spokes(g) {
      const geo = new THREE.BoxGeometry(.3, .036, .03), mat = M.metal(), node = new THREE.BoxGeometry(.05, .05, .05), nm = M.glow(.9);
      for (let i = 0; i < 6; i++) { const a = (i + .5) / 6 * Math.PI * 2; add(g, geo, mat, [Math.cos(a) * .44, Math.sin(a) * .44, 0], a); add(g, node, nm, [Math.cos(a) * .6, Math.sin(a) * .6, 0], a); }
    },
    hex(g) {
      const geo = zDisc(.44, .05, 6), plate = add(g, geo, M.glass({opacity: .5, emissiveIntensity: 1}));
      plate.rotation.z = Math.PI / 6;
      const e = noRay(new THREE.LineSegments(new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({color: HOLO, transparent: true, opacity: .8, depthWrite: false})));
      e.rotation.z = Math.PI / 6; g.add(e);
      add(g, new THREE.RingGeometry(.29, .31, 6), M.glow(.7), [0, 0, .03], Math.PI / 6);
      add(g, new THREE.RingGeometry(.15, .16, 3), M.glow(.6), [0, 0, .031], Math.PI / 2);
    },
    energy(g) {
      const u = {uCharge: {value: .8}, uTime: {value: 0}}; energyMats.push(u);
      const mat = new THREE.ShaderMaterial({uniforms: u, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
        vertexShader: 'varying vec3 n;varying vec3 v;varying float h;void main(){h=position.z;n=normalize(normalMatrix*normal);vec4 p=modelViewMatrix*vec4(position,1.);v=normalize(-p.xyz);gl_Position=projectionMatrix*p;}',
        fragmentShader: 'uniform float uCharge;uniform float uTime;varying vec3 n;varying vec3 v;varying float h;void main(){float d=abs(dot(n,v));float f=pow(1.-d,1.5);float c=pow(d,4.);float band=.75+.25*sin(h*60.-uTime*9.);vec3 col=mix(vec3(.25,.85,1.),vec3(1.),c*.8);gl_FragColor=vec4(col*(f*.7*band+c*.6)*uCharge,1.);}'});
      add(g, new THREE.CylinderGeometry(.16, .16, .3, 40, 6, false).rotateX(Math.PI / 2), mat);
      coreDisc = add(g, new THREE.CircleGeometry(.13, 32), M.glow(.85, 0xdffcff), [0, 0, .152]);
      [-.09, 0, .09].forEach(z => add(g, new THREE.TorusGeometry(.19, .006, 4, 48), M.glow(.6), [0, 0, z]));
      coreHalo = halo(g, 1.5, .8); coreHalo.position.z = .1;
    },
    cage(g) {
      const spin = new THREE.Group(); g.add(spin); spinners.push({obj: spin, axis: 'y', speed: .35});
      add(spin, new THREE.IcosahedronGeometry(.31, 1), M.wire(.42));
      const node = new THREE.OctahedronGeometry(.016, 0), nm = M.glow(.95), pos = new THREE.IcosahedronGeometry(.31, 1).getAttribute('position');
      const seen = new Set();
      for (let i = 0; i < pos.count; i++) {
        const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i), k = `${x.toFixed(3)},${y.toFixed(3)},${z.toFixed(3)}`;
        if (seen.has(k)) continue; seen.add(k); add(spin, node, nm, [x, y, z]);
      }
    },
    bolt(g) {
      add(g, zDisc(.068, .05, 6), M.metal(), [0, 0, .03]);
      add(g, zDisc(.026, .32, 10), M.metal({opacity: .7}), [0, 0, -.15]);
      add(g, new THREE.RingGeometry(.03, .04, 6), M.glow(.9), [0, 0, .057]);
    },
    bezel(g) {
      add(g, new THREE.TorusGeometry(.66, .032, 10, 72), M.metal());
      const tex = faceTex((c, h) => {
        c.strokeStyle = 'rgba(111,243,255,.9)';
        for (let i = 0; i < 72; i++) { const a = i / 72 * Math.PI * 2, r0 = h * (i % 6 ? .93 : .9); c.lineWidth = i % 6 ? 1.5 : 3; c.beginPath(); c.moveTo(h + Math.cos(a) * r0, h + Math.sin(a) * r0); c.lineTo(h + Math.cos(a) * h * .99, h + Math.sin(a) * h * .99); c.stroke(); }
      }, 256);
      add(g, new THREE.RingGeometry(.68, .76, 72, 1), M.face(tex, .8));
    },
    lens(g) {
      const geo = new THREE.SphereGeometry(.9, 48, 10, 0, Math.PI * 2, 0, .76).rotateX(Math.PI / 2).translate(0, 0, -.9 * Math.cos(.76));
      // the dome and its wireframe are see-through for picking, so the parts behind the lens
      // (cage, energy column, hex plate) stay pinchable; an invisible rim band picks the lens
      noRay(add(g, geo, M.glass({opacity: .13, emissiveIntensity: .35, color: 0x1a4652})));
      noRay(add(g, new THREE.SphereGeometry(.905, 24, 5, 0, Math.PI * 2, 0, .76).rotateX(Math.PI / 2).translate(0, 0, -.905 * Math.cos(.76)), M.wire(.08)));
      add(g, new THREE.TorusGeometry(.62, .008, 4, 72), M.glow(.6));
      const pick = add(g, new THREE.RingGeometry(.48, .72, 48, 1), new THREE.MeshBasicMaterial({transparent: true, opacity: 0, depthWrite: false, colorWrite: false, side: THREE.DoubleSide}));
      pick.userData.pickOnly = true;
    },
  };

  const parts = PARTS.map((def, idx) => {
    const obj = new THREE.Group(); obj.name = def.id; obj.userData.target = true; obj.userData.part = idx;
    try { (BUILD[def.kind] || (() => {}))(obj, def); } catch (e) { console.error('reactor part build failed', def.id, e); }
    tilt.add(obj);
    const mats = [];
    obj.traverse(o => {
      const ms = o.material ? (Array.isArray(o.material) ? o.material : [o.material]) : [];
      if (o.userData.pickOnly) return;
      ms.forEach(m => { if (!m || m.isShaderMaterial || mats.some(x => x.m === m)) return;
        const field = m.emissive ? 'emissive' : m.color ? 'color' : null; if (field) mats.push({m, field, base: m[field].clone()}); });
    });
    const q = e => new THREE.Quaternion().setFromEuler(new THREE.Euler(e[0], e[1], e[2]));
    return {def, idx, obj, mats,
      homeP: new THREE.Vector3(...def.home), homeQ: q(def.homeRot), expP: new THREE.Vector3(...def.exp), expQ: q(def.expRot),
      tw: null, state: 'home', hover: 0, flash: 0, pulse: 0, shown: -1};
  });

  // labels (canvas sprites + thin leader lines), never pickable
  const labelLayer = new THREE.Group(); tilt.add(labelLayer);
  parts.forEach(p => {
    try {
      const c = mkCanvas(512, 64), g = c.getContext('2d');
      g.font = '500 22px ui-monospace,Consolas,monospace'; g.fillStyle = '#ffffff'; spaced(g, p.def.name, 256, 40, 6);
      g.fillStyle = 'rgba(255,255,255,.5)'; g.fillRect(156, 52, 200, 1.5);
      const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
      const s = noRay(new THREE.Sprite(new THREE.SpriteMaterial({map: tex, color: HOLO, transparent: true, opacity: 0, depthWrite: false, depthTest: false})));
      s.scale.set(.88, .11, 1); s.renderOrder = 10; labelLayer.add(s);
      const lg = new THREE.BufferGeometry(); lg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
      const line = noRay(new THREE.Line(lg, new THREE.LineBasicMaterial({color: HOLO, transparent: true, opacity: 0, depthWrite: false, depthTest: false})));
      line.renderOrder = 9; labelLayer.add(line);
      p.label = {sprite: s, line, layout: labelLayout(p.def), op: 0};
    } catch (e) { p.label = null; }
  });

  // ---- state --------------------------------------------------------------------------------
  let exploded = false, explodeAmt = 0, hovered = null, charge = .8, flick = 1, flickT = 0;
  const drags = {};      // hand index -> {part, offset, goal}
  const TILT = {home: [D(6), D(-16)], exp: [D(14), D(-40)]};

  const partOf = obj => { let o = obj; while (o && o.userData && o.userData.part === undefined && o.parent) o = o.parent; return o && o.userData && o.userData.part !== undefined ? parts[o.userData.part] : null; };
  function tween(p, toP, toQ, delay, dur = .95) {
    p.tw = {fromP: p.obj.position.clone(), fromQ: p.obj.quaternion.clone(), toP: toP.clone(), toQ: toQ.clone(), delay, dur, el: 0};
  }
  function releaseAll() { Object.keys(drags).forEach(k => delete drags[k]); }
  function setExploded(on, quiet) {
    exploded = on; releaseAll();
    parts.forEach(p => {
      tween(p, on ? p.expP : p.homeP, on ? p.expQ : p.homeQ, staggerDelay(p.def.order, parts.length, on), on ? 1.05 : .9);
      p.state = on ? 'slot' : 'home';
    });
    if (!on) ctx.closeCard && ctx.closeCard();
    if (!quiet) ctx.toast(on ? 'XR-7 exploded · pinch-drag a part, hold a pinch for its specs' : 'XR-7 assembled · field sealed');
  }
  function run(action) { if (action === 'explode') setExploded(true); else if (action === 'assemble') setExploded(false); }
  function resetAll() {
    exploded = false; explodeAmt = 0; hovered = null; releaseAll();
    reactor.position.set(...BASE_POS); reactor.rotation.set(0, 0, 0); reactor.scale.setScalar(BASE_SCALE); delete reactor.userData._base;
    tilt.rotation.set(TILT.home[0], TILT.home[1], 0);
    parts.forEach(p => { p.tw = null; p.state = 'home'; p.hover = 0; p.flash = 0; p.pulse = 0; p.shown = -1; p.obj.position.copy(p.homeP); p.obj.quaternion.copy(p.homeQ); p.obj.scale.setScalar(1);
      p.mats.forEach(x => x.m[x.field].copy(x.base)); if (p.label) { p.label.op = 0; p.label.sprite.material.opacity = 0; p.label.line.material.opacity = 0; p.label.sprite.visible = p.label.line.visible = false; } });
  }
  const safe = fn => (...a) => { try { return fn(...a); } catch (e) { console.error('reactor', e); return undefined; } };
  const tmpV = new THREE.Vector3();

  function paint(p) {
    const h = p.hover, f = Math.min(1, p.flash), key = Math.round(h * 50) * 100 + Math.round(f * 50);
    if (key === p.shown) return; p.shown = key;
    p.mats.forEach(x => { const c = x.m[x.field]; c.copy(x.base); if (h > 0) c.lerp(cGold, h * (x.field === 'emissive' ? .9 : .85)); if (f > 0) c.lerp(cWhite, f * .85); });
  }

  resetAll();

  return {
    id: 'reactor', label: 'ARC REACTOR', group,
    hint: 'Two fists apart or twist a fist counter-clockwise to explode · clap or twist clockwise to assemble · pinch-drag a part · hold a pinch for specs · E toggles',
    enter: safe(() => { resetAll(); }),
    exit: safe(() => { resetAll(); ctx.closeCard && ctx.closeCard(); }),
    targets: () => parts.map(p => p.obj),
    hover: safe(obj => { const p = obj ? partOf(obj) : null; hovered = p; }),
    press: safe(obj => {
      const p = partOf(obj); if (!p) return;
      if (!exploded) { ctx.toast('Explode it first: two fists apart, or twist a fist counter-clockwise'); return; }
      p.pulse = 1; p.flash = 1; ctx.toast(p.def.name);
    }),
    hold: safe(obj => {
      const p = partOf(obj); if (!p) return;
      if (!exploded) {
        ctx.card({kicker: 'XR-7 · TOROIDAL CELL', title: 'Assembled reactor', rows: [['Parts', String(parts.length)], ['Output', '3.2 GW (nominal)'], ['Field', '3.4 T toroidal'], ['State', 'Sealed']],
          body: 'Pull two fists apart, or twist a fist counter-clockwise, to see every part. Clap to put it back together.'});
        return;
      }
      p.flash = 1;
      ctx.card({kicker: `XR-7 · PART ${String(p.idx + 1).padStart(2, '0')} / ${parts.length}`, title: p.def.name, rows: p.def.spec, body: p.def.body});
    }),
    grab: safe((obj, hand) => {
      if (!exploded) return false;
      const p = partOf(obj); if (!p) return false;
      const k = hand && hand.i != null ? hand.i : 0;
      Object.keys(drags).forEach(h => { if (drags[h].part === p) delete drags[h]; });
      drags[k] = {part: p, offset: null, goal: p.obj.position.clone()};
      p.tw = null; p.state = 'drag'; p.flash = .6; return true;
    }) ,
    drag: safe((hand, worldPoint) => {
      const d = drags[hand && hand.i != null ? hand.i : 0]; if (!d || !worldPoint) return;
      const local = tilt.worldToLocal(tmpV.copy(worldPoint));
      if (!d.offset) d.offset = d.part.obj.position.clone().sub(local);
      d.goal.copy(local).add(d.offset);
    }),
    drop: safe(hand => {
      const k = hand && hand.i != null ? hand.i : 0, d = drags[k]; if (!d) return; delete drags[k];
      const p = d.part, pos = p.obj.position;
      if (shouldSnap([pos.x, pos.y, pos.z], [p.expP.x, p.expP.y, p.expP.z])) { tween(p, p.expP, p.expQ, 0, .5); p.state = 'slot'; p.pulse = .6; }
      else { p.state = 'free'; ctx.toast(p.def.name + ' parked · drop it near its slot to snap back'); }
    }),
    manipulable: () => reactor,
    fistsApart: safe(() => run(gestureAction(exploded, 'fistsApart'))),
    palmsApart: safe(() => run(gestureAction(exploded, 'palmsApart'))),
    clap: safe(() => run(gestureAction(exploded, 'clap'))),
    twist: safe(dir => run(gestureAction(exploded, 'twist', dir))),
    key: k => { if (k !== 'e') return false; try { run(gestureAction(exploded, 'key')); } catch (e) { console.error(e); } return true; },
    menu: () => [
      {label: exploded ? 'ASSEMBLE' : 'EXPLODE', run: safe(() => setExploded(!exploded))},
      {label: 'RESET', run: safe(() => { resetAll(); ctx.closeCard && ctx.closeCard(); ctx.toast('XR-7 reset'); })},
    ],
    update: safe((dt, t) => {
      // readiness cues, like the original: what the hands are about to do
      try {
        const hs = ctx.hands() || [], fists = hs.filter(h => h.fist), palms = hs.filter(h => h.palm);
        let cue = null;
        if (!exploded && fists.length === 2) cue = {title: 'EXPLODE READY', lines: ['PULL HANDS APART', 'TO EXPLODE']};
        else if (exploded && palms.length === 2) cue = {title: 'ASSEMBLE READY', lines: ['CLAP HANDS', 'TO ASSEMBLE']};
        else if (fists.length === 1 && hs.length === 1) cue = {title: 'TWIST', lines: ['CCW: EXPLODE', 'CW: ASSEMBLE']};
        if (ctx.cue) ctx.cue(cue);
      } catch (e) {}
      const k6 = 1 - Math.exp(-dt * 6);
      explodeAmt += ((exploded ? 1 : 0) - explodeAmt) * (1 - Math.exp(-dt * 3));
      const tx = TILT.home[0] + (TILT.exp[0] - TILT.home[0]) * explodeAmt, ty = TILT.home[1] + (TILT.exp[1] - TILT.home[1]) * explodeAmt;
      tilt.rotation.x += (tx + Math.sin(t * .4) * .02 - tilt.rotation.x) * k6; tilt.rotation.y += (ty + Math.sin(t * .27) * .03 - tilt.rotation.y) * k6;
      tilt.position.y = Math.sin(t * .8) * .035;

      const dragged = new Set(Object.values(drags).map(d => d.part));
      const kDrag = 1 - Math.exp(-dt * 14);
      parts.forEach(p => {
        const d = dragged.has(p) ? Object.values(drags).find(x => x.part === p) : null;
        if (d) { p.obj.position.lerp(d.goal, kDrag); p.obj.quaternion.slerp(p.expQ, k6 * .3); }
        else if (p.tw) {
          const w = p.tw; w.el += dt; const u = (w.el - w.delay) / w.dur;
          if (u > 0) { const e = easeInOutCubic(u); p.obj.position.lerpVectors(w.fromP, w.toP, e); p.obj.quaternion.slerpQuaternions(w.fromQ, w.toQ, e); }
          if (u >= 1) p.tw = null;
        }
        const want = hovered === p || d ? 1 : 0;
        p.hover += (want - p.hover) * (1 - Math.exp(-dt * 12)); if (Math.abs(p.hover - want) < .01) p.hover = want;
        p.flash = Math.max(0, p.flash - dt * 3.2);
        p.pulse = Math.max(0, p.pulse - dt * 2.4);
        p.obj.scale.setScalar(1 + Math.sin(p.pulse * Math.PI) * .12 + (d ? .04 : 0));
        paint(p);
      });

      // energy: a calm breath when sealed; a nervous flicker when the field is open
      flickT -= dt;
      if (explodeAmt > .3 && flickT <= 0) { flick = .35 + Math.random() * .55; flickT = .04 + Math.random() * .22; }
      if (explodeAmt <= .3 || flickT < .02) flick += (1 - flick) * Math.min(1, dt * 18);
      const breath = .78 + .14 * Math.sin(t * 2.1) + .05 * Math.sin(t * 5.3);
      const jitter = explodeAmt * (.12 * Math.sin(t * 37) + .08 * Math.sin(t * 61));
      charge += (breath * (1 - explodeAmt * (1 - flick)) + jitter - charge) * Math.min(1, dt * 10);
      energyMats.forEach(u => { u.uCharge.value = Math.max(.05, charge); u.uTime.value = t; });
      if (coreHalo) { coreHalo.scale.setScalar(1.15 + charge * .7); coreHalo.material.opacity = .25 + charge * .55; }
      if (coreDisc) coreDisc.material.opacity = .45 + charge * .45;
      spinners.forEach(s => { s.obj.rotation[s.axis] += dt * s.speed * (1 + explodeAmt * 1.5); });

      // labels
      const show = !!(ctx.labels && ctx.labels()) && exploded;
      parts.forEach(p => {
        const L = p.label; if (!L) return;
        L.op += ((show ? explodeAmt : 0) - L.op) * (1 - Math.exp(-dt * 6));
        const vis = L.op > .02; L.sprite.visible = L.line.visible = vis; if (!vis) return;
        const o = p.obj.position, lp = L.layout.pos, an = L.layout.anchor;
        L.sprite.position.set(o.x + lp[0], o.y + lp[1], o.z + lp[2]);
        const a = L.line.geometry.getAttribute('position');
        const ex = lp[0], ey = lp[1] - Math.sign(lp[1] || 1) * .07;
        a.setXYZ(0, o.x + an[0], o.y + an[1], o.z + an[2]); a.setXYZ(1, o.x + ex, o.y + ey, o.z + lp[2]); a.needsUpdate = true;
        const hot = p.hover > .5;
        L.sprite.material.opacity = L.op * (hot ? 1 : .85); L.line.material.opacity = L.op * .45;
        L.sprite.material.color.set(hot ? GOLD : HOLO); L.line.material.color.set(hot ? GOLD : HOLO);
      });
    }),
  };
}
