// Xenon Holo mode: XENON. The Xenon core floats over a ring of panels built
// from Xenon's live data: Now Playing, My Taste, System, Modes, Talk.
// Pinch a button to press it, fist-drag on the ring to spin it (the stage's
// generic fist-move acts on the ring), hold an open palm and Xenon listens.

export default function create(ctx) {
  const {THREE} = ctx;
  const group = new THREE.Group();
  const W = 560, H = 340, RADIUS = 3.1;

  // ---- the core -----------------------------------------------------------------------
  const core = new THREE.Group(); core.position.set(0, 1.85, 0); group.add(core);
  const energyU = {uCharge: {value: .3}, uTime: {value: 0}};
  const energy = new THREE.Mesh(new THREE.SphereGeometry(.3, 40, 28), new THREE.ShaderMaterial({uniforms: energyU, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: 'varying vec3 n;varying vec3 v;void main(){n=normalize(normalMatrix*normal);vec4 p=modelViewMatrix*vec4(position,1.);v=normalize(-p.xyz);gl_Position=projectionMatrix*p;}',
    fragmentShader: 'uniform float uCharge;uniform float uTime;varying vec3 n;varying vec3 v;void main(){float f=pow(1.-max(dot(n,v),0.),1.6);float c=pow(max(dot(n,v),0.),3.);vec3 col=mix(vec3(.25,.85,1.),vec3(1.),c*.7);gl_FragColor=vec4(col*(f*.55+c*.7)*uCharge*(.94+.06*sin(uTime*11.)),1.);}'}));
  core.add(energy);
  const haloC = document.createElement('canvas'); haloC.width = haloC.height = 128;
  { const g = haloC.getContext('2d'), gr = g.createRadialGradient(64, 64, 0, 64, 64, 64); gr.addColorStop(0, 'rgba(160,245,255,.9)'); gr.addColorStop(.35, 'rgba(111,243,255,.25)'); gr.addColorStop(1, 'rgba(111,243,255,0)'); g.fillStyle = gr; g.fillRect(0, 0, 128, 128); }
  const halo = new THREE.Sprite(new THREE.SpriteMaterial({map: new THREE.CanvasTexture(haloC), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending}));
  halo.scale.setScalar(1.8); core.add(halo);
  const shellMats = [];
  ctx.loadModel('/models/core.glb').then(g => {
    const m = g.scene.clone(true); const box = new THREE.Box3().setFromObject(m), s = box.getSize(new THREE.Vector3()), c = box.getCenter(new THREE.Vector3());
    const k = 1.7 / Math.max(s.x, s.y, s.z); m.scale.setScalar(k); m.position.set(-c.x * k, -c.y * k + .05, -c.z * k);
    m.traverse(o => { if (!o.isMesh) return; const mat = new THREE.MeshPhysicalMaterial({map: o.material.map || null, normalMap: o.material.normalMap || null, roughness: .18, metalness: .6, clearcoat: .6,
      emissive: new THREE.Color(0x2fb8ff), emissiveIntensity: .05, transparent: true, opacity: .36, depthWrite: false, side: THREE.DoubleSide}); o.material = mat; shellMats.push(mat);
      o.add(new THREE.Mesh(o.geometry, new THREE.MeshBasicMaterial({color: 0x6ff3ff, wireframe: true, transparent: true, opacity: .05, depthWrite: false}))); });
    core.add(m);
  }).catch(() => {});

  // ---- panels ---------------------------------------------------------------------------
  const ring = new THREE.Group(); group.add(ring);
  const panels = [];
  let st = {};
  function panel(id, title, draw) {
    const c = document.createElement('canvas'); c.width = W; c.height = H; const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(2.1, 2.1 * H / W), new THREE.MeshBasicMaterial({map: tex, transparent: true, depthWrite: false, side: THREE.DoubleSide}));
    const p = {id, title, draw, c, g: c.getContext('2d'), tex, mesh, buttons: [], hover: -1, flash: -1};
    mesh.userData.target = true; mesh.userData.p = p; ring.add(mesh); panels.push(p); return p;
  }
  function frame(p) {
    const g = p.g; g.clearRect(0, 0, W, H); g.fillStyle = 'rgba(6,18,26,.62)'; g.fillRect(0, 0, W, H);
    g.strokeStyle = 'rgba(111,243,255,.55)'; g.lineWidth = 2; g.strokeRect(3, 3, W - 6, H - 6);
    g.fillStyle = '#6ff3ff'; [[0, 0], [W - 26, 0], [0, H - 4], [W - 26, H - 4]].forEach(([x, y]) => g.fillRect(x + 3, y + 3, 22, 2));
    g.font = '600 20px ui-monospace,Consolas,monospace'; g.fillStyle = '#6ff3ff'; g.fillText(p.title, 24, 40);
    g.fillStyle = 'rgba(111,243,255,.25)'; g.fillRect(24, 52, W - 48, 1);
  }
  function btn(p, i, x, y, w, h, label, cmd, lit) {
    const g = p.g; p.buttons.push({x, y, w, h, cmd});
    const hot = p.hover === i, fl = p.flash === i;
    g.fillStyle = fl ? 'rgba(255,255,255,.55)' : lit ? 'rgba(111,243,255,.30)' : hot ? 'rgba(255,210,122,.20)' : 'rgba(111,243,255,.06)'; g.fillRect(x, y, w, h);
    g.strokeStyle = hot ? '#ffd27a' : lit ? '#6ff3ff' : 'rgba(111,243,255,.4)'; g.lineWidth = hot ? 2.5 : 1.2; g.strokeRect(x, y, w, h);
    g.font = '500 19px ui-monospace,Consolas,monospace'; g.fillStyle = '#e9f3f6'; g.textAlign = 'center'; g.fillText(label, x + w / 2, y + h / 2 + 7); g.textAlign = 'left';
  }
  function text(p, t, x, y, size, color) {
    const g = p.g; g.font = `${size || 22}px system-ui,Segoe UI,sans-serif`; g.fillStyle = color || '#e9f3f6';
    let s = String(t || ''); while (g.measureText(s).width > W - x - 24 && s.length > 3) s = s.slice(0, -2); if (s !== String(t || '')) s += '…'; g.fillText(s, x, y);
  }
  function bar(p, label, v, y) {
    const g = p.g; text(p, `${label}  ${Math.round(v)}%`, 24, y, 20, '#9fb4bc'); g.fillStyle = 'rgba(111,243,255,.12)'; g.fillRect(24, y + 12, W - 48, 14);
    g.fillStyle = v > 85 ? '#ff8f8f' : '#6ff3ff'; g.fillRect(24, y + 12, (W - 48) * Math.min(1, v / 100), 14);
  }
  panel('np', '01 · NOW PLAYING', p => { text(p, st.track || 'Nothing playing', 24, 100, 28); text(p, st.artist || '', 24, 134, 20, '#9fb4bc');
    btn(p, 0, 24, 190, 160, 70, '◀◀', 'media previous'); btn(p, 1, 200, 190, 160, 70, '▶ ❚❚', 'media play pause', st.playing); btn(p, 2, 376, 190, 160, 70, '▶▶', 'media next'); });
  panel('taste', '02 · MY TASTE', p => { const top = (st.top || []).slice(0, 3); if (!top.length) text(p, 'Still learning what you like', 24, 110, 20, '#9fb4bc');
    top.forEach((a, i) => btn(p, i, 24, 72 + i * 62, W - 48, 52, `${i + 1}  ${a}`, 'play ' + a)); btn(p, 3, 24, 262, W - 48, 52, 'PLAY MY USUAL', 'play my usual'); });
  panel('sys', '03 · SYSTEM', p => { bar(p, 'CPU', st.cpu || 0, 100); bar(p, 'RAM', st.ram || 0, 160); text(p, `Laptop ${st.time || '--:--'}`, 24, 240, 20, '#9fb4bc');
    text(p, st.xtreme ? 'Xtreme mode on' : st.game ? 'Game mode on' : 'Normal mode', 24, 276, 20, '#6ff3ff'); });
  panel('modes', '04 · MODES', p => { btn(p, 0, 24, 84, W - 48, 60, 'GAME MODE', st.game ? 'game mode off' : 'game mode on', st.game && !st.xtreme);
    btn(p, 1, 24, 158, W - 48, 60, 'XTREME', st.xtreme ? 'xtreme mode off' : 'go xtreme', st.xtreme);
    btn(p, 2, 24, 232, W - 48, 60, 'MUSIC WIDGET', st.widget ? 'turn off the music widget' : 'turn on the music widget', st.widget); });
  panel('talk', '05 · TALK', p => { text(p, 'Hold an open palm, or press:', 24, 100, 20, '#9fb4bc'); btn(p, 0, 24, 130, W - 48, 80, '🎙  TALK TO XENON', 'listen now');
    btn(p, 1, 24, 226, W - 48, 60, "WHAT'S PLAYING?", "what's playing"); });
  const redraw = p => { p.buttons = []; frame(p); p.draw(p); p.tex.needsUpdate = true; };
  function place() {
    panels.forEach((p, i) => {
      const a = i / panels.length * Math.PI * 2; p.mesh.position.set(Math.sin(a) * RADIUS, .3, Math.cos(a) * RADIUS);
      p.mesh.rotation.set(0, a, 0);
    });
  }
  place();
  function buttonAt(hit) { if (!hit || !hit.uv) return -1; const p = hit.obj.userData.p, x = hit.uv.x * W, y = (1 - hit.uv.y) * H; return p.buttons.findIndex(b => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h); }
  const _wp = new THREE.Vector3();
  const facing = p => p.mesh.getWorldPosition(_wp).z > .6;

  let palmAt = 0, lastListen = 0, charge = .3, spin = 0, lastPoll = 0;
  return {
    id: 'xenon', label: 'XENON', group, hoverEvery: true,
    hint: 'Pinch a button · fist on a panel and drag to spin the ring · hold an open palm to talk · clap for what\'s playing',
    enter() { panels.forEach(redraw); },
    targets: () => panels.filter(facing).map(p => p.mesh),
    hover(obj, hit) {
      panels.forEach(p => { const want = obj && obj.userData.p === p ? buttonAt(hit) : -1; if (p.hover !== want) { p.hover = want; redraw(p); } });
    },
    async press(obj, hand, hit) {
      const p = obj.userData.p, i = buttonAt(hit); if (i < 0) return;
      p.flash = i; redraw(p); setTimeout(() => { p.flash = -1; redraw(p); }, 220);
      ctx.toast(await ctx.xenon(p.buttons[i].cmd));
    },
    manipulable: () => ring,               // fist-move / flick / resize act on the ring
    async clap() { ctx.toast(await ctx.xenon("what's playing")); },
    palmFlip() {},
    update(dt, t) {
      const s0 = ctx.status();
      if (s0 !== st) { st = s0; if (t - lastPoll > 1.4) { lastPoll = t; panels.forEach(redraw); } }
      // keep the ring level (fist-move rotates it about z; only allow y spin)
      // a fist dragged sideways spins the ring round the core (instead of sliding it)
      ring.rotation.y += ring.position.x * .55; ring.position.x = 0; ring.position.z += (0 - ring.position.z) * .1;
      ring.rotation.x *= .9; ring.rotation.z = 0; ring.position.y += (0 - ring.position.y) * .1;
      // every panel turns to face you; the ones behind the core fade
      panels.forEach(p => { p.mesh.getWorldPosition(_wp); p.mesh.lookAt(ctx.camera.position.x, _wp.y, ctx.camera.position.z);
        p.mesh.material.opacity = THREE.MathUtils.clamp(.15 + (_wp.z + RADIUS) / (2 * RADIUS) * .95, .12, 1); });
      const orb = st.orb || 'idle', want = {listening: .95, heard: 1.1, thinking: .75, searching: .75, speaking: .8, working: .7, confirm: .9}[orb] ?? (st.playing ? .4 : .28);
      const speak = orb === 'speaking' ? .25 * (.5 + .5 * Math.sin(t * 9.5)) : 0;
      charge += (want + (st.energy || 0) * .8 + speak - charge) * Math.min(1, dt * 4);
      spin += dt * ({thinking: 3, searching: 3, listening: 1.3, heard: 1.6}[orb] ?? .35);
      energyU.uCharge.value = charge; energyU.uTime.value = t; halo.scale.setScalar(1.3 + charge * 1.4); halo.material.opacity = .35 + charge * .5;
      shellMats.forEach(m => m.emissiveIntensity = .03 + charge * .12); core.rotation.y = spin * .15; core.position.y = 1.85 + Math.sin(t * .8) * .04;
      // open palm held (no target) -> Xenon listens
      const hs = ctx.hands ? ctx.hands() : [];
      const h = hs[0];
      if (h && h.palm && !h.palmUp) { if (!palmAt) palmAt = t; if (t - palmAt > .9 && t - lastListen > 5) { lastListen = t; palmAt = 0; ctx.xenon('listen now').then(ctx.toast); } } else palmAt = 0;
    },
  };
}
