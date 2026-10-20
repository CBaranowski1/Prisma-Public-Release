// Brain ingestion drop zone: drag/drop or pick .md files -> POST multipart to
// /api/brain/ingest -> status line. New notes appear in the graph on next open.
(function () {
  "use strict";
  function wire() {
    var zone = document.getElementById("brain-ingest-drop");
    var input = document.getElementById("brain-ingest-input");
    var status = document.getElementById("brain-ingest-status");
    if (!zone || !input) return;
    function setStatus(t) { if (status) status.textContent = t; }

    async function send(fileList) {
      var files = Array.prototype.filter.call(fileList || [], function (f) {
        return /\.md$/i.test(f.name);
      });
      if (!files.length) { setStatus("No .md files."); return; }
      var fd = new FormData();
      files.forEach(function (f) { fd.append("files", f, f.name); });
      setStatus("Uploading " + files.length + " file(s)…");
      try {
        var r = await fetch("/api/brain/ingest", { method: "POST", body: fd, credentials: "same-origin" });
        var j = await r.json();
        if (j.error) { setStatus("Error: " + j.error); return; }
        var created = 0, renamed = 0;
        (j.results || []).forEach(function (x) {
          if (x.status === "created") created++; else if (x.status === "renamed") renamed++;
        });
        setStatus(created + " added" + (renamed ? ", " + renamed + " renamed" : "") + ". Reopen the graph to see them.");
      } catch (e) { setStatus("Upload failed."); }
    }

    zone.addEventListener("click", function () { input.click(); });
    input.addEventListener("change", function () { send(input.files); input.value = ""; });
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.style.opacity = "1"; });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.style.opacity = "0.8"; });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files) send(e.dataTransfer.files);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
