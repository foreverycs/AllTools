/**
 * Shared helpers for tool pages that upload files and download results.
 * Attach via: <script src="{{ static_url('/static/js/upload.js') }}"></script>
 */
(function (global) {
  "use strict";

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function downloadName(disposition, fallback) {
    if (!disposition) return fallback;
    var star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(disposition);
    if (star) {
      try {
        return decodeURIComponent(star[1].replace(/\+/g, " "));
      } catch (e) {
        /* fall through */
      }
    }
    var ascii = /filename="?([^";]+)"?/i.exec(disposition);
    if (ascii) return ascii[1];
    return fallback;
  }

  function errDetail(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map(function (d) {
        return d.msg || JSON.stringify(d);
      }).join("; ");
    }
    return "请求失败";
  }

  /**
   * POST FormData with upload progress. Resolves with the XHR (responseType blob).
   *
   * @param {string} url
   * @param {FormData} formData
   * @param {{
   *   onProgress?: (pct:number, phase:'upload'|'done'|'process') => void,
   *   onStatus?: (text:string, cls?:string) => void,
   *   processHint?: string,
   *   longWaitSec?: number
   * }} [opts]
   * @returns {Promise<XMLHttpRequest>}
   */
  function xhrPost(url, formData, opts) {
    opts = opts || {};
    var processHint =
      opts.processHint ||
      "上传完成，正在转换… 大文件 / OCR 可能需要数分钟，请勿关闭页面";
    var longWaitSec = typeof opts.longWaitSec === "number" ? opts.longWaitSec : 20;
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      var processTimer = null;
      var tickTimer = null;
      var processStarted = 0;

      function clearTimers() {
        if (processTimer) {
          clearTimeout(processTimer);
          processTimer = null;
        }
        if (tickTimer) {
          clearInterval(tickTimer);
          tickTimer = null;
        }
      }

      function startProcessHints() {
        processStarted = Date.now();
        if (opts.onStatus) opts.onStatus(processHint, "info");
        if (opts.onProgress) opts.onProgress(78, "process");
        // Periodic “still working” hints so long conversions feel alive.
        tickTimer = setInterval(function () {
          var sec = Math.round((Date.now() - processStarted) / 1000);
          if (opts.onProgress) {
            // Creep slowly toward 95% while waiting (never claim 100% early).
            var pct = Math.min(95, 78 + Math.log10(1 + sec) * 8);
            opts.onProgress(pct, "process");
          }
          if (opts.onStatus && sec >= longWaitSec) {
            opts.onStatus(
              "仍在处理（已 " +
                sec +
                " 秒）… 复杂表格 / 扫描件 OCR 较慢，请继续等待",
              "info"
            );
          }
        }, 5000);
      }

      xhr.open("POST", url);
      xhr.responseType = "blob";
      // Client-side safety net; reverse proxies should still set ≥600s.
      xhr.timeout = typeof opts.timeoutMs === "number" ? opts.timeoutMs : 0;
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && opts.onProgress) {
          opts.onProgress((e.loaded / e.total) * 70, "upload");
        }
        if (opts.onStatus && e.lengthComputable) {
          opts.onStatus(
            "上传中… " + Math.round((e.loaded / e.total) * 100) + "%",
            "info"
          );
        }
      };
      xhr.upload.onload = function () {
        if (opts.onProgress) opts.onProgress(75, "upload");
        startProcessHints();
      };
      xhr.onload = function () {
        clearTimers();
        if (opts.onProgress) opts.onProgress(100, "done");
        resolve(xhr);
      };
      xhr.onerror = function () {
        clearTimers();
        reject(new Error("网络错误"));
      };
      xhr.onabort = function () {
        clearTimers();
        reject(new Error("已取消"));
      };
      xhr.ontimeout = function () {
        clearTimers();
        reject(
          new Error(
            "请求超时。请检查反代 proxy_read_timeout（建议 ≥600s），或缩小页数 / 关闭 OCR 后重试"
          )
        );
      };
      xhr.send(formData);
    });
  }

  /**
   * Wire drag-and-drop + file input onto a drop zone.
   *
   * @param {{
   *   drop: HTMLElement,
   *   input: HTMLInputElement,
   *   onFiles: (FileList|File[]) => void,
   *   enabled?: () => boolean
   * }} cfg
   */
  function bindDropZone(cfg) {
    var drop = cfg.drop;
    var input = cfg.input;
    var onFiles = cfg.onFiles;
    var enabled = cfg.enabled || function () {
      return true;
    };

    input.addEventListener("change", function (e) {
      onFiles(e.target.files);
    });

    ["dragover", "dragenter"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        if (enabled()) drop.classList.add("drag");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        drop.classList.remove("drag");
      });
    });
    drop.addEventListener("drop", function (e) {
      if (!enabled()) return;
      onFiles(e.dataTransfer.files);
    });
  }

  /**
   * Trigger a browser download for a Blob.
   */
  function saveBlob(blob, filename) {
    var a = document.createElement("a");
    var href = URL.createObjectURL(blob);
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () {
      URL.revokeObjectURL(href);
    }, 2000);
  }

  /**
   * Parse error message from a failed XHR with blob response.
   * @returns {Promise<string>}
   */
  async function xhrErrorMessage(xhr) {
    var msg = "HTTP " + xhr.status;
    try {
      var text = await xhr.response.text();
      var err = JSON.parse(text);
      msg = errDetail(err.detail) || msg;
    } catch (e) {
      /* keep msg */
    }
    return msg;
  }

  /** Join app root path with an absolute path. */
  function appUrl(path) {
    var root = global.__ROOT__ || "";
    if (!path) return root || "/";
    if (path.charAt(0) !== "/") path = "/" + path;
    return root ? root + path : path;
  }

  global.ToolkitUpload = {
    fmtSize: fmtSize,
    downloadName: downloadName,
    errDetail: errDetail,
    xhrPost: xhrPost,
    bindDropZone: bindDropZone,
    saveBlob: saveBlob,
    xhrErrorMessage: xhrErrorMessage,
    appUrl: appUrl,
  };
})(typeof window !== "undefined" ? window : globalThis);
