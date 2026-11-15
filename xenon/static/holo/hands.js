// Xenon Holo - hand gesture engine.
//
// Turns MediaPipe hand landmarks (up to two hands, in camera coordinates
// 0..1, NOT mirrored) into poses and gesture events. The page decides what
// the gestures do; this file only recognises them.
//
// Poses per hand:  pinch · point · fist · palm (open) · palmUp (palm facing up)
// Events (emitted through on(name, fn)):
//   pinchStart / pinchMove / pinchEnd     {hand}
//   pinchHold                            {hand}          pinch held still 0.8 s
//   doublePinch                          {hand}          two quick pinches
//   fistStart / fistMove / fistEnd       {hand, dx, dy, dAngle}
//   twist                                {dir: +1 clockwise | -1 counter}
//   flick                                {hand, vx, vy}  pointing finger flicked
//   resize                               {ratio}         both hands pinching
//   clap                                 {}
//   fistsApart                           {}              two fists pulled apart
//   palmsApart                           {}              palms together, then apart fast
//   palmFlip                             {up: bool}
//
// Each hand object: {i, lm, pinch, point, fist, palm, palmUp, tip:{x,y}, center:{x,y},
//   size, angle, screen:{x,y}} where screen is in mirrored view pixels.

const D = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

export class Hands {
  constructor() {
    this.listeners = {};
    this.mirror = true;
    this.view = { w: innerWidth, h: innerHeight };
    this.hands = [];
    this.prev = [{}, {}];
    this.lastPinchAt = [0, 0];
    this.clapCool = 0;
    this.twoFist = null;
    this.palmsTogetherAt = 0;
    this.resizeStart = null;
  }

  on(name, fn) { (this.listeners[name] ||= []).push(fn); return this; }
  emit(name, data) { (this.listeners[name] || []).forEach(fn => { try { fn(data || {}); } catch (e) { console.error(e); } }); }

  // Map a landmark to view pixels (mirrored like a mirror when this.mirror).
  // The video is shown with object-fit: cover, so when the window's shape
  // differs from the camera's, part of the picture is cropped; map through it.
  toScreen(p) {
    const { w, h, vw, vh } = this.view;
    const x = this.mirror ? 1 - p.x : p.x;
    if (!vw || !vh) return { x: x * w, y: p.y * h };
    const s = Math.max(w / vw, h / vh);
    return { x: (w - vw * s) / 2 + x * vw * s, y: (h - vh * s) / 2 + p.y * vh * s };
  }

  pose(lm, handed, was = {}) {
    const size = D(lm[0], lm[9]) || 1e-3;
    const pinchD = D(lm[4], lm[8]) / size;
    const ext = (tip, pip) => D(lm[tip], lm[0]) > D(lm[pip], lm[0]) * 1.12;
    const ix = ext(8, 6), mx = ext(12, 10), rx = ext(16, 14), px = ext(20, 18);
    // In a real fist the thumb wraps over the index, so thumb and index tips
    // touch too; a pinch keeps the index tip clear of its own knuckle
    // (fist ~0.2, pinch ~0.5, measured on real photos through MediaPipe).
    // Hysteresis like the original: start below 0.30, let go above 0.42, so a
    // jittery real hand doesn't flicker into accidental double pinches.
    const fold = D(lm[8], lm[5]) / size;
    const pinch = was.pinch ? pinchD < 0.42 && fold > 0.28 : pinchD < 0.30 && fold > 0.35;
    const fist = !ix && !mx && !rx && !px && !pinch;
    const point = ix && !mx && !rx && !px && !pinch;
    const palm = ix && mx && rx && px && !pinch && D(lm[8], lm[20]) / size > 0.55;   // fingers spread (real hands: ~0.6-1.2)
    // Palm up/down: the palm normal from (index base - wrist) x (pinky base - wrist).
    const ax = lm[5].x - lm[0].x, ay = lm[5].y - lm[0].y, az = (lm[5].z || 0) - (lm[0].z || 0);
    const bx = lm[17].x - lm[0].x, by = lm[17].y - lm[0].y, bz = (lm[17].z || 0) - (lm[0].z || 0);
    const nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
    const flip = handed === 'Left' ? -1 : 1;
    // With the handedness flip this normal points OUT of the palm (checked on
    // real photos: palm toward the camera gives z < 0, the back of the hand
    // z > 0). Image y grows downward, so a palm facing the ceiling has a
    // negative y. Unit length, so distance to the camera doesn't matter.
    const up = -ny * flip / (Math.hypot(nx, ny, nz) || 1);
    const palmUp = palm && up > 0.6;                  // same bar as the original
    const angle = Math.atan2(lm[9].y - lm[0].y, lm[9].x - lm[0].x);
    const center = { x: (lm[0].x + lm[9].x) / 2, y: (lm[0].y + lm[9].y) / 2 };
    return { lm, size, pinch, pinchD, point, fist, palm, palmUp, up, handed, angle, tip: lm[8], center };
  }

  // Feed one MediaPipe result. now = ms.
  update(result, now) {
    const lms = (result && result.landmarks) || [];
    const handed = (result && (result.handedness || result.handednesses)) || [];
    const hands = lms.map((lm, i) => {
      const h = this.pose(lm, handed[i] && handed[i][0] && handed[i][0].categoryName, this.prev[i] || {});
      h.i = i;
      h.screen = this.toScreen(h.tip);
      h.centerScreen = this.toScreen(h.center);
      return h;
    });
    this.hands = hands;

    hands.forEach((h, i) => this.single(h, this.prev[i] || (this.prev[i] = {}), now));
    for (let i = hands.length; i < 2; i++) {          // a hand left the picture
      const p = this.prev[i];
      if (p && p.pinch) this.emit('pinchEnd', { hand: p.last });
      if (p && p.fist) this.emit('fistEnd', { hand: p.last });
      this.prev[i] = {};
    }
    this.pairs(hands, now);
    return hands;
  }

  single(h, p, now) {
    const sx = h.screen.x, sy = h.screen.y;
    // ---- pinch: start / move / hold / end, and double pinch
    if (h.pinch && !p.pinch) {
      p.pinchAt = now; p.pinchFrom = { x: sx, y: sy }; p.held = false;
      if (now - (this.lastPinchAt[h.i] || 0) < 380) this.emit('doublePinch', { hand: h });
      this.lastPinchAt[h.i] = now;
      this.emit('pinchStart', { hand: h });
    } else if (h.pinch && p.pinch) {
      this.emit('pinchMove', { hand: h });
      const still = Math.hypot(sx - p.pinchFrom.x, sy - p.pinchFrom.y) < 28;
      if (!p.held && still && now - p.pinchAt > 800) { p.held = true; this.emit('pinchHold', { hand: h }); }
      if (!still) p.pinchFrom = { x: sx, y: sy, moved: true };
    } else if (!h.pinch && p.pinch) {
      this.emit('pinchEnd', { hand: h, quick: now - p.pinchAt < 450 });
    }
    // ---- fist: move + twist (wrist rotation)
    if (h.fist) {
      const c = h.centerScreen;
      if (!p.fist) { p.fistFrom = c; p.twistAnchor = c; p.fistAngle = h.angle; p.twistAcc = 0; this.emit('fistStart', { hand: h }); }
      else {
        let dA = h.angle - p.angleLast; if (dA > Math.PI) dA -= 2 * Math.PI; if (dA < -Math.PI) dA += 2 * Math.PI;
        if (this.mirror) dA = -dA;
        this.emit('fistMove', { hand: h, dx: c.x - p.lastC.x, dy: c.y - p.lastC.y, dAngle: dA, now });
        // A twist is the wrist turning (~50 degrees) while the fist stays
        // within about half a hand of where the turn began; carrying the fist
        // somewhere (to move an object) restarts the count.
        const handPx = h.size * this.view.h;
        if (Math.hypot(c.x - p.twistAnchor.x, c.y - p.twistAnchor.y) > handPx * 0.6) { p.twistAnchor = c; p.twistAcc = 0; }
        else p.twistAcc += dA;
        if (Math.abs(p.twistAcc) > 0.9) { this.emit('twist', { dir: p.twistAcc > 0 ? 1 : -1 }); p.twistAcc = 0; p.twistAnchor = c; }
      }
      p.lastC = c;
    } else if (p.fist) this.emit('fistEnd', { hand: h });
    // ---- flick: pointing fingertip moving fast, then stopping
    if (h.point) {
      const v = p.lastTip ? { x: sx - p.lastTip.x, y: sy - p.lastTip.y, t: now - p.lastTipAt } : null;
      if (v && v.t > 0) {
        const speed = Math.hypot(v.x, v.y) / v.t;          // px per ms
        if (speed > 2.2 && now - (p.flickAt || 0) > 500) { p.flickAt = now; this.emit('flick', { hand: h, vx: v.x / v.t, vy: v.y / v.t }); }
      }
    }
    p.lastTip = { x: sx, y: sy }; p.lastTipAt = now;
    // ---- palm flip (up / down), debounced
    if (h.palm) {
      const up = h.palmUp;
      if (p.palmUp === undefined) p.palmUp = false;   // a fresh hand counts as palm down, so showing it palm up flips
      if (up !== p.palmUp) { p.flipCount = (p.flipCount || 0) + 1; if (p.flipCount > 6) { p.palmUp = up; p.flipCount = 0; this.emit('palmFlip', { up, hand: h }); } }
      else p.flipCount = 0;
    }
    p.angleLast = h.angle;
    p.pinch = h.pinch; p.fist = h.fist; p.palm = h.palm; p.last = h;
  }

  pairs(hands, now) {
    if (hands.length < 2) { this.twoFist = null; this.resizeStart = null; return; }
    const [a, b] = hands;
    const d = Math.hypot(a.centerScreen.x - b.centerScreen.x, a.centerScreen.y - b.centerScreen.y);
    const scale = (a.size + b.size) / 2 * this.view.h;      // hand size in px
    // clap: two open palms meeting fast
    const prevD = this.pairD; this.pairD = d; const dt = now - (this.pairAt || now); this.pairAt = now;
    if (a.palm && b.palm && prevD !== undefined && dt > 0) {
      const closing = (prevD - d) / dt;                      // px per ms
      if (d < scale * 1.3 && closing > 0.9 && now > this.clapCool) { this.clapCool = now + 900; this.emit('clap'); }
      if (d < scale * 1.2) this.palmsTogetherAt = now;
      if (d > scale * 3 && now - this.palmsTogetherAt < 450 && now > this.clapCool) { this.clapCool = now + 900; this.palmsTogetherAt = 0; this.emit('palmsApart'); }
    }
    // two fists pulled apart
    if (a.fist && b.fist) {
      if (!this.twoFist) this.twoFist = d;
      else if (d > this.twoFist * 1.6) { this.emit('fistsApart'); this.twoFist = Infinity; }
    } else this.twoFist = null;
    // resize: both hands pinching
    if (a.pinch && b.pinch) {
      const pd = Math.hypot(a.screen.x - b.screen.x, a.screen.y - b.screen.y);
      if (!this.resizeStart) this.resizeStart = pd;
      this.emit('resize', { ratio: pd / Math.max(10, this.resizeStart) });
    } else if (this.resizeStart) { this.resizeStart = null; this.emit('resizeEnd'); }
  }
}
