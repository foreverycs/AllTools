/**
 * Soft floating particles for page backgrounds (.site-bg / .bg / .home-bg).
 * Particles drift toward the pointer; respects prefers-reduced-motion.
 */
(function () {
  "use strict";

  var REDUCED =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var MAX_DPR = 1.75;
  var LINK_DIST = 150;
  /** Radius (px) where the pointer pulls nearby particles. */
  var ATTRACT_RADIUS = 280;
  /** Max acceleration toward cursor (px per frame-normalized unit). */
  var ATTRACT_STRENGTH = 0.14;
  /** Soft push when almost on top of the cursor (avoids a hard clump). */
  var REPEL_RADIUS = 42;
  var REPEL_STRENGTH = 0.16;
  /** Velocity damping each frame. */
  var DRAG = 0.982;
  var MAX_SPEED = 3.6;
  var PALETTE = [
    [99, 102, 241], // indigo
    [245, 158, 11], // amber
    [16, 185, 129], // emerald
    [124, 58, 237], // violet
    [14, 165, 233], // sky
  ];

  // Shared pointer state (one listener for all canvas mounts).
  var pointer = {
    x: 0,
    y: 0,
    active: false,
    /** 0..1 fade when pointer leaves / returns */
    strength: 0,
  };
  var pointerBound = false;

  function bindPointer() {
    if (pointerBound || REDUCED) return;
    pointerBound = true;

    function onMove(e) {
      var x;
      var y;
      if (e.touches && e.touches.length) {
        x = e.touches[0].clientX;
        y = e.touches[0].clientY;
      } else {
        x = e.clientX;
        y = e.clientY;
      }
      pointer.x = x;
      pointer.y = y;
      pointer.active = true;
    }

    function onLeave() {
      pointer.active = false;
    }

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("touchmove", onMove, { passive: true });
    window.addEventListener("pointerdown", onMove, { passive: true });
    window.addEventListener("blur", onLeave);
    document.addEventListener("mouseleave", onLeave);
    document.documentElement.addEventListener("mouseleave", onLeave);
  }

  function clamp(n, a, b) {
    return Math.max(a, Math.min(b, n));
  }

  function particleCount(w, h) {
    var area = w * h;
    var n = Math.round(area / 9000);
    return clamp(n, 48, 110);
  }

  function pickColor() {
    return PALETTE[(Math.random() * PALETTE.length) | 0];
  }

  /**
   * @param {HTMLElement} host
   */
  function mount(host) {
    if (!host || host.dataset.particles === "1") return;
    host.dataset.particles = "1";

    if (getComputedStyle(host).position === "static") {
      host.style.position = "fixed";
    }

    var canvas = document.createElement("canvas");
    canvas.className = "bg-particles";
    canvas.setAttribute("aria-hidden", "true");
    host.appendChild(canvas);

    var ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    var particles = [];
    var w = 0;
    var h = 0;
    var dpr = 1;
    var raf = 0;
    var running = false;
    var last = 0;

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
      w = Math.max(1, window.innerWidth);
      h = Math.max(1, window.innerHeight);
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = w + "px";
      canvas.style.height = h + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      seed(particleCount(w, h));
    }

    function seed(n) {
      var next = [];
      for (var i = 0; i < n; i++) {
        var prev = particles[i];
        var rgb = prev ? prev.rgb : pickColor();
        next.push({
          x: prev ? clamp(prev.x, 0, w) : Math.random() * w,
          y: prev ? clamp(prev.y, 0, h) : Math.random() * h,
          vx: (Math.random() - 0.5) * 0.42,
          vy: (Math.random() - 0.5) * 0.42 - 0.06,
          r: 1.8 + Math.random() * 3.4,
          a: 0.45 + Math.random() * 0.4,
          rgb: rgb,
          tw: Math.random() * Math.PI * 2,
          tws: 0.8 + Math.random() * 1.4,
          /** Per-particle mass-ish factor so motion feels organic */
          mass: 0.65 + Math.random() * 0.7,
        });
      }
      particles = next;
    }

    function step(ts) {
      if (!running) return;
      raf = requestAnimationFrame(step);
      if (document.hidden) {
        last = ts;
        return;
      }
      var dt = last ? Math.min(0.05, (ts - last) / 1000) : 0.016;
      last = ts;
      var frame = 60 * dt;

      // Smooth pointer influence on/off
      var targetStr = pointer.active ? 1 : 0;
      pointer.strength += (targetStr - pointer.strength) * Math.min(1, 6 * dt);

      ctx.clearRect(0, 0, w, h);

      var i;
      var p;
      var mx = pointer.x;
      var my = pointer.y;
      var pull = pointer.strength;

      for (i = 0; i < particles.length; i++) {
        p = particles[i];

        // Mild ambient drift
        p.vx += (Math.random() - 0.5) * 0.008 * frame;
        p.vy += (Math.random() - 0.5) * 0.008 * frame - 0.0012 * frame;

        if (pull > 0.01) {
          var dx = mx - p.x;
          var dy = my - p.y;
          var dist = Math.sqrt(dx * dx + dy * dy) || 0.0001;

          if (dist < ATTRACT_RADIUS) {
            var t = 1 - dist / ATTRACT_RADIUS;
            // Ease: stronger near edge of influence, settle near center
            var falloff = t * t;
            var inv = 1 / dist;
            var ax = dx * inv;
            var ay = dy * inv;

            // Attraction toward cursor
            var force =
              ATTRACT_STRENGTH * falloff * pull * (1.15 - 0.35 * p.mass);
            p.vx += ax * force * frame;
            p.vy += ay * force * frame;

            // Soft repel very close so particles orbit / swirl instead of stacking
            if (dist < REPEL_RADIUS) {
              var rt = 1 - dist / REPEL_RADIUS;
              var rf = REPEL_STRENGTH * rt * rt * pull;
              p.vx -= ax * rf * frame;
              p.vy -= ay * rf * frame;
            }

            // Tangential swirl for a “follow the mouse” feel
            var swirl = 0.032 * falloff * pull;
            p.vx += -ay * swirl * frame;
            p.vy += ax * swirl * frame;
          }
        }

        p.vx *= Math.pow(DRAG, frame);
        p.vy *= Math.pow(DRAG, frame);

        var sp = Math.sqrt(p.vx * p.vx + p.vy * p.vy);
        if (sp > MAX_SPEED) {
          p.vx = (p.vx / sp) * MAX_SPEED;
          p.vy = (p.vy / sp) * MAX_SPEED;
        }

        p.x += p.vx * frame;
        p.y += p.vy * frame;
        p.tw += p.tws * dt;

        if (p.x < -8) p.x = w + 8;
        else if (p.x > w + 8) p.x = -8;
        if (p.y < -8) p.y = h + 8;
        else if (p.y > h + 8) p.y = -8;
      }

      // Soft constellation links
      ctx.lineWidth = 1.25;
      for (i = 0; i < particles.length; i++) {
        var a = particles[i];
        for (var j = i + 1; j < particles.length; j++) {
          var b = particles[j];
          var ldx = a.x - b.x;
          var ldy = a.y - b.y;
          var ldist = Math.sqrt(ldx * ldx + ldy * ldy);
          if (ldist > LINK_DIST) continue;
          var lalpha = (1 - ldist / LINK_DIST) * 0.28;
          ctx.strokeStyle = "rgba(79, 70, 229," + lalpha + ")";
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }

      for (i = 0; i < particles.length; i++) {
        p = particles[i];
        var nearBoost = 1;
        if (pull > 0.02) {
          var ndx = p.x - mx;
          var ndy = p.y - my;
          var nd = Math.sqrt(ndx * ndx + ndy * ndy);
          if (nd < ATTRACT_RADIUS) {
            nearBoost = 1 + (1 - nd / ATTRACT_RADIUS) * 0.85 * pull;
          }
        }
        var pulse = 0.72 + 0.28 * Math.sin(p.tw);
        var alpha = Math.min(0.95, p.a * pulse * nearBoost);
        var r = p.r * (0.95 + 0.25 * pulse) * (0.95 + 0.25 * nearBoost);
        var glow = r * 4.2;
        var g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, glow);
        g.addColorStop(
          0,
          "rgba(" + p.rgb[0] + "," + p.rgb[1] + "," + p.rgb[2] + "," + alpha + ")"
        );
        g.addColorStop(
          0.35,
          "rgba(" +
            p.rgb[0] +
            "," +
            p.rgb[1] +
            "," +
            p.rgb[2] +
            "," +
            alpha * 0.55 +
            ")"
        );
        g.addColorStop(
          0.7,
          "rgba(" +
            p.rgb[0] +
            "," +
            p.rgb[1] +
            "," +
            p.rgb[2] +
            "," +
            alpha * 0.18 +
            ")"
        );
        g.addColorStop(1, "rgba(" + p.rgb[0] + "," + p.rgb[1] + "," + p.rgb[2] + ",0)");
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(p.x, p.y, glow, 0, Math.PI * 2);
        ctx.fill();

        // Brighter solid core so dots read clearly on light backgrounds
        ctx.fillStyle =
          "rgba(" +
          p.rgb[0] +
          "," +
          p.rgb[1] +
          "," +
          p.rgb[2] +
          "," +
          Math.min(1, alpha * 0.95) +
          ")";
        ctx.beginPath();
        ctx.arc(p.x, p.y, Math.max(1.2, r * 0.55), 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function start() {
      if (running || REDUCED) return;
      running = true;
      last = 0;
      raf = requestAnimationFrame(step);
    }

    var resizeTimer = 0;
    function onResize() {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        resize();
      }, 80);
    }

    window.addEventListener("resize", onResize, { passive: true });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) last = 0;
    });

    resize();
    if (REDUCED) {
      ctx.clearRect(0, 0, w, h);
      for (var i = 0; i < particles.length; i++) {
        var p = particles[i];
        ctx.fillStyle =
          "rgba(" + p.rgb[0] + "," + p.rgb[1] + "," + p.rgb[2] + "," + p.a * 0.7 + ")";
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      }
      return;
    }
    bindPointer();
    start();
  }

  function boot() {
    var hosts = document.querySelectorAll(".home-bg, .site-bg, .bg");
    if (!hosts.length) {
      var layer = document.createElement("div");
      layer.className = "site-bg";
      layer.setAttribute("aria-hidden", "true");
      document.body.insertBefore(layer, document.body.firstChild);
      hosts = [layer];
    } else if (document.querySelector(".home-bg")) {
      hosts = document.querySelectorAll(".home-bg");
    }
    for (var i = 0; i < hosts.length; i++) {
      mount(hosts[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
