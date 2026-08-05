/**
 * Shared page-level UX for simple tool pages.
 *
 * Provides small helpers that are re-implemented in most tool templates:
 * status display, copy-to-clipboard, keyboard shortcuts, meta rendering,
 * and HTTP error parsing.
 *
 * Usage:
 *   const { setStatus, clearStatus, setupCopy, setupShortcut, renderMeta, parseError } = window.ToolPage;
 */
(function (global) {
  "use strict";

  function setStatus(el, kind, text) {
    if (!el) return;
    if (!text) {
      el.className = "status";
      el.textContent = "";
      return;
    }
    el.className = "status show " + (kind || "info");
    el.textContent = text;
  }

  function clearStatus(el) {
    setStatus(el, "", "");
  }

  function copyText(text, okMsg) {
    var t = text == null ? "" : String(text);
    if (!t) return Promise.resolve(false);
    var done = function () {
      if (global.ToolkitUX && typeof global.ToolkitUX.toast === "function") {
        global.ToolkitUX.toast(okMsg || "已复制到剪贴板", "ok");
      }
      return true;
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(t).then(done).catch(function () {
        return legacyCopy(t) ? done() : Promise.resolve(false);
      });
    }
    return Promise.resolve(legacyCopy(t) ? done() : false);
  }

  function legacyCopy(t) {
    try {
      var ta = document.createElement("textarea");
      ta.value = t;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return !!ok;
    } catch (e) {
      return false;
    }
  }

  function setupCopy(btnEl, getText, okMsg) {
    btnEl.addEventListener("click", function () {
      copyText(getText(), okMsg);
    });
  }

  function setupShortcut(inputEl, actionFn) {
    inputEl.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        actionFn();
      }
    });
  }

  function renderMeta(metaEl, data, fieldMap) {
    if (!metaEl || !data) return;
    var parts = [];
    for (var i = 0; i < fieldMap.length; i++) {
      var f = fieldMap[i];
      var val = f.value != null ? f.value : data[f.key];
      if (val != null && val !== "") {
        parts.push("<span><strong>" + f.label + "</strong> " + val + "</span>");
      }
    }
    metaEl.innerHTML = parts.join("");
  }

  function parseError(resp) {
    if (!resp) return "请求失败";
    var data = null;
    try { data = resp.json(); } catch (e) {}
    var detail = data && data.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(function (d) { return d.msg || d; }).join("; ");
    if (detail && typeof detail === "object") return detail.message || JSON.stringify(detail);
    if (data && data.message) return data.message;
    return "HTTP " + resp.status;
  }

  function appUrl(path) {
    var root = global.__ROOT__ || "";
    return root + path;
  }

  async function runForm(url, fd, opts) {
    opts = opts || {};
    var res = await fetch(url, { method: opts.method || "POST", body: fd });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(parseError(res));
    return data;
  }

  global.ToolPage = {
    setStatus: setStatus,
    clearStatus: clearStatus,
    copyText: copyText,
    setupCopy: setupCopy,
    setupShortcut: setupShortcut,
    renderMeta: renderMeta,
    parseError: parseError,
    appUrl: appUrl,
    runForm: runForm,
  };

})(typeof window !== "undefined" ? window : this);
