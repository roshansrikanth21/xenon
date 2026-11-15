// Xenon Holo - the stage: scene, camera feed, hands, menu, cards, tray,
// settings, keyboard, and the modes that plug into it.
//
// ---- The mode contract -------------------------------------------------------------
// A mode file default-exports create(ctx) and returns an object with:
//   id, label, hint                    menu label and one-line HUD hint
//   group: THREE.Group                 added to the scene while the mode runs
//   enter(), exit(), update(dt, t)
//   targets(): Object3D[]              what a fingertip can point at / pinch
//   hover(obj | null, hit)             highlight what is pointed at (hit = {obj, point, uv})
//   hoverEvery: true                   call hover every frame while on a target (for button-level hover)
//   press(obj, hand, hit)              quick pinch on a target
//   grab(obj, hand) -> bool            pinch-drag starts on a target (optional)
//   drag(hand, worldPoint)             ...moves (worldPoint is on the object's depth plane)
//   drop(hand)                         ...ends
//   hold(obj, hand, hit)               pinch held still 0.8 s on a target
//   manipulable(obj) -> Object3D       what fist-move, flick-spin and resize act on
//   clap(), twist(dir), fistsApart(), palmsApart(), palmFlip(up)
//   key(k) -> bool                     mode keys (E explode etc.)
//   menu(): [{label, run}]             extra menu buttons while active
//   chip(i)                            a tray chip was pinched (table mode)
// Every member is optional except id, label, group.
//
// ctx = {THREE, scene, camera, toast, card, closeCard, xenon, status, loadModel,
//        planePoint(sx, sy, z), labels(), setTray(chips, onMix), colors, debug}

import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {MeshoptDecoder} from 'three/addons/libs/meshopt_decoder.module.js';
import {Hands} from './hands.js';

const $ = id => document.getElementById(id);
const key = (location.hash.match(/k=([^&]+)/) || [])[1] || (() => { try { return sessionStorage.getItem('xk') || ''; } catch (e) { return ''; } })();
if (location.hash) { try { sessionStorage.setItem('xk', key); } catch (e) {} window.history.replaceState(null, '', location.pathname + location.search); }
const params = new URLSearchParams(location.search);

// ---- settings ----------------------------------------------------------------------
const QUALITY = {
  high: {ratio: 2, delegate: 'GPU', cam: {width: 1920, height: 1080}},
  medium: {ratio: 1.5, delegate: 'GPU', cam: {width: 1280, height: 720}},
  low: {ratio: 1, delegate: 'GPU', cam: {width: 960, height: 540}},
  safe: {ratio: 0.8, delegate: 'CPU', cam: {width: 640, height: 360}},
};
const S = {
  quality: params.get('quality') || 'auto', mirror: true, depthInvert: false, bw: true, dim: false,
  labels: true, skeleton: true, debug: false, camIndex: 0,
};
function pickQuality() {
  if (S.quality !== 'auto') return QUALITY[S.quality] || QUALITY.medium;
  const cores = navigator.hardwareConcurrency || 4;
  return cores >= 12 ? QUALITY.high : cores >= 6 ? QUALITY.medium : QUALITY.low;
}

// ---- renderer + scene (transparent over the camera) -------------------------------------
const canvas = $('gl');
const renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: true});
renderer.setClearColor(0x000000, 0);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 0.9;
function applyRatio() { renderer.setPixelRatio(Math.min(devicePixelRatio, pickQuality().ratio)); renderer.setSize(innerWidth, innerHeight); }
applyRatio();
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(40, innerWidth / innerHeight, 0.01, 100);
camera.position.set(0, 1.4, 8.4); camera.lookAt(0, 1.2, 0);
scene.add(new THREE.HemisphereLight(0x8fe8ff, 0x050608, 0.6));
const sun = new THREE.DirectionalLight(0xffffff, 0.9); sun.position.set(3, 6, 5); scene.add(sun);
const rim = new THREE.PointLight(0x6ff3ff, 2.5, 14, 1.6); rim.position.set(-3, 2.5, 3); scene.add(rim);

const gltf = new GLTFLoader(); gltf.setMeshoptDecoder(MeshoptDecoder);
const modelCache = {};
const loadModel = url => (modelCache[url] ||= gltf.loadAsync(url));

// ---- Xenon link -----------------------------------------------------------------------
let status = {};
async function xenon(text) {
  try {
    const r = await fetch('/api/cmd', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Xenon-Key': key}, body: JSON.stringify({text})});
    const reply = r.ok ? (await r.json()).reply : 'Xenon did not answer.';
    setTimeout(poll, 600);
    return reply;
  } catch (e) { return 'Xenon is not reachable.'; }
}
async function poll() {
  try { const r = await fetch('/api/status', {headers: {'X-Xenon-Key': key}}); if (r.ok) status = await r.json(); } catch (e) {}
}
poll(); setInterval(poll, 1500);

// ---- HUD pieces: toast, card, tray, menu ----------------------------------------------------
let toastT = 0;
function toast(text) { const el = $('toast'); el.textContent = text; el.classList.add('on'); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove('on'), 3200); }
function card({title, kicker = '', rows = [], body = ''}) {
  const el = $('card');
  el.innerHTML = `<div class="ck">${kicker}</div><div class="ct">${title}</div>` +
    rows.map(([k, v]) => `<div class="cr"><span>${k}</span><b>${v}</b></div>`).join('') +
    (body ? `<p>${body}</p>` : '') + `<button class="cx" data-act="close-card">CLOSE</button>`;
  el.classList.add('on');
}
function closeCard() { $('card').classList.remove('on'); }
let trayMix = null;
function setTray(chips, onMix) {
  trayMix = onMix || null;
  const el = $('tray');
  if (!chips) { el.classList.remove('on'); el.innerHTML = ''; return; }
  el.innerHTML = chips.map((c, i) => `<button class="chip" data-act="chip" data-i="${i}">${c}</button>`).join('') +
    `<button class="chip mix" data-act="mix">MIX${chips.length ? ' · ' + chips.length : ''}</button>`;
  el.classList.add('on');
}

const MODES = {};
let mode = null;
const ctx = {THREE, scene, camera, toast, card, closeCard, xenon, status: () => status, loadModel, setTray, hands: () => hands.hands,
  labels: () => S.labels, colors: {holo: 0x6ff3ff, gold: 0xffd27a, warn: 0xff7b8a}, debug: () => S.debug,
  planePoint(sx, sy, z = 0) {
    const ndc = new THREE.Vector2(sx / innerWidth * 2 - 1, -(sy / innerHeight * 2 - 1));
    const ray = new THREE.Raycaster(); ray.setFromCamera(ndc, camera);
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), -z);
    const out = new THREE.Vector3(); return ray.ray.intersectPlane(plane, out) || out;
  },
};

async function registerModes() {
  const files = ['xenon', 'reactor', 'table', 'shards'];
  for (const f of files) {
    try { const m = (await import(`/holo/js/${f}.js`)).default(ctx); MODES[m.id] = m; }
    catch (e) { console.error('mode failed to load', f, e); }
  }
}

function setMode(id) {
  const next = MODES[id]; if (!next || next === mode) { closeMenu(); return; }
  if (mode) { try { mode.exit && mode.exit(); } catch (e) { console.error(e); } scene.remove(mode.group); }
  closeCard(); setTray(null);
  mode = next; scene.add(mode.group); mode.enter && mode.enter();
  $('modeName').textContent = mode.label; $('hint').textContent = mode.hint || '';
  manip = null; spinVel.set(0, 0); closeMenu();
  try { localStorage.setItem('xholo-mode', id); } catch (e) {}
}

// Menu: a ring of buttons around where it was opened.
let menuOpen = false;
function openMenu(at) {
  const el = $('menu');
  const items = [
    ...Object.values(MODES).map(m => ({label: m.label, run: () => setMode(m.id), on: m === mode})),
    ...((mode && mode.menu && mode.menu()) || []),
  ];
  const cx = Math.min(innerWidth - 170, Math.max(170, at ? at.x : innerWidth / 2));
  const cy = Math.min(innerHeight - 170, Math.max(170, at ? at.y : innerHeight / 2));
  el.innerHTML = '';
  items.forEach((it, i) => {
    const a = -Math.PI / 2 + i / items.length * Math.PI * 2, r = 125;
    const b = document.createElement('button'); b.className = 'mi' + (it.on ? ' on' : ''); b.textContent = it.label;
    b.dataset.act = 'menu'; b.dataset.i = i; b.style.left = (cx + Math.cos(a) * r) + 'px'; b.style.top = (cy + Math.sin(a) * r) + 'px';
    el.appendChild(b);
  });
  const core = document.createElement('div'); core.className = 'mcore'; core.style.left = cx + 'px'; core.style.top = cy + 'px'; core.textContent = 'XENON';
  el.appendChild(core);
  el._items = items; el.classList.add('on'); menuOpen = true;
}
function closeMenu() { $('menu').classList.remove('on'); menuOpen = false; }

// Anything on the HUD with data-act can be "pinched" (or clicked).
function act(el) {
  const a = el.dataset.act;
  if (a === 'menu') { const it = $('menu')._items[+el.dataset.i]; it && it.run(); if (menuOpen && a === 'menu') closeMenu(); return true; }
  if (a === 'close-card') { closeCard(); return true; }
  if (a === 'chip') { mode && mode.chip && mode.chip(+el.dataset.i); return true; }
  if (a === 'mix') { trayMix && trayMix(); return true; }
  if (a === 'setting') { setting(el.dataset.s); return true; }
  if (a === 'guide') { $('guide').classList.toggle('on'); return true; }
  return false;
}
document.addEventListener('click', e => { const el = e.target.closest('[data-act]'); if (el) act(el); });
addEventListener('dblclick', e => { if (!e.target.closest('[data-act]')) openMenu({x: e.clientX, y: e.clientY}); });
function hudAt(x, y) { const el = document.elementFromPoint(x, y); return el && el.closest('[data-act]'); }

// ---- settings + keyboard -------------------------------------------------------------------
function setting(s) {
  if (s === 'mirror') { S.mirror = !S.mirror; hands.mirror = S.mirror; }
  if (s === 'invert') S.depthInvert = !S.depthInvert;
  if (s === 'bw') S.bw = !S.bw;
  if (s === 'dim') S.dim = !S.dim;
  if (s === 'labels') S.labels = !S.labels;
  if (s === 'skeleton') S.skeleton = !S.skeleton;
  if (s === 'debug') S.debug = !S.debug;
  if (s === 'camera') nextCamera();
  if (s === 'quality') { const order = ['auto', 'high', 'medium', 'low', 'safe']; S.quality = order[(order.indexOf(S.quality) + 1) % order.length]; applyRatio(); restartTracking(); }
  paintSettings();
}
function paintSettings() {
  const v = $('cam'); v.style.transform = S.mirror ? 'scaleX(-1)' : 'none';
  v.style.filter = (S.bw ? 'grayscale(1) contrast(1.15) sepia(.35) hue-rotate(150deg) ' : '') + `brightness(${S.dim ? 0.18 : 0.42})`;
  const label = {mirror: 'MIRROR', invert: 'DEPTH INVERT', bw: 'B&W', dim: 'DIM', labels: 'LABELS', skeleton: 'HANDS', debug: 'DEBUG'};
  document.querySelectorAll('[data-s]').forEach(b => {
    const s = b.dataset.s;
    if (s === 'quality') b.textContent = 'QUALITY · ' + S.quality.toUpperCase();
    else if (s === 'camera') b.textContent = 'CAMERA ' + (S.camIndex + 1);
    else { const on = {mirror: S.mirror, invert: S.depthInvert, bw: S.bw, dim: S.dim, labels: S.labels, skeleton: S.skeleton, debug: S.debug}[s]; b.textContent = label[s]; b.classList.toggle('on', !!on); }
  });
  $('debug').style.display = S.debug ? 'block' : 'none';
}
addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (k === 'escape') { if (menuOpen) closeMenu(); else if ($('guide').classList.contains('on')) $('guide').classList.remove('on'); else closeCard(); return; }
  if (mode && mode.key && mode.key(k)) return;
  ({m: () => menuOpen ? closeMenu() : openMenu(), f: () => setting('mirror'), i: () => setting('invert'), b: () => setting('bw'),
    c: () => setting('camera'), l: () => setting('labels'), o: () => setting('skeleton'), d: () => setting('debug'),
    h: () => $('guide').classList.toggle('on'), q: () => setting('quality')}[k] || (() => {}))();
});

// ---- camera + tracking ----------------------------------------------------------------------
const video = $('cam');
const hands = new Hands();
let tracker = null, cams = [];
async function startCamera() {
  const q = pickQuality();
  if (video.srcObject) video.srcObject.getTracks().forEach(t => t.stop());
  const device = cams[S.camIndex];
  video.srcObject = await navigator.mediaDevices.getUserMedia({video: {...q.cam, ...(device ? {deviceId: {exact: device.deviceId}} : {facingMode: 'user'})}, audio: false});
  await video.play();
  cams = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput');
}
async function nextCamera() { if (cams.length < 2) { toast('Only one camera found.'); return; } S.camIndex = (S.camIndex + 1) % cams.length; await startCamera(); paintSettings(); }
let visionLib = null;
async function startTracking() {
  visionLib ||= await import('https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs');
  const files = await visionLib.FilesetResolver.forVisionTasks('https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm');
  tracker = await visionLib.HandLandmarker.createFromOptions(files, {baseOptions: {modelAssetPath:
    'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task', delegate: pickQuality().delegate},
    runningMode: 'VIDEO', numHands: 2});
}
async function restartTracking() { if (!tracker) return; try { tracker.close(); } catch (e) {} tracker = null; await startTracking(); }

// ---- what the hands do ----------------------------------------------------------------------
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
// Lines and points pick within a hair of the finger, not the default whole world unit.
ray.params.Line.threshold = 0.02; ray.params.Points.threshold = 0.05;
function pick(sx, sy) {
  if (!mode || !mode.targets) return null;
  ndc.set(sx / innerWidth * 2 - 1, -(sy / innerHeight * 2 - 1)); ray.setFromCamera(ndc, camera);
  const hits = ray.intersectObjects(mode.targets(), true);
  for (const h of hits) { let o = h.object; while (o && !o.userData.target && o.parent) o = o.parent; if (o && o.userData.target) return {obj: o, point: h.point, uv: h.uv}; }
  return null;
}
let hovered = null, pinchTarget = null, dragging = false, manip = null, manipBase = 1;
const spinVel = new THREE.Vector2();
let menuHold = null;

hands.on('pinchStart', ({hand}) => {
  if (hand.i !== 0 && hands.hands.length > 1 && hands.hands[1].pinch && hands.hands[0].pinch) return;     // two-hand resize
  const hud = hudAt(hand.screen.x, hand.screen.y);
  if (hud) { act(hud); pinchTarget = 'hud'; return; }
  pinchTarget = pick(hand.screen.x, hand.screen.y); dragging = false;
  if (!pinchTarget) menuHold = {x: hand.screen.x, y: hand.screen.y, at: performance.now()};
});
hands.on('pinchMove', ({hand}) => {
  if (!pinchTarget || pinchTarget === 'hud') return;
  const z = pinchTarget.obj.getWorldPosition(new THREE.Vector3()).z;
  if (!dragging && mode.grab) {
    const moved = Math.hypot(hand.screen.x - (hands.prev[hand.i].pinchFrom || hand.screen).x, hand.screen.y - (hands.prev[hand.i].pinchFrom || hand.screen).y);
    if ((hands.prev[hand.i].pinchFrom || {}).moved || moved > 30) dragging = mode.grab(pinchTarget.obj, hand) === true;
  }
  if (dragging && mode.drag) mode.drag(hand, ctx.planePoint(hand.screen.x, hand.screen.y, z));
});
hands.on('pinchEnd', ({hand, quick}) => {
  if (pinchTarget && pinchTarget !== 'hud') {
    if (dragging) mode.drop && mode.drop(hand);
    else if (quick && mode.press) mode.press(pinchTarget.obj, hand, pinchTarget);
  }
  pinchTarget = null; dragging = false; menuHold = null;
});
hands.on('pinchHold', ({hand}) => {
  if (pinchTarget && pinchTarget !== 'hud' && !dragging) { mode.hold && mode.hold(pinchTarget.obj, hand, pinchTarget); return; }
  if (!pinchTarget && menuHold) { openMenu(hand.screen); menuHold = null; }
});
hands.on('doublePinch', ({hand}) => { if (!pick(hand.screen.x, hand.screen.y) && !hudAt(hand.screen.x, hand.screen.y)) openMenu(hand.screen); });
hands.on('fistStart', ({hand}) => {
  const hit = pick(hand.centerScreen.x, hand.centerScreen.y);
  manip = (mode && mode.manipulable && mode.manipulable(hit && hit.obj)) || (mode && mode.group);
  manip && (manip.userData._fistSize = hand.size);
});
hands.on('fistMove', ({hand, dx, dy, dAngle}) => {
  if (!manip) return;
  const depth = manip.getWorldPosition(new THREE.Vector3());
  const a = ctx.planePoint(hand.centerScreen.x - dx, hand.centerScreen.y - dy, depth.z), b = ctx.planePoint(hand.centerScreen.x, hand.centerScreen.y, depth.z);
  manip.position.x += b.x - a.x; manip.position.y += b.y - a.y;
  // pushing the fist toward the camera (hand grows) pulls the object nearer
  const grow = hand.size - (manip.userData._fistSize || hand.size); manip.userData._fistSize = hand.size;
  manip.position.z += grow * 18 * (S.depthInvert ? -1 : 1);
  manip.rotation.z += dAngle * 0.9;
});
hands.on('fistEnd', () => {});
hands.on('flick', ({hand, vx, vy}) => {
  const hit = pick(hand.screen.x, hand.screen.y);
  manip = (mode && mode.manipulable && mode.manipulable(hit && hit.obj)) || manip || (mode && mode.group);
  spinVel.set(vx * 2.2, vy * 2.2);
});
hands.on('resize', ({ratio}) => {
  if (!manip) { const h = hands.hands[0]; const hit = h && pick(h.screen.x, h.screen.y); manip = (mode && mode.manipulable && mode.manipulable(hit && hit.obj)) || (mode && mode.group); }
  if (!manip) return;
  if (manip.userData._base === undefined) manip.userData._base = manip.scale.x;
  const s = THREE.MathUtils.clamp(manip.userData._base * ratio, 0.25, 4); manip.scale.setScalar(s);
});
hands.on('resizeEnd', () => { if (manip) delete manip.userData._base; });
['clap', 'twist', 'fistsApart', 'palmsApart', 'palmFlip'].forEach(ev => hands.on(ev, d => {
  if (!mode || !mode[ev]) return;
  if (ev === 'twist') mode.twist(d.dir); else if (ev === 'palmFlip') mode.palmFlip(d.up); else mode[ev]();
}));

// ---- overlay: hands, cursor, menu-hold ring ---------------------------------------------------
const ov = $('ov'), og = ov.getContext('2d');
function sizeOv() { ov.width = innerWidth * devicePixelRatio; ov.height = innerHeight * devicePixelRatio; hands.view = {w: innerWidth, h: innerHeight}; }
sizeOv();
addEventListener('resize', () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); applyRatio(); sizeOv(); });
const BONES = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]];
function drawHands(now) {
  const k = devicePixelRatio; og.clearRect(0, 0, ov.width, ov.height);
  hands.hands.forEach(h => {
    if (S.skeleton) {
      og.strokeStyle = 'rgba(111,243,255,.45)'; og.lineWidth = 1.4 * k; og.shadowColor = '#6ff3ff'; og.shadowBlur = 6 * k;
      BONES.forEach(([a, b]) => { const p = hands.toScreen(h.lm[a]), q = hands.toScreen(h.lm[b]); og.beginPath(); og.moveTo(p.x * k, p.y * k); og.lineTo(q.x * k, q.y * k); og.stroke(); });
    }
    // fingertip: gold while pointing, white on pinch, cyan otherwise
    const c = h.screen, col = h.pinch ? '#ffffff' : h.point ? '#ffd27a' : '#6ff3ff';
    og.shadowColor = col; og.shadowBlur = 14 * k; og.strokeStyle = col; og.lineWidth = 2 * k;
    og.beginPath(); og.arc(c.x * k, c.y * k, (h.pinch ? 7 : 12) * k, 0, Math.PI * 2); og.stroke();
    if (h.point) { og.fillStyle = 'rgba(255,210,122,.35)'; og.fill(); }
  });
  if (menuHold) {          // the ring that fills before the menu opens
    const p = Math.min(1, (now - menuHold.at) / 800); og.strokeStyle = '#6ff3ff'; og.lineWidth = 3 * k;
    og.beginPath(); og.arc(menuHold.x * k, menuHold.y * k, 26 * k, -Math.PI / 2, -Math.PI / 2 + p * Math.PI * 2); og.stroke();
  }
  og.shadowBlur = 0;
}

// ---- start + loop ----------------------------------------------------------------------------
$('go').addEventListener('click', async () => {
  $('gate').classList.add('off'); $('state').textContent = 'STARTING CAMERA';
  try { await startCamera(); } catch (e) { $('state').textContent = 'NO CAMERA ACCESS'; toast('Allow the camera for this page, then reload.'); return; }
  $('state').textContent = 'LOADING HAND TRACKING';
  try { await startTracking(); } catch (e) { S.quality = 'safe'; paintSettings(); try { await startTracking(); } catch (e2) { $('state').textContent = 'TRACKING FAILED'; toast('Hand tracking could not start: ' + e2); return; } }
  $('state').textContent = 'READY · PINCH & HOLD FOR THE MENU';
});

const clock = new THREE.Clock(); let frames = 0, fpsAt = performance.now(), fps = 0;
function loop() {
  const dt = Math.min(0.05, clock.getDelta()), t = clock.elapsedTime, now = performance.now();
  if (tracker && video.readyState >= 2) {
    const res = tracker.detectForVideo(video, now); const hs = hands.update(res, now);
    const h0 = hs[0];
    if (mode && mode.hover) { const hit = h0 && (h0.point || !h0.fist) ? pick(h0.screen.x, h0.screen.y) : null; const o = hit && hit.obj; if (o !== hovered || (hit && mode.hoverEvery)) { hovered = o; mode.hover(o, hit); } }
    $('state').textContent = hs.length ? (hs.map(h => h.pinch ? 'PINCH' : h.fist ? 'FIST' : h.point ? 'POINT' : h.palm ? (h.palmUp ? 'PALM UP' : 'PALM') : 'HAND').join(' · ')) : 'SHOW YOUR HANDS';
  }
  if (manip && spinVel.lengthSq() > 1e-6) { manip.rotation.y += spinVel.x * dt; manip.rotation.x += spinVel.y * dt; spinVel.multiplyScalar(Math.pow(0.18, dt)); }
  if (mode && mode.update) mode.update(dt, t);
  drawHands(now);
  renderer.render(scene, camera);
  frames++; if (now - fpsAt > 1000) { fps = frames; frames = 0; fpsAt = now; if (S.debug) $('debug').textContent = `${fps} fps · ${hands.hands.length} hands · quality ${S.quality} · mode ${mode && mode.id}`; }
  requestAnimationFrame(loop);
}

paintSettings();
await registerModes();
let first = 'xenon'; try { first = localStorage.getItem('xholo-mode') || 'xenon'; } catch (e) {}
setMode(MODES[first] ? first : 'xenon');
loop();
// Test hook (only with ?debug): drive gestures with recorded or synthetic landmarks.
if (params.has('debug')) window.__holo = {hands, MODES, setMode, mode: () => mode, openMenu, pick, camera, THREE, act, S};
