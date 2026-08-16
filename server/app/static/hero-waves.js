/* Flowing water-ripple canvas for the dark hero (kin to dshdesktop.cn).
   Self-contained, no deps. Layered drifting sine ribbons + soft light pools
   on a deep blue ground. Pauses offscreen; honors prefers-reduced-motion. */
(function () {
  "use strict";
  var canvas = document.getElementById("hero-waves");
  if (!canvas) return;
  var ctx = canvas.getContext("2d");
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0, t = 0, running = false, raf = 0;

  function resize() {
    var r = canvas.parentElement.getBoundingClientRect();
    W = Math.max(1, r.width); H = Math.max(1, r.height);
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /* one silky ribbon: a band of translucent sine strokes */
  function ribbon(baseY, amp, wavelen, phase, drift, strokes, hue, alpha, slope) {
    var i, x, y, k;
    for (i = 0; i < strokes; i++) {
      var off = (i - strokes / 2) * (amp * 0.16);
      var localPhase = phase + i * 0.22;
      ctx.beginPath();
      for (x = -40; x <= W + 40; x += 14) {
        k = x / wavelen;
        y = baseY + off + (x - W / 2) * slope
          + Math.sin(k * 2.1 + localPhase) * amp
          + Math.sin(k * 0.7 - localPhase * 0.6) * amp * 0.55
          + Math.sin(k * 4.3 + localPhase * 1.7) * amp * 0.18
          + drift;
        if (x === -40) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = "hsla(" + hue + ", 62%, " + (58 + i * 1.1) + "%, " + alpha + ")";
      ctx.lineWidth = 1.1;
      ctx.stroke();
    }
  }

  function pool(cx, cy, r, a) {
    var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    g.addColorStop(0, "rgba(120, 168, 255, " + a + ")");
    g.addColorStop(1, "rgba(120, 168, 255, 0)");
    ctx.fillStyle = g;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
  }

  function frame() {
    /* deep blue ground */
    var g = ctx.createLinearGradient(0, 0, W * 0.3, H);
    g.addColorStop(0, "#0a1830");
    g.addColorStop(0.55, "#0f2547");
    g.addColorStop(1, "#0b1c38");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    /* drifting light pools */
    pool(W * (0.22 + 0.03 * Math.sin(t * 0.21)), H * 0.18, Math.max(W, H) * 0.5, 0.10);
    pool(W * (0.78 + 0.04 * Math.cos(t * 0.17)), H * 0.65, Math.max(W, H) * 0.45, 0.07);

    /* ripple ribbons — slow, layered, silky */
    ribbon(H * 0.22, 30, 430, t * 0.55, Math.sin(t * 0.30) * 9, 16, 218, 0.058, -0.10);
    ribbon(H * 0.55, 44, 640, -t * 0.38, Math.cos(t * 0.22) * 12, 20, 214, 0.050, 0.07);
    ribbon(H * 0.85, 34, 540, t * 0.44 + 2.1, Math.sin(t * 0.26 + 1) * 10, 16, 210, 0.055, -0.06);

    t += 0.016;
  }

  function loop() { frame(); raf = requestAnimationFrame(loop); }
  function start() { if (!running) { running = true; raf = requestAnimationFrame(loop); } }
  function stop() { running = false; cancelAnimationFrame(raf); }

  resize();
  window.addEventListener("resize", function () { resize(); if (reduced) frame(); });

  if (reduced) { frame(); return; }  /* single static frame, no motion */

  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      entries[0].isIntersecting ? start() : stop();
    }).observe(canvas);
  } else {
    start();
  }
  document.addEventListener("visibilitychange", function () {
    document.hidden ? stop() : start();
  });
})();
