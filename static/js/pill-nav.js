/* ============================================================
   Pill-nav chrome — shared interactions for homepage + tool pages.
   Homepage-only behaviour (grid filter, hero canvas, CTA build) stays
   inline in index.html; this file only wires the navigation chrome.
   ============================================================ */
(function () {
  "use strict";

  function init() {
    /* —— Mobile bottom-sheet menu —— */
    var toggle = document.getElementById("pn-mobile-toggle");
    var sheet = document.getElementById("mobile-sheet");
    var backdrop = document.getElementById("ms-backdrop");
    if (toggle && sheet) {
      function closeSheet() {
        sheet.classList.remove("open");
        sheet.setAttribute("aria-hidden", "true");
        toggle.setAttribute("aria-expanded", "false");
        if (backdrop) backdrop.classList.remove("show");
      }
      function openSheet() {
        sheet.classList.add("open");
        sheet.setAttribute("aria-hidden", "false");
        toggle.setAttribute("aria-expanded", "true");
        if (backdrop) backdrop.classList.add("show");
      }
      toggle.addEventListener("click", function () {
        if (sheet.classList.contains("open")) closeSheet();
        else openSheet();
      });
      if (backdrop) backdrop.addEventListener("click", closeSheet);
      var msClose = document.getElementById("ms-close");
      if (msClose) msClose.addEventListener("click", closeSheet);
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && sheet.classList.contains("open")) closeSheet();
      });
      /* Click an in-sheet category → jump to its href (homepage filter via hash) */
      sheet.querySelectorAll(".ms-item[data-ms]").forEach(function (a) {
        a.addEventListener("click", function () { closeSheet(); });
      });
    }

    /* —— Theme toggle —— */
    var th = document.getElementById("homeThemeToggle");
    if (th) {
      function syncThemeIcon() {
        var el = document.querySelector(".home-theme-icon");
        if (el) el.textContent = document.documentElement.classList.contains("light-mode") ? "☀" : "☾";
      }
      th.addEventListener("click", function () {
        var light = document.documentElement.classList.toggle("light-mode");
        try { localStorage.setItem("toolkit_theme_v1", light ? "light" : "dark"); } catch (e) {}
        document.documentElement.setAttribute("data-theme", light ? "light" : "dark");
        syncThemeIcon();
      });
      syncThemeIcon();
    }

    /* —— Pill category buttons: navigate to data-href, move indicator ——
       On the homepage (has #tools-grid) navigation is handled inline
       (setFilter), so we only attach the indicator here. */
    var isHome = !!document.getElementById("tools-grid");
    var pnCats = Array.prototype.slice.call(document.querySelectorAll(".pn-cat[data-pn]"));
    var pnIndicator = document.getElementById("pn-indicator");
    function moveIndicator(btn) {
      if (!pnIndicator || !btn) return;
      var cats = btn.parentElement;
      if (!cats) return;
      var cr = cats.getBoundingClientRect();
      var br = btn.getBoundingClientRect();
      pnIndicator.classList.add("show");
      pnIndicator.style.width = br.width + "px";
      pnIndicator.style.left = (br.left - cr.left) + "px";
    }
    if (!isHome) {
      pnCats.forEach(function (c) {
        c.addEventListener("click", function () {
          var href = c.getAttribute("data-href");
          if (href && href !== "#") window.location.href = href;
        });
      });
    }
    /* Position the indicator on the active button once laid out.
       Recompute after full load + fonts so it wraps icon and text together. */
    var act = document.querySelector(".pn-cat.active");
    if (act) {
      requestAnimationFrame(function () { moveIndicator(act); });
      window.addEventListener("load", function () {
        var cur = document.querySelector(".pn-cat.active");
        if (cur) moveIndicator(cur);
      });
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(function () {
          var cur = document.querySelector(".pn-cat.active");
          if (cur) moveIndicator(cur);
        });
      }
      setTimeout(function () {
        var cur = document.querySelector(".pn-cat.active");
        if (cur) moveIndicator(cur);
      }, 300);
      window.addEventListener("resize", function () {
        var cur = document.querySelector(".pn-cat.active");
        if (cur) moveIndicator(cur);
      });
    }

    /* —— Command palette shortcut (Ctrl/Cmd+K) —— */
    var tux = window.ToolkitUX;
    if (tux && tux.openPalette) {
      document.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
          e.preventDefault();
          tux.openPalette();
        }
      });
    }

    /* —— Colored category icons ———
       The pill nav uses the colored emoji rendered by the template
       ({{ item.icon }}, e.g. 📕). Nothing to convert; keep them as-is. */
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();