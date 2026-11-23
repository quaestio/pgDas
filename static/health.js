// Table & index health: tiles, ranked findings (with the SQL that fixes each), and sortable tables of tables and indexes. Everything from the server is shown as text.
(function () {
  "use strict";
  var body = document.getElementById("hBody");
  if (!body) { return; }
  var serverSel = document.getElementById("hServer"), dbSel = document.getElementById("hDatabase"), limitSel = document.getElementById("hLimit"), status = document.getElementById("hStatus"), tilesEl = document.getElementById("hTiles");
  var current = null, sortState = {tables: ["total", -1], indexes: ["size", -1]};

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function bytes(v) {
    if (v === null || v === undefined) { return "–"; }
    var u = ["B", "kB", "MB", "GB", "TB"], i = 0, n = Math.abs(v);
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
    return (n >= 100 || i === 0 ? Math.round(n) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + " " + u[i];
  }
  function num(v) { return v === null || v === undefined ? "–" : Number(v).toLocaleString(); }
  function ago(iso) {
    if (!iso) { return "never"; }
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) { return "just now"; } if (s < 5400) { return Math.round(s / 60) + " min ago"; } if (s < 172800) { return Math.round(s / 3600) + " h ago"; }
    return Math.round(s / 86400) + " d ago";
  }
  function percent(a, b) { return b ? a * 100 / b : null; }
  function pct(v) { return v === null || v === undefined ? "–" : (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " %"; }
  function csrf() { return (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ""; }

  function tile(k, v, sub, level) {
    var t = el("div", {"class": "dash-tile" + (level ? " " + level : "")});
    t.appendChild(el("div", {"class": "k"}, k)); t.appendChild(el("div", {"class": "v"}, v)); if (sub) { t.appendChild(el("div", {"class": "s"}, sub)); }
    return t;
  }
  function renderTiles(d) {
    var s = d.summary; tilesEl.textContent = "";
    tilesEl.appendChild(tile("Findings", String(s.critical + s.warnings + s.information), s.critical + " critical, " + s.warnings + " warnings, " + s.information + " notes", s.critical ? "bad" : s.warnings ? "warn" : "good"));
    tilesEl.appendChild(tile("Tables", String(s.tables), bytes(s.table_bytes) + " in the biggest " + d.tables.length));
    tilesEl.appendChild(tile("Dead rows", num(s.dead_rows), s.needing_vacuum + " table" + (s.needing_vacuum === 1 ? "" : "s") + " need vacuum", s.needing_vacuum ? "warn" : "good"));
    tilesEl.appendChild(tile("Stale statistics", String(s.stale_statistics), "tables to ANALYZE", s.stale_statistics ? "warn" : "good"));
    tilesEl.appendChild(tile("Unused indexes", String(s.unused_indexes), bytes(s.unused_bytes) + " wasted", s.unused_indexes ? "warn" : "good"));
    tilesEl.appendChild(tile("Duplicate indexes", String(s.duplicate_indexes), bytes(s.duplicate_bytes) + " wasted", s.duplicate_indexes ? "warn" : "good"));
    tilesEl.appendChild(tile("Invalid indexes", String(s.invalid_indexes), "failed to build", s.invalid_indexes ? "bad" : "good"));
    tilesEl.appendChild(tile("Near forced vacuum", String(s.near_freeze), "tables past half the limit", s.near_freeze ? "bad" : "good"));
    status.textContent = "";
    var parts = ["Read just now", "statistics " + (d.stats_age_days === null ? "never reset" : "since " + d.stats_age_days + " days ago"), "PostgreSQL " + Math.floor(d.version / 10000)];
    status.appendChild(document.createTextNode(parts.join("   ")));
    if (d.truncated) { status.appendChild(el("span", {"class": "bad"}, "   Showing only the biggest " + d.tables.length + " tables and " + d.indexes.length + " indexes of " + d.table_count + " and " + d.index_count + ".")); }
    (d.notes || []).forEach(function (n) { status.appendChild(el("span", {"class": "bad"}, "   Not read: " + n)); });
  }

  // ---- findings -----------------------------------------------------------------------------------------------------------------------
  var OPERATION = {dead_rows: "vacuum_analyze", never_vacuumed: "vacuum_analyze", stale_statistics: "analyze", freeze_age: "vacuum"};
  function finding(f, d) {
    var box = el("div", {"class": "h-finding " + f.level});
    var head = el("div", {"class": "h-head"});
    head.appendChild(el("span", {"class": "h-badge " + f.level}, f.level === "info" ? "note" : f.level));
    head.appendChild(el("strong", {}, f.object)); head.appendChild(document.createTextNode(" — " + f.title));
    box.appendChild(head); box.appendChild(el("p", {"class": "muted"}, f.detail));
    if (f.sql) {
      var row = el("div", {"class": "h-sql"}); row.appendChild(el("code", {}, f.sql));
      var copy = el("button", {type: "button", "class": "btn"}, "Copy"); copy.addEventListener("click", function () { if (navigator.clipboard) { navigator.clipboard.writeText(f.sql); } copy.textContent = "Copied"; });
      row.appendChild(copy);
      if (f.table && OPERATION[f.kind]) {
        var run = el("button", {type: "button", "class": "btn primary"}, "Run it now");
        run.addEventListener("click", function () { maintain(run, f, d); }); row.appendChild(run);
      }
      box.appendChild(row);
    }
    return box;
  }
  function maintain(button, f, d) {
    var operation = OPERATION[f.kind];
    if (!window.confirm("Run " + f.sql.replace(/;$/, "") + " now? It runs on the live database and can take a while on a big table.")) { return; }
    button.disabled = true; button.textContent = "Running…";
    var url = body.getAttribute("data-maintenance-url-template").replace("__SID__", encodeURIComponent(d.server.id));
    fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf(), "X-Requested-With": "fetch"},
                body: JSON.stringify({db: d.database, schema: f.table[0], table: f.table[1], operation: operation})})
      .then(function (r) { return r.json(); }).then(function (a) { button.textContent = a.ok ? "Done" : (a.error || "Not done"); })
      .catch(function () { button.textContent = "Did not complete"; }).then(function () { button.disabled = false; });
  }
  function renderFindings(d) {
    var section = el("div", {});
    section.appendChild(el("div", {"class": "dash-section"}, "Findings"));
    if (!d.findings.length) { section.appendChild(el("div", {"class": "dash-panel"}, "Nothing to report: no table needs vacuuming, no index is unused, duplicated or broken, and no table is near a forced vacuum.")); }
    d.findings.slice(0, 60).forEach(function (f) { section.appendChild(finding(f, d)); });
    if (d.findings.length > 60) { section.appendChild(el("p", {"class": "muted"}, (d.findings.length - 60) + " more findings are not shown: fix the ones above first.")); }
    return section;
  }

  // ---- sortable tables -----------------------------------------------------------------------------------------------------------------
  function sorted(rows, key, direction, value) {
    return rows.slice().sort(function (a, b) {
      var x = value(a, key), y = value(b, key);
      if (x === null || x === undefined) { return 1; } if (y === null || y === undefined) { return -1; }
      return (x > y ? 1 : x < y ? -1 : 0) * direction;
    });
  }
  function table(title, id, rows, columns, d) {
    var box = el("div", {"class": "dash-panel dash-wide"});
    box.appendChild(el("h3", {}, title));
    var tbl = el("table", {"class": "dash-table"}), thead = el("thead"), hr = el("tr");
    columns.forEach(function (c) {
      var th = el("th", {title: "Sort", style: "cursor:pointer"}, c.label + (sortState[id][0] === c.key ? (sortState[id][1] < 0 ? " ▼" : " ▲") : ""));
      th.addEventListener("click", function () { sortState[id] = [c.key, sortState[id][0] === c.key ? -sortState[id][1] : -1]; render(current); });
      hr.appendChild(th);
    });
    thead.appendChild(hr); tbl.appendChild(thead);
    var tb = el("tbody"), order = columns.filter(function (c) { return c.key === sortState[id][0]; })[0] || columns[0];
    sorted(rows, order.key, sortState[id][1], order.value).forEach(function (r) {
      var tr = el("tr");
      columns.forEach(function (c) { var td = el("td", {"class": c.numeric ? "n" : ""}); td.textContent = c.text(r); if (c.title) { td.title = c.title(r); } tr.appendChild(td); });
      if (id === "tables" && d.pgstattuple) {
        var td = el("td"); var b = el("button", {type: "button", "class": "btn"}, "Measure"); b.title = "Exact dead space (pgstattuple)";
        b.addEventListener("click", function () { measure(b, r, d); }); td.appendChild(b); tr.appendChild(td);
      }
      tb.appendChild(tr);
    });
    if (id === "tables" && d.pgstattuple) { hr.appendChild(el("th", {}, "")); }
    tbl.appendChild(tb); box.appendChild(tbl);
    return box;
  }
  function measure(button, r, d) {
    button.disabled = true; button.textContent = "Reading…";
    fetch(body.getAttribute("data-measure-url"), {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf(), "X-Requested-With": "fetch"},
                                                    body: JSON.stringify({server: d.server.id, db: d.database, schema: r.schema, table: r.table})})
      .then(function (x) { return x.json(); }).then(function (a) {
        button.textContent = a.ok ? pct(a.dead_percent) + " dead, " + pct(a.free_percent) + " free, " + bytes(a.reclaimable) + " reclaimable" : (a.error || "Not measured");
      }).catch(function () { button.textContent = "Did not complete"; }).then(function () { button.disabled = false; });
  }
  function tableColumns() {
    return [
      {key: "name", label: "Table", value: function (r) { return r.schema + "." + r.table; }, text: function (r) { return r.schema + "." + r.table; }},
      {key: "total", label: "Size", numeric: true, value: function (r) { return r.total; }, text: function (r) { return bytes(r.total); }, title: function (r) { return "table " + bytes(r.heap) + ", indexes " + bytes(r.indexes) + ", toast " + bytes(r.toast); }},
      {key: "live", label: "Rows", numeric: true, value: function (r) { return r.live; }, text: function (r) { return num(r.live); }},
      {key: "dead", label: "Dead", numeric: true, value: function (r) { return percent(r.dead, r.live + r.dead); }, text: function (r) { return num(r.dead) + " (" + pct(percent(r.dead, r.live + r.dead)) + ")"; }},
      {key: "vacuum", label: "Vacuumed", value: function (r) { return r.last_autovacuum || r.last_vacuum || ""; }, text: function (r) { var a = r.last_autovacuum, b = r.last_vacuum; return ago(a && b ? (a > b ? a : b) : (a || b)); }},
      {key: "analyze", label: "Analyzed", value: function (r) { return r.last_autoanalyze || r.last_analyze || ""; }, text: function (r) { var a = r.last_autoanalyze, b = r.last_analyze; return ago(a && b ? (a > b ? a : b) : (a || b)); }},
      {key: "seq", label: "Full scans", numeric: true, value: function (r) { return r.seq_scan; }, text: function (r) { return num(r.seq_scan); }},
      {key: "idx", label: "Index use", numeric: true, value: function (r) { return percent(r.idx_scan, r.idx_scan + r.seq_scan); }, text: function (r) { return pct(percent(r.idx_scan, r.idx_scan + r.seq_scan)); }},
      {key: "upd", label: "Updates", numeric: true, value: function (r) { return r.updated; }, text: function (r) { return num(r.updated); }},
      {key: "hot", label: "Cheap (HOT)", numeric: true, value: function (r) { return percent(r.hot_updated, r.updated); }, text: function (r) { return pct(percent(r.hot_updated, r.updated)); }},
      {key: "xid", label: "Forced-vacuum age", numeric: true, value: function (r) { return r.xid_age; }, text: function (r) { return pct(percent(r.xid_age, current.freeze_max_age)); }},
      {key: "cache", label: "In memory", numeric: true, value: function (r) { return percent(r.heap_hit, r.heap_hit + r.heap_read); }, text: function (r) { return pct(percent(r.heap_hit, r.heap_hit + r.heap_read)); }}
    ];
  }
  function indexColumns() {
    return [
      {key: "index", label: "Index", value: function (r) { return r.schema + "." + r.index; }, text: function (r) { return r.schema + "." + r.index; }, title: function (r) { return r.definition; }},
      {key: "table", label: "On table", value: function (r) { return r.table; }, text: function (r) { return r.table; }},
      {key: "size", label: "Size", numeric: true, value: function (r) { return r.size; }, text: function (r) { return bytes(r.size); }},
      {key: "scans", label: "Used", numeric: true, value: function (r) { return r.scans; }, text: function (r) { return num(r.scans) + " times"; }},
      {key: "last", label: "Last used", value: function (r) { return r.last_scan || ""; }, text: function (r) { return r.last_scan ? ago(r.last_scan) : "(not recorded)"; }},
      {key: "kind", label: "Kind", value: function (r) { return r.primary ? 0 : r.unique ? 1 : 2; }, text: function (r) { return (r.primary ? "primary key" : r.unique ? "unique" : "index") + (r.valid ? "" : ", INVALID") + (r.constraints && !r.primary && !r.unique ? ", used by a constraint" : ""); }}
    ];
  }
  function running(d) {
    if (!d.running_vacuums.length) { return null; }
    var box = el("div", {"class": "dash-panel"}); box.appendChild(el("h3", {}, "Vacuums running now"));
    d.running_vacuums.forEach(function (r) { box.appendChild(el("div", {}, r.table + ": " + r.phase + (r.heap_total ? " (" + Math.round(r.heap_scanned * 100 / r.heap_total) + "% of the table scanned)" : ""))); });
    return box;
  }

  function render(d) {
    current = d;
    renderTiles(d);
    body.textContent = "";
    body.appendChild(renderFindings(d));
    var r = running(d); if (r) { body.appendChild(r); }
    body.appendChild(el("div", {"class": "dash-section"}, "Tables"));
    body.appendChild(table("Tables, biggest first (click a heading to sort)", "tables", d.tables, tableColumns(), d));
    body.appendChild(el("div", {"class": "dash-section"}, "Indexes"));
    body.appendChild(table("Indexes, biggest first (hover an index for its definition)", "indexes", d.indexes, indexColumns(), d));
    if (!d.pgstattuple) { body.appendChild(el("p", {"class": "muted"}, "Dead rows here are PostgreSQL's own estimates. Install the pgstattuple extension (CREATE EXTENSION pgstattuple) to get an exact measure of a table with the Measure button.")); }
  }

  function load(fresh) {
    if (!dbSel.value) { return; }
    status.textContent = "Reading the database…";
    var url = body.getAttribute("data-url") + "?server=" + encodeURIComponent(serverSel.value) + "&db=" + encodeURIComponent(dbSel.value) + "&limit=" + limitSel.value + (fresh ? "&fresh=1" : "");
    fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}}).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) { status.textContent = d.error || "Could not read it."; body.textContent = ""; tilesEl.textContent = ""; return; }
      render(d);
    }).catch(function () { status.textContent = "Could not read the database. Try again."; });
  }
  function loadDatabases() {
    dbSel.textContent = ""; dbSel.appendChild(el("option", {value: ""}, "Loading…")); body.textContent = ""; tilesEl.textContent = "";
    fetch(body.getAttribute("data-dbs-url") + "?server=" + encodeURIComponent(serverSel.value), {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}}).then(function (r) { return r.json(); }).then(function (a) {
      dbSel.textContent = "";
      if (!a.ok) { dbSel.appendChild(el("option", {value: ""}, "(unavailable)")); status.textContent = a.error || "Could not list the databases."; return; }
      a.databases.forEach(function (n) { dbSel.appendChild(el("option", {value: n}, n)); });
      status.textContent = a.databases.length ? "" : "No databases are visible to the saved login.";
      load(false);
    }).catch(function () { status.textContent = "Could not list the databases."; });
  }
  serverSel.addEventListener("change", loadDatabases);
  dbSel.addEventListener("change", function () { load(false); }); limitSel.addEventListener("change", function () { load(false); });
  document.getElementById("hRefresh").addEventListener("click", function () { load(true); });
  loadDatabases();
})();