// Import CSV into a table (Data tab). The file is read and checked in the browser: pick the delimiter, say whether the first row is
// a header, map file columns to table columns and look at the first rows as they will be stored. "Review import" then sends every
// row as an insert to the review popup (static/review.js), which saves them all in one transaction, records them in the audit log
// and can be undone from there (explorer/row_edits.py). At most MAX_ROWS rows per import. Values are shown as text, never as HTML.
(function () {
  if (!window.pgdasReview) return;
  var MAX_ROWS = 5000, MAX_BYTES = 5 * 1024 * 1024, PREVIEW = 5;
  var toastEl = document.getElementById("pgToast"), toastTimer = null;

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message; toastEl.className = "toast"; toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 6000);
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = attrs[key]; else if (key === "class") node.className = attrs[key]; else node.setAttribute(key, attrs[key]);
    });
    (children || []).forEach(function (child) { node.appendChild(child); });
    return node;
  }

  // RFC 4180: quoted fields may hold the delimiter, line breaks and "" for a quote.
  function parseCsv(text, delimiter) {
    var rows = [], row = [], field = "", quoted = false, i = 0;
    if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
    for (; i < text.length; i++) {
      var ch = text[i];
      if (quoted) {
        if (ch === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else quoted = false; } else field += ch;
      } else if (ch === '"' && field === "") quoted = true;
      else if (ch === delimiter) { row.push(field); field = ""; }
      else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && text[i + 1] === "\n") i++;
        row.push(field); field = ""; rows.push(row); row = [];
      } else field += ch;
    }
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    return rows.filter(function (r) { return !(r.length === 1 && r[0] === ""); });      // blank lines
  }

  function guessDelimiter(text) {
    var first = text.split(/\r?\n/, 1)[0], best = ",", most = -1;
    [",", ";", "\t", "|"].forEach(function (d) { var n = first.split(d).length - 1; if (n > most) { most = n; best = d; } });
    return best;
  }

  var open = null;       // {root, ctx, rows, columns}

  function close() { if (open) { open.root.remove(); open = null; } }

  function state() {
    var root = open.root, header = root.querySelector("[data-header]").checked;
    var rows = open.rows, head = header && rows.length ? rows[0] : null, data = header ? rows.slice(1) : rows;
    var width = rows.reduce(function (m, r) { return Math.max(m, r.length); }, 0);
    var names = []; for (var i = 0; i < width; i++) names.push(head && head[i] !== undefined && head[i] !== "" ? head[i] : "column " + (i + 1));
    return {head: head, data: data, names: names};
  }

  function rebuildMapping(keep) {
    var s = state(), box = open.root.querySelector("[data-map]");
    box.textContent = "";
    open.columns.forEach(function (column) {
      if (column.locked) return;
      var select = el("select", {"data-map-col": column.name}, [el("option", {value: "", text: "— not in the file (use the default) —"})]);
      s.names.forEach(function (name, index) { select.appendChild(el("option", {value: String(index), text: name})); });
      var chosen = keep && keep[column.name] !== undefined ? keep[column.name] : "";
      if (!keep) {
        var plain = function (text) { return text.toLowerCase().replace(/[^a-z0-9]/g, ""); };      // "E-mail", "e mail" and "email" are the same name
        var match = s.names.findIndex(function (n) { return plain(n) === plain(column.name); });
        if (match < 0 && !s.head && s.names.length === open.columns.filter(function (c) { return !c.locked; }).length) match = open.columns.filter(function (c) { return !c.locked; }).indexOf(column);
        chosen = match >= 0 ? String(match) : "";
      }
      select.value = chosen;
      box.appendChild(el("div", {"class": "dlg-row"}, [el("label", {text: column.name + " (" + column.type + ")"}), select]));
    });
  }

  function mapping() {
    var map = {};
    open.root.querySelectorAll("[data-map-col]").forEach(function (s) { if (s.value !== "") map[s.getAttribute("data-map-col")] = parseInt(s.value, 10); });
    return map;
  }

  function inserts() {
    var s = state(), map = mapping(), emptyNull = open.root.querySelector("[data-empty-null]").checked;
    return s.data.map(function (cells) {
      var row = {};
      Object.keys(map).forEach(function (name) {
        var value = cells[map[name]];
        if (value === undefined || value === "") value = emptyNull ? null : "";
        row[name] = value;
      });
      return row;
    });
  }

  function refresh() {
    var root = open.root, s = state(), line = root.querySelector("[data-problem]"), table = root.querySelector("[data-preview]");
    var problem = "";
    if (!open.rows.length) problem = "Choose a CSV file.";
    else if (!s.data.length) problem = "The file has no data rows.";
    else if (s.data.length > MAX_ROWS) problem = "The file has " + s.data.length.toLocaleString() + " rows; import at most " + MAX_ROWS.toLocaleString() + " at a time (split the file).";
    else if (!Object.keys(mapping()).length) problem = "Map at least one table column to a column of the file.";
    line.textContent = problem; line.hidden = !problem;
    root.querySelector("[data-dlg-save]").disabled = !!problem;
    root.querySelector("[data-summary]").textContent = open.rows.length ? s.data.length.toLocaleString() + " row" + (s.data.length === 1 ? "" : "s") + " in the file, " + s.names.length + " column" + (s.names.length === 1 ? "" : "s") + "." : "";
    table.textContent = "";
    if (problem && !s.data.length) return;
    var map = mapping(), names = Object.keys(map), head = el("tr");
    names.forEach(function (n) { head.appendChild(el("th", {text: n})); });
    table.appendChild(el("thead", {}, [head]));
    var body = el("tbody");
    inserts().slice(0, PREVIEW).forEach(function (row) {
      var tr = el("tr");
      names.forEach(function (n) { var td = el("td", {text: row[n] === null ? "NULL" : row[n]}); if (row[n] === null) td.className = "null"; tr.appendChild(td); });
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function load(file) {
    if (!file) return;
    var line = open.root.querySelector("[data-problem]");
    if (file.size > MAX_BYTES) { open.rows = []; line.textContent = "That file is larger than 5 MB. Split it into smaller files."; line.hidden = false; return; }
    var reader = new FileReader();
    reader.onload = function () {
      open.text = String(reader.result);
      var delimiter = guessDelimiter(open.text);
      open.root.querySelector("[data-delimiter]").value = delimiter === "\t" ? "tab" : delimiter;
      reparse(false);
    };
    reader.readAsText(file);
  }

  function reparse(keepMapping) {
    var chosen = open.root.querySelector("[data-delimiter]").value;
    open.rows = parseCsv(open.text || "", chosen === "tab" ? "\t" : chosen);
    var keep = null;
    if (keepMapping) { keep = {}; var current = mapping(); Object.keys(current).forEach(function (k) { keep[k] = current[k]; }); }
    rebuildMapping(keep);
    refresh();
  }

  function openDialog(ctx) {
    close();
    var delimiter = el("select", {"data-delimiter": ""});
    [[",", "comma ( , )"], [";", "semicolon ( ; )"], ["tab", "tab"], ["|", "pipe ( | )"]].forEach(function (d) { delimiter.appendChild(el("option", {value: d[0], text: d[1]})); });
    var header = el("input", {type: "checkbox", "data-header": ""}); header.checked = true;
    var emptyNull = el("input", {type: "checkbox", "data-empty-null": ""}); emptyNull.checked = true;
    var file = el("input", {type: "file", accept: ".csv,.tsv,.txt,text/csv", "data-file": ""});
    var root = el("div", {"class": "cyber-modal", "data-iodlg": ""}, [el("div", {"class": "cyber-dialog dlg tdlg", role: "dialog", "aria-modal": "true"}, [
      el("div", {"class": "modal-top"}, [el("span", {text: "IMPORT — CSV into " + ctx.schema + "." + ctx.table}), el("button", {type: "button", "data-dlg-close": "", "aria-label": "Close", text: "✕"})]),
      el("div", {"class": "dlg-row"}, [el("label", {text: "File"}), file]),
      el("div", {"class": "dlg-row"}, [el("label", {text: "Delimiter"}), delimiter]),
      el("label", {"class": "dlg-check"}, [header, document.createTextNode(" The first row holds the column names")]),
      el("label", {"class": "dlg-check"}, [emptyNull, document.createTextNode(" Empty cells become NULL (otherwise an empty text)")]),
      el("p", {"class": "dlg-grants-head"}, [el("strong", {text: "Which file column goes into which table column"})]),
      el("div", {"data-map": ""}),
      el("p", {"class": "dlg-hint", "data-summary": ""}),
      el("div", {"class": "scroll"}, [el("table", {"data-preview": ""})]),
      el("p", {"class": "dlg-hint", text: "Nothing is imported yet. “Review import” shows what will be added; all rows are saved together, recorded in the audit log, and can be undone from there."}),
      el("p", {"class": "dlg-problem", "data-problem": "", hidden: "hidden"}),
      el("div", {"class": "dlg-foot"}, [el("button", {type: "button", "class": "btn", "data-dlg-close": "", text: "Close"}),
        el("button", {type: "button", "class": "btn primary", "data-dlg-save": "", disabled: "disabled", text: "Review import…"})]),
    ])]);
    open = {root: root, ctx: ctx, rows: [], columns: ctx.columns, text: ""};
    document.body.appendChild(root);
    rebuildMapping(null);
    refresh();
    file.addEventListener("change", function () { load(file.files[0]); });
    delimiter.addEventListener("change", function () { reparse(false); });
    header.addEventListener("change", function () { rebuildMapping(null); refresh(); });
    emptyNull.addEventListener("change", refresh);
    root.addEventListener("change", function (event) { if (event.target.hasAttribute("data-map-col")) refresh(); });
    root.addEventListener("click", function (event) {
      if (event.target === root || event.target.closest("[data-dlg-close]")) { close(); return; }
      if (event.target.closest("[data-dlg-save]")) save();
    });
  }

  function save() {
    var current = open, rows = inserts(), button = current.root.querySelector("[data-dlg-save]"), line = current.root.querySelector("[data-problem]");
    button.disabled = true;
    window.pgdasReview.start({
      server: current.ctx.server, db: current.ctx.db,
      rows: {schema: current.ctx.schema, table: current.ctx.table, edits: [], inserts: rows, deletes: []},
      onError: function (text) { line.textContent = text; line.hidden = false; },
      onSuccess: function (result) {
        close();
        toast(result.message || "Imported.");
        document.dispatchEvent(new CustomEvent("pgdas:changed", {detail: {server: current.ctx.server, redraw: true}}));
      },
    }).then(function () { if (open === current) refresh(); });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-import-csv]");
    if (!button) return;
    var grid = button.closest("[data-grid]"), holder = button.closest("[data-structure]");
    if (!grid || !holder) return;
    var meta = JSON.parse(grid.getAttribute("data-grid")), where = JSON.parse(holder.getAttribute("data-structure"));
    openDialog({server: where.server, db: where.db, schema: meta.schema, table: meta.table, columns: meta.columns});
  });
  document.addEventListener("keydown", function (event) {
    var review = document.getElementById("changeModal");
    if (event.key === "Escape" && open && (!review || review.hidden)) close();
  });
})();
