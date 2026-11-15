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
  labels: true, skeleton: true, occlusion: true, debug: false, camIndex: 0,
};
function qualityName() {
  if (QUALITY[S.quality]) return S.quality;
  const cores = navigator.hardwareConcurrency || 4;
  return cores >= 12 ? 'high' : cores >= 6 ? 'medium' : 'low';
}
function pickQuality() { return QUALITY[qualityName()]; }

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
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
// Card text comes from modes and live status (track names), so it is always escaped.
function card({title, kicker = '', rows = [], body = '', actions = []}) {
  const el = $('card');
  el.innerHTML = `<div class="ck">${esc(kicker)}</div><div class="ct">${esc(title)}</div>` +
    rows.map(([k, v]) => `<div class="cr"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('') +
    (body ? `<p>${esc(body)}</p>` : '') +
    actions.map(a => `<button class="cx" data-act="${esc(a.act)}" data-s="${esc(a.s || '')}">${esc(a.label)}</button>`).join('') +
    `<button class="cx" data-act="close-card">CLOSE</button>`;
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
let cueState = null;
const ctx = {THREE, scene, camera, toast, card, closeCard, xenon, status: () => status, loadModel, setTray, hands: () => hands.hands,
  cue: c => { cueState = c || null; },
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
  manip = null; spinVel.set(0, 0); closeMenu(); cueState = null; throwing = null; holding = false; resizing = false;
  try { localStorage.setItem('xholo-mode', id); } catch (e) {}
}

// Menu: a ring of buttons around where it was opened.
let menuOpen = false;
function openMenu(at) {
  const el = $('menu');
  const items = [
    ...Object.values(MODES).map(m => ({label: m.label, run: () => setMode(m.id), on: m === mode})),
    ...((mode && mode.menu && mode.menu()) || []),
    {label: 'CLOSE', run: () => {}},                 // hands-only: a way out without the keyboard
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
  if (a === 'settings-toggle') { document.querySelector('.settings').classList.toggle('open'); return true; }
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
  if (s === 'occlusion') S.occlusion = !S.occlusion;
  if (s === 'debug') S.debug = !S.debug;
  if (s === 'camera') nextCamera();
  if (s === 'lower') { const order = ['high', 'medium', 'low', 'safe']; S.quality = order[Math.min(3, order.indexOf(qualityName()) + 1)]; applyRatio(); restartTracking(); closeCard(); toast('QUALITY · ' + S.quality.toUpperCase()); }
  if (s === 'quality') { const order = ['auto', 'high', 'medium', 'low', 'safe']; S.quality = order[(order.indexOf(S.quality) + 1) % order.length]; applyRatio(); restartTracking(); }
  paintSettings();
}
const videoFilter = () => (S.bw ? 'grayscale(1) contrast(1.15) sepia(.35) hue-rotate(150deg) ' : '') + `brightness(${S.dim ? 0.18 : 0.42})`;
function paintSettings() {
  const v = $('cam'); v.style.transform = S.mirror ? 'scaleX(-1)' : 'none';
  v.style.filter = videoFilter();
  const label = {mirror: 'MIRROR', invert: 'DEPTH INVERT', bw: 'B&W', dim: 'DIM', labels: 'LABELS', skeleton: 'HANDS', occlusion: 'OCCLUSION', debug: 'DEBUG'};
  document.querySelectorAll('[data-s]').forEach(b => {
    const s = b.dataset.s;
    if (s === 'quality') b.textContent = 'QUALITY · ' + S.quality.toUpperCase();
    else if (s === 'camera') b.textContent = 'CAMERA ' + (S.camIndex + 1);
    else { const on = {mirror: S.mirror, invert: S.depthInvert, bw: S.bw, dim: S.dim, labels: S.labels, skeleton: S.skeleton, occlusion: S.occlusion, debug: S.debug}[s]; b.textContent = label[s]; b.classList.toggle('on', !!on); }
  });
  $('debug').style.display = S.debug ? 'block' : 'none';
}
addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (k === 'escape') { if (menuOpen) closeMenu(); else if ($('guide').classList.contains('on')) $('guide').classList.remove('on'); else closeCard(); return; }
  if (mode && mode.key && mode.key(k)) return;
  ({m: () => menuOpen ? closeMenu() : openMenu(), f: () => setting('mirror'), i: () => setting('invert'), b: () => setting('bw'),
    c: () => setting('camera'), l: () => setting('labels'), o: () => setting('occlusion'), k: () => setting('skeleton'), d: () => setting('debug'),
    h: () => $('guide').classList.toggle('on'), q: () => setting('quality'), r: () => { if (S.debug) saveRecording(); }}[k] || (() => {}))();
});

// ---- camera + tracking ----------------------------------------------------------------------
const video = $('cam');
const hands = new Hands();
let tracker = null, cams = [];
async function startCamera() {
  const q = pickQuality();
  if (video.srcObject) video.srcObject.getTracks().forEach(t => t.stop());
  const device = cams[S.camIndex];
  const want = {...q.cam, ...(device ? {deviceId: {exact: device.deviceId}} : {facingMode: 'user'})};
  try { video.srcObject = await navigator.mediaDevices.getUserMedia({video: want, audio: false}); }
  catch (e) { if (e.name !== 'OverconstrainedError') throw e; video.srcObject = await navigator.mediaDevices.getUserMedia({video: true, audio: false}); }
  await video.play();
  syncVideoSize();
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
// Throw: a fist released while moving fast sends the object flying; it slows,
// bounces off the edges of the view and settles where it stops.
const fistVel = new THREE.Vector2(), throwVel = new THREE.Vector2();
let fistAt = 0, throwing = null, holding = false, resizing = false;
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
  fistVel.set(0, 0); fistAt = 0; holding = !!manip; if (manip && manip === throwing) throwing = null;
});
hands.on('fistMove', ({hand, dx, dy, dAngle, now: tNow}) => {
  if (!manip) return;
  const depth = manip.getWorldPosition(new THREE.Vector3());
  const a = ctx.planePoint(hand.centerScreen.x - dx, hand.centerScreen.y - dy, depth.z), b = ctx.planePoint(hand.centerScreen.x, hand.centerScreen.y, depth.z);
  manip.position.x += b.x - a.x; manip.position.y += b.y - a.y;
  const now = tNow || performance.now(), dts = (now - fistAt) / 1000; fistAt = now;
  if (dts > 0 && dts < .2) fistVel.lerp(new THREE.Vector2((b.x - a.x) / dts, (b.y - a.y) / dts), .5);
  // pushing the fist toward the camera (hand grows) pulls the object nearer
  const grow = hand.size - (manip.userData._fistSize || hand.size); manip.userData._fistSize = hand.size;
  manip.position.z += grow * 18 * (S.depthInvert ? -1 : 1);
  manip.rotation.z += dAngle * 0.9;
});
hands.on('fistEnd', () => {
  if (manip && holding && fistVel.length() > 3.2) { throwing = manip; delete manip.userData._flyAt; throwVel.copy(fistVel).clampLength(0, 14); }
  holding = false;
});
hands.on('flick', ({hand, vx, vy}) => {
  const hit = pick(hand.screen.x, hand.screen.y);
  manip = (mode && mode.manipulable && mode.manipulable(hit && hit.obj)) || manip || (mode && mode.group);
  spinVel.set(vx * 2.2, vy * 2.2);
});
hands.on('resize', ({ratio}) => {
  if (!manip) { const h = hands.hands[0]; const hit = h && pick(h.screen.x, h.screen.y); manip = (mode && mode.manipulable && mode.manipulable(hit && hit.obj)) || (mode && mode.group); }
  if (!manip) return;
  if (manip.userData._base === undefined) manip.userData._base = manip.scale.x;
  const s = THREE.MathUtils.clamp(manip.userData._base * ratio, 0.25, 4); manip.scale.setScalar(s); resizing = true;
});
hands.on('resizeEnd', () => { if (manip) delete manip.userData._base; resizing = false; });
['clap', 'twist', 'fistsApart', 'palmsApart', 'palmFlip'].forEach(ev => hands.on(ev, d => {
  if (!mode || !mode[ev]) return;
  if (ev === 'twist') mode.twist(d.dir); else if (ev === 'palmFlip') mode.palmFlip(d.up); else mode[ev]();
}));

// ---- overlay: hands, cursor, menu-hold ring ---------------------------------------------------
const ov = $('ov'), og = ov.getContext('2d');
function syncVideoSize() { hands.view = {w: innerWidth, h: innerHeight, vw: video.videoWidth || 0, vh: video.videoHeight || 0}; }
function sizeOv() { ov.width = innerWidth * devicePixelRatio; ov.height = innerHeight * devicePixelRatio; syncVideoSize(); }
video.addEventListener('resize', syncVideoSize);
sizeOv();
addEventListener('resize', () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); applyRatio(); sizeOv(); });
const BONES = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]];
// Hand occlusion: a soft mask of each hand (bones + palm) filled with the live
// video, drawn over the holograms so your real hand passes in front of them.
const oc = document.createElement('canvas'), ocg = oc.getContext('2d');
function drawOcclusion(k) {
  if (!S.occlusion || video.readyState < 2 || !hands.hands.length || !video.videoWidth) return;
  const W = ov.width, H = ov.height;
  if (oc.width !== W || oc.height !== H) { oc.width = W; oc.height = H; }
  ocg.globalCompositeOperation = 'source-over'; ocg.filter = 'blur(' + (3 * k) + 'px)'; ocg.clearRect(0, 0, W, H);
  ocg.lineCap = ocg.lineJoin = 'round'; ocg.strokeStyle = ocg.fillStyle = '#fff';
  hands.hands.forEach(h => {
    const P = i => { const p = hands.toScreen(h.lm[i]); return [p.x * k, p.y * k]; };
    const [ax, ay] = P(0), [bx, by] = P(9), palm = Math.hypot(ax - bx, ay - by);
    ocg.lineWidth = palm * 0.34;
    BONES.forEach(([a, b]) => { ocg.beginPath(); ocg.moveTo(...P(a)); ocg.lineTo(...P(b)); ocg.stroke(); });
    ocg.beginPath(); [0, 1, 2, 5, 9, 13, 17].forEach((i, n) => n ? ocg.lineTo(...P(i)) : ocg.moveTo(...P(i))); ocg.closePath(); ocg.fill();
  });
  ocg.globalCompositeOperation = 'source-in'; ocg.filter = videoFilter();
  const vw = video.videoWidth, vh = video.videoHeight, s = Math.max(W / vw, H / vh), dw = vw * s, dh = vh * s;
  ocg.save(); if (S.mirror) { ocg.translate(W, 0); ocg.scale(-1, 1); }
  ocg.drawImage(video, (W - dw) / 2, (H - dh) / 2, dw, dh); ocg.restore();
  ocg.filter = 'none'; ocg.globalCompositeOperation = 'source-over';
  og.drawImage(oc, 0, 0);
}
// Holographic rings on each palm and wrist (the palm ring opens with the hand)
// and a small readout: grip, openness, rough distance.
const MONO_FONT = '"JetBrains Mono", ui-monospace, monospace';
function openness(h) {
  const L = h.lm, c = {x: (L[0].x + L[5].x + L[9].x + L[13].x + L[17].x) / 5, y: (L[0].y + L[5].y + L[9].y + L[13].y + L[17].y) / 5};
  const d = [8, 12, 16, 20].reduce((s, i) => s + Math.hypot(L[i].x - c.x, L[i].y - c.y), 0) / 4 / h.size;
  return Math.min(1, Math.max(0, (d - .6) / .9));
}
function drawHandHud(h, k, now) {
  const L = h.lm, P = i => hands.toScreen(L[i]);
  const c = [0, 5, 9, 13, 17].map(P).reduce((a, p) => ({x: a.x + p.x / 5, y: a.y + p.y / 5}), {x: 0, y: 0});
  const w = P(0), size = Math.hypot(P(9).x - w.x, P(9).y - w.y), open = openness(h), t = now / 1000;
  og.save(); og.shadowColor = '#6ff3ff'; og.shadowBlur = 8 * k; og.lineCap = 'round';
  // palm: an outer dashed ring that turns, an inner arc whose length is the openness
  og.strokeStyle = 'rgba(111,243,255,.55)'; og.lineWidth = 1.2 * k; og.setLineDash([6 * k, 9 * k]); og.lineDashOffset = -t * 30 * k;
  og.beginPath(); og.arc(c.x * k, c.y * k, size * .62 * k, 0, Math.PI * 2); og.stroke(); og.setLineDash([]);
  og.strokeStyle = h.pinch ? '#ffffff' : h.fist ? '#ffd27a' : '#6ff3ff'; og.lineWidth = 2.2 * k;
  og.beginPath(); og.arc(c.x * k, c.y * k, size * .46 * k, -Math.PI / 2 + t, -Math.PI / 2 + t + Math.PI * 2 * (.15 + .85 * open)); og.stroke();
  // wrist: a flat ring across the forearm
  const ang = Math.atan2(P(9).y - w.y, P(9).x - w.x);
  og.strokeStyle = 'rgba(111,243,255,.4)'; og.lineWidth = 1.4 * k;
  og.beginPath(); og.ellipse(w.x * k, w.y * k, size * .36 * k, size * .12 * k, ang + Math.PI / 2, 0, Math.PI * 2); og.stroke();
  if (S.labels) {
    const side = h.handed === 'Left' ? 'RIGHT' : h.handed === 'Right' ? 'LEFT' : '';     // MediaPipe labels assume a mirrored picture
    const grip = h.pinch ? 'PINCH' : h.fist ? 'FIST' : open > .7 ? 'OPEN' : 'RELAXED';
    const vh = hands.view.vh || innerHeight, f = (hands.view.vw || innerWidth) / 2 / Math.tan(Math.PI / 6);
    const dist = .09 * f / Math.max(1, h.size * vh);
    const x = (c.x + size * .8) * k, y = (c.y - size * .5) * k;
    og.shadowBlur = 0; og.font = `600 ${10 * k}px ${MONO_FONT}`; og.fillStyle = '#6ff3ff';
    og.fillText(`${side} HAND`.trim(), x, y);
    og.font = `400 ${10 * k}px ${MONO_FONT}`; og.fillStyle = 'rgba(233,243,246,.8)';
    og.fillText(`GRIP ${grip} ${Math.round(open * 100)}%`, x, y + 14 * k);
    og.fillText(`DIST ${dist.toFixed(2)} m`, x, y + 28 * k);
    og.strokeStyle = 'rgba(111,243,255,.4)'; og.lineWidth = 1 * k; og.beginPath(); og.moveTo(x - 6 * k, y - 10 * k); og.lineTo(x - 6 * k, y + 32 * k); og.stroke();
  }
  og.restore();
}
function drawCue(k) {
  if (!cueState || !hands.hands.length) return;
  const pts = hands.hands.map(h => h.centerScreen), x = pts.reduce((s, p) => s + p.x, 0) / pts.length, y = Math.min(...pts.map(p => p.y)) - 120;
  const lines = cueState.lines || [], W = 190 * k, H = (26 + lines.length * 15) * k, X = x * k - W / 2, Y = y * k - H;
  og.save(); og.fillStyle = 'rgba(4,14,20,.62)'; og.strokeStyle = 'rgba(255,210,122,.7)'; og.lineWidth = 1 * k;
  og.fillRect(X, Y, W, H); og.strokeRect(X, Y, W, H);
  og.textAlign = 'center'; og.font = `600 ${11 * k}px ${MONO_FONT}`; og.fillStyle = '#ffd27a'; og.shadowColor = '#ffd27a'; og.shadowBlur = 8 * k;
  og.fillText(cueState.title, x * k, Y + 17 * k); og.shadowBlur = 0;
  og.font = `400 ${10 * k}px ${MONO_FONT}`; og.fillStyle = 'rgba(233,243,246,.85)';
  lines.forEach((l, i) => og.fillText(l, x * k, Y + (33 + i * 15) * k));
  og.restore();
}
function drawHands(now) {
  const k = devicePixelRatio; og.clearRect(0, 0, ov.width, ov.height);
  drawOcclusion(k);
  if (S.skeleton) hands.hands.forEach(h => drawHandHud(h, k, now));
  drawCue(k);
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

function cameraFailed(e) {
  const why = !window.isSecureContext ? 'The camera only works over https or on localhost. Open Holo from the Xenon tray.'
    : e && e.name === 'NotAllowedError' ? 'Camera access was blocked. Click the camera icon in the address bar, allow it, then press RETRY.'
    : e && e.name === 'NotFoundError' ? 'No camera was found. Plug one in, then press RETRY.'
    : e && e.name === 'NotReadableError' ? 'The camera is busy in another app (Camera, Teams, Zoom, Discord). Close it, then press RETRY.'
    : 'The camera could not start (' + ((e && e.name) || e) + '). Press RETRY.';
  $('state').textContent = 'NO CAMERA ACCESS';
  $('go').textContent = 'RETRY'; $('gatemsg').textContent = why; $('gate').classList.remove('off');
}
// ---- start + loop ----------------------------------------------------------------------------
$('go').addEventListener('click', async () => {
  $('gate').classList.add('off'); $('state').textContent = 'STARTING CAMERA';
  try { await startCamera(); } catch (e) { cameraFailed(e); return; }
  if (tracker) { $('state').textContent = 'READY · PINCH & HOLD FOR THE MENU'; return; }       // a retry after tracking already loaded
  $('state').textContent = 'LOADING HAND TRACKING';
  try { await startTracking(); } catch (e) { S.quality = 'safe'; paintSettings(); try { await startTracking(); } catch (e2) { $('state').textContent = 'TRACKING FAILED'; toast('Hand tracking could not start: ' + e2); return; } }
  $('state').textContent = 'READY · PINCH & HOLD FOR THE MENU';
});

let lastObjState = '', rec = [];
// D (debug) then R saves the last 120 frames of hand landmarks, for tuning gestures.
function saveRecording() {
  if (!rec.length) { toast('Nothing recorded yet · show your hands with DEBUG on'); return; }
  const url = URL.createObjectURL(new Blob([JSON.stringify(rec)], {type: 'application/json'}));
  Object.assign(document.createElement('a'), {href: url, download: 'xenon-holo-hands.json'}).click();
  setTimeout(() => URL.revokeObjectURL(url), 4000); toast(`Saved ${rec.length} frames`);
}
const clock = new THREE.Clock(); let frames = 0, fpsAt = performance.now(), fps = 0, slowFor = 0, slowOffered = false;
// Slow for 5 seconds straight: offer a lower quality, once.
function checkSpeed() {
  if (!tracker || slowOffered || document.hidden || qualityName() === 'safe') { slowFor = 0; return; }
  slowFor = fps < 22 ? slowFor + 1 : 0;
  if (slowFor >= 5) {
    slowOffered = true;
    card({kicker: 'PERFORMANCE', title: 'Running slow', rows: [['FPS', fps], ['QUALITY', qualityName().toUpperCase()]],
      body: 'A lower quality setting keeps tracking smooth.', actions: [{label: 'LOWER QUALITY', act: 'setting', s: 'lower'}]});
  }
}
function loop() {
  const dt = Math.min(0.05, clock.getDelta()), t = clock.elapsedTime, now = performance.now();
  if (tracker && video.readyState >= 2) {
    const res = tracker.detectForVideo(video, now); const hs = hands.update(res, now);
    const h0 = hs[0];
    if (mode && mode.hover) { const hit = h0 && (h0.point || !h0.fist) ? pick(h0.screen.x, h0.screen.y) : null; const o = hit && hit.obj; if (o !== hovered || (hit && mode.hoverEvery)) { hovered = o; mode.hover(o, hit); } }
    $('state').textContent = hs.length ? (hs.map(h => h.pinch ? 'PINCH' : h.fist ? 'FIST' : h.point ? 'POINT' : h.palm ? (h.palmUp ? 'PALM UP' : 'PALM') : 'HAND').join(' · ')) : 'SHOW YOUR HANDS';
  }
  if (manip && spinVel.lengthSq() > 1e-6) { manip.rotation.y += spinVel.x * dt; manip.rotation.x += spinVel.y * dt; spinVel.multiplyScalar(Math.pow(0.18, dt)); }
  // something else moved it (RESET, a mode action): the throw is over
  if (throwing && throwing.userData._flyAt && throwing.position.distanceTo(throwing.userData._flyAt) > 1e-4) throwing = null;
  if (throwing) {
    const p = throwing.position; p.x += throwVel.x * dt; p.y += throwVel.y * dt;
    const dist = Math.max(1, camera.position.z - p.z), hh = Math.tan(camera.fov * Math.PI / 360) * dist * .85, hw = hh * camera.aspect;
    if (Math.abs(p.x - camera.position.x) > hw) { p.x = camera.position.x + Math.sign(p.x - camera.position.x) * hw; throwVel.x *= -.6; }
    if (Math.abs(p.y - camera.position.y) > hh) { p.y = camera.position.y + Math.sign(p.y - camera.position.y) * hh; throwVel.y *= -.6; }
    throwVel.multiplyScalar(Math.pow(.25, dt));
    if (throwVel.length() < .12) throwing = null; else (throwing.userData._flyAt ||= new THREE.Vector3()).copy(p);
  }
  const objState = throwing ? 'IN FLIGHT' : resizing ? 'MANIPULATING' : holding ? 'HELD' : 'STANDBY';
  if (objState !== lastObjState) { lastObjState = objState; $('objState').textContent = objState; $('objState').classList.toggle('live', objState !== 'STANDBY'); }
  if (S.debug && hands.hands.length) { rec.push({t: now, hands: hands.hands.map(h => ({handed: h.handed, landmarks: h.lm.map(({x, y, z}) => ({x, y, z}))}))}); if (rec.length > 120) rec.shift(); }
  if (mode && mode.update) mode.update(dt, t);
  drawHands(now);
  renderer.render(scene, camera);
  frames++; if (now - fpsAt > 1000) { fps = frames; frames = 0; fpsAt = now; checkSpeed(); if (S.debug) $('debug').textContent = `${fps} fps · ${hands.hands.length} hands · quality ${S.quality} · mode ${mode && mode.id}`; }
  requestAnimationFrame(loop);
}

paintSettings();
await registerModes();
let first = 'xenon'; try { first = localStorage.getItem('xholo-mode') || 'xenon'; } catch (e) {}
setMode(MODES[first] ? first : 'xenon');
loop();
// Test hook (only with ?debug): drive gestures with recorded or synthetic landmarks.
if (params.has('debug')) window.__holo = {hands, MODES, setMode, mode: () => mode, openMenu, pick, camera, THREE, act, S,
  throwState: () => ({throwing: !!throwing, v: throwVel.length(), fv: fistVel.length(), holding, manip: manip && manip.uuid})};
