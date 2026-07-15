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
   *   onProgress?: (pct:number, phase:'upload'|'done') => void,
   *   onStatus?: (text:string, cls?:string) => void
   * }} [opts]
   * @returns {Promise<XMLHttpRequest>}
   */
  function xhrPost(url, formData, opts) {
    opts = opts || {};
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.responseType = "blob";
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
        if (opts.onStatus) opts.onStatus("上传完成，正在处理…", "info");
      };
      xhr.onload = function () {
        if (opts.onProgress) opts.onProgress(100, "done");
        resolve(xhr);
      };
      xhr.onerror = function () {
        reject(new Error("网络错误"));
      };
      xhr.onabort = function () {
        reject(new Error("已取消"));
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
