// Dashboards: tiles, tables and charts (drawn here as SVG: no library), from /watch/dashboards/data/. Everything from the server is put in the page as text.
(function () {
  "use strict";
  var body = document.getElementById("dashBody");
  if (!body) { return; }
  var serverSel = document.getElementById("dashServer"), dbSel = document.getElementById("dashDatabase"), rangeSel = document.getElementById("dashRange");
  var auto = document.getElementById("dashAuto"), status = document.getElementById("dashStatus"), tilesEl = document.getElementById("dashTiles");
  var tip = document.getElementById("dashTip"), timer = null, current = null, hidden = {};
  var PALETTE = ["#5ec8ff", "#4cc38a", "#ffd166", "#ff6b6b", "#9d8cff", "#ff9f6b", "#6bf0e0", "#e86bd0"];
  var SECTIONS = [
    ["Workload", ["tps", "qps", "query_time", "rows", "cache"]],
    ["Sessions and locks", ["connections", "connection_use", "waiting", "ages", "states_time", "session_events"]],
    ["Storage, WAL and disk", ["size", "wal", "wal_dir", "checkpoints", "buffers", "io_wait", "io", "temp"]],
    ["Trouble signs", ["trouble", "xid", "autovacuum"]],
    ["Replication", ["lag_bytes", "lag_seconds", "slots"]]
  ];

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function svg(tag, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  // ---- number formats ---------------------------------------------------------------------------------------------------------------
  function scaled(value, units, step) {
    var i = 0, v = Math.abs(value);
    while (v >= step && i < units.length - 1) { v /= step; i += 1; }
    return (value < 0 ? "-" : "") + (v >= 100 || i === 0 && v === Math.round(v) ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + " " + units[i];
  }
  function fmt(unit, v) {
    if (v === null || v === undefined || isNaN(v)) { return "–"; }
    switch (unit) {
      case "bytes": return scaled(v, ["B", "kB", "MB", "GB", "TB"], 1024);
      case "bytes_s": return scaled(v, ["B/s", "kB/s", "MB/s", "GB/s"], 1024);
      case "percent": return (v >= 10 ? v.toFixed(1) : v.toFixed(2)) + " %";
      case "ms": return v >= 1000 ? (v / 1000).toFixed(2) + " s" : v.toFixed(v >= 10 ? 0 : 1) + " ms";
      case "ms_s": return v.toFixed(v >= 10 ? 0 : 1) + " ms/s";
      case "s": return seconds(v);
      case "per_s": return (v >= 1000 ? scaled(v, ["", "k", "M"], 1000) : v >= 10 ? v.toFixed(0) : v.toFixed(2)) + "/s";
      case "per_min": return (v >= 10 ? v.toFixed(0) : v.toFixed(2)) + "/min";
      default: return v >= 1000 ? scaled(v, ["", "k", "M", "G"], 1000).trim() : (v === Math.round(v) ? String(v) : v.toFixed(2));
    }
  }
  function seconds(v) {
    if (v < 1) { return (v * 1000).toFixed(0) + " ms"; }
    if (v < 120) { return v.toFixed(1) + " s"; }
    if (v < 7200) { return Math.round(v / 60) + " min"; }
    if (v < 172800) { return (v / 3600).toFixed(1) + " h"; }
    return (v / 86400).toFixed(1) + " d";
  }
  function clock(t, hours) {
    var d = new Date(t * 1000), pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return hours > 36 ? pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) : pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function niceTicks(max) {
    if (!(max > 0)) { return [0, 1]; }
    var raw = max / 4, magnitude = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), norm = raw / magnitude;
    var step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * magnitude, ticks = [];
    for (var v = 0; v <= max + step * 0.999; v += step) { ticks.push(v); }
    return ticks;
  }

  // ---- one chart ----------------------------------------------------------------------------------------------------------------------
  function chart(host, id, panel, range) {
    host.textContent = "";
    host.appendChild(el("h3", {}, panel.title));
    if (panel.description) { host.appendChild(el("p", {"class": "desc"}, panel.description)); }
    if (panel.empty) { host.appendChild(el("div", {"class": "note"}, panel.note || "No data.")); return; }
    var W = 560, H = 190, m = {l: 58, r: 10, t: 8, b: 22}, series = panel.series;
    var visible = series.filter(function (s) { return !hidden[id + "|" + s.name]; });
    var x0 = range.start, x1 = range.end, stacked = panel.stacked, cumulative = {};
    var ymax = 0;
    if (stacked) {
      var stamps = {};
      visible.forEach(function (s) { s.points.forEach(function (p) { stamps[p[0]] = (stamps[p[0]] || 0) + p[1]; }); });
      Object.keys(stamps).forEach(function (t) { ymax = Math.max(ymax, stamps[t]); });
    } else {
      visible.forEach(function (s) { s.points.forEach(function (p) { ymax = Math.max(ymax, p[1]); }); });
    }
    if (panel.max !== null && panel.max !== undefined) { ymax = panel.max; }
    var ticks = niceTicks(ymax), top = ticks[ticks.length - 1] || 1;
    if (panel.max !== null && panel.max !== undefined) { top = panel.max; ticks = ticks.filter(function (t) { return t <= top; }); }
    var X = function (t) { return m.l + (t - x0) / (x1 - x0) * (W - m.l - m.r); };
    var Y = function (v) { return H - m.b - v / top * (H - m.t - m.b); };
    var root = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": panel.title});
    var grid = svg("g", {"class": "grid"}), axis = svg("g", {"class": "axis"});
    ticks.forEach(function (t) {
      grid.appendChild(svg("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t)}));
      var label = svg("text", {x: m.l - 6, y: Y(t) + 3, "text-anchor": "end"}); label.textContent = fmt(panel.unit, t); axis.appendChild(label);
    });
    for (var i = 0; i <= 4; i += 1) {
      var tx = x0 + (x1 - x0) * i / 4, label2 = svg("text", {x: X(tx), y: H - 6, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle"});
      label2.textContent = clock(tx, range.hours); axis.appendChild(label2);
    }
    root.appendChild(grid); root.appendChild(axis);
    var baseline = {};
    visible.forEach(function (s, index) {
      var color = s.color || PALETTE[index % PALETTE.length], d = "", area = "", gap = 0, last = null, below = [];
      var steps = []; for (var k = 1; k < s.points.length; k += 1) { steps.push(s.points[k][0] - s.points[k - 1][0]); }
      steps.sort(function (a, b) { return a - b; }); gap = steps.length ? steps[Math.floor(steps.length / 2)] * 2.5 : 0;
      s.points.forEach(function (p, n) {
        var base = stacked ? (baseline[p[0]] || 0) : 0, value = p[1] + base;
        if (stacked) { baseline[p[0]] = value; }
        var px = X(p[0]), py = Y(value);
        if (last === null || (gap && p[0] - last > gap)) { d += "M" + px.toFixed(1) + " " + py.toFixed(1); } else { d += "L" + px.toFixed(1) + " " + py.toFixed(1); }
        last = p[0];
      });
      if (stacked) {
        var path = svg("path", {d: d + "L" + X(s.points[s.points.length - 1][0]).toFixed(1) + " " + Y(0) + "L" + X(s.points[0][0]).toFixed(1) + " " + Y(0) + "Z", fill: color, "fill-opacity": 0.25, stroke: "none"});
        root.appendChild(path);
      }
      root.appendChild(svg("path", {d: d, fill: "none", stroke: color, "stroke-width": 1.6, "stroke-linejoin": "round"}));
      if (s.points.length === 1) { root.appendChild(svg("circle", {cx: X(s.points[0][0]), cy: Y(s.points[0][1]), r: 2.5, fill: color})); }
    });
    var cursor = svg("line", {y1: m.t, y2: H - m.b, stroke: "#9cd4f5", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden"});
    root.appendChild(cursor);
    var overlay = svg("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"});
    overlay.addEventListener("mousemove", function (event) {
      var box = root.getBoundingClientRect(), px = (event.clientX - box.left) / box.width * W, t = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0);
      var lines = [], nearest = null;
      visible.forEach(function (s, index) {
        var best = null;
        s.points.forEach(function (p) { if (best === null || Math.abs(p[0] - t) < Math.abs(best[0] - t)) { best = p; } });
        if (best && (!nearest || Math.abs(best[0] - t) < Math.abs(nearest - t))) { nearest = best[0]; }
        if (best) { lines.push([s.color || PALETTE[index % PALETTE.length], s.name, best[1]]); }
      });
      if (nearest === null) { return; }
      cursor.setAttribute("x1", X(nearest)); cursor.setAttribute("x2", X(nearest)); cursor.setAttribute("visibility", "visible");
      tip.textContent = "";
      tip.appendChild(el("b", {}, new Date(nearest * 1000).toLocaleString()));
      lines.forEach(function (l) {
        var row = el("div", {}), sw = el("span", {"class": "sw"}); sw.style.background = l[0];
        row.appendChild(sw); row.appendChild(document.createTextNode(" " + l[1] + ": " + fmt(panel.unit, l[2]))); tip.appendChild(row);
      });
      tip.hidden = false; tip.style.left = Math.min(window.innerWidth - 290, event.clientX + 14) + "px"; tip.style.top = (event.clientY + 14) + "px";
    });
    overlay.addEventListener("mouseleave", function () { tip.hidden = true; cursor.setAttribute("visibility", "hidden"); });
    root.appendChild(overlay);
    host.appendChild(root);
    var legend = el("div", {"class": "legend"});
    series.forEach(function (s, index) {
      var last = s.points.length ? s.points[s.points.length - 1][1] : null;
      var button = el("button", {type: "button", "class": hidden[id + "|" + s.name] ? "off" : "", title: "Show or hide"});
      var sw = el("span", {"class": "sw"}); sw.style.background = s.color || PALETTE[index % PALETTE.length];
      button.appendChild(sw); button.appendChild(document.createTextNode(s.name + (last === null ? "" : " " + fmt(panel.unit, last))));
      button.addEventListener("click", function () { hidden[id + "|" + s.name] = !hidden[id + "|" + s.name]; chart(host, id, panel, range); });
      legend.appendChild(button);
    });
    host.appendChild(legend);
    if (panel.note) { host.appendChild(el("p", {"class": "desc"}, panel.note)); }
  }

  // ---- tiles and tables -----------------------------------------------------------------------------------------------------------------
  function tile(k, v, sub, level) {
    var t = el("div", {"class": "dash-tile" + (level ? " " + level : "")});
    t.appendChild(el("div", {"class": "k"}, k)); t.appendChild(el("div", {"class": "v"}, v)); if (sub) { t.appendChild(el("div", {"class": "s"}, sub)); }
    return t;
  }
  function renderTiles(t) {
    tilesEl.textContent = "";
    var use = t.connections !== null && t.max_connections ? t.connections * 100 / t.max_connections : null;
    tilesEl.appendChild(tile("Connections", t.connections === null ? "–" : t.connections + " / " + t.max_connections, use === null ? "" : use.toFixed(0) + " % of the limit", use > 90 ? "bad" : use > 75 ? "warn" : "good"));
    tilesEl.appendChild(tile("Active now", t.active === null ? "–" : String(t.active), t.idle_in_transaction ? t.idle_in_transaction + " idle in transaction" : ""));
    tilesEl.appendChild(tile("Blocked", t.blocked === null ? "–" : String(t.blocked), "sessions waiting on locks", t.blocked ? "bad" : "good"));
    tilesEl.appendChild(tile("Transactions", fmt("per_s", t.tps), "commits + rollbacks"));
    tilesEl.appendChild(tile("Cache hit", t.cache_hit === null ? "–" : t.cache_hit.toFixed(1) + " %", "reads served from memory", t.cache_hit !== null && t.cache_hit < 90 ? "warn" : ""));
    tilesEl.appendChild(tile("WAL written", fmt("bytes_s", t.wal_rate), ""));
    tilesEl.appendChild(tile("Size", fmt("bytes", t.size), ""));
    tilesEl.appendChild(tile("Oldest transaction", t.oldest_transaction === null ? "–" : seconds(t.oldest_transaction), "open right now", t.oldest_transaction > 600 ? "warn" : ""));
    tilesEl.appendChild(tile("Up for", t.uptime === null ? "–" : seconds(t.uptime), t.version));
    tilesEl.appendChild(tile("Role", t.role || "–", t.role === "primary" ? t.standbys + " standby" + (t.standbys === 1 ? "" : "s") + ", " + t.slots + " slot" + (t.slots === 1 ? "" : "s") : ""));
  }
  function table(title, head, rows, wide) {
    var box = el("div", {"class": "dash-panel" + (wide ? " dash-wide" : "")});
    box.appendChild(el("h3", {}, title));
    if (!rows.length) { box.appendChild(el("div", {"class": "note"}, "Nothing to show.")); return box; }
    var tbl = el("table", {"class": "dash-table"}), thead = el("thead"), hr = el("tr");
    head.forEach(function (h) { hr.appendChild(el("th", {}, h[0])); });
    thead.appendChild(hr); tbl.appendChild(thead);
    var tb = el("tbody");
    rows.forEach(function (r) {
      var tr = el("tr");
      head.forEach(function (h) {
        var cell = h[2] === "code" ? el("td", {}) : el("td", {"class": h[2] === "n" ? "n" : ""});
        if (h[2] === "code") { cell.appendChild(el("code", {}, String(r[h[1]] === null ? "" : r[h[1]]))); } else { cell.textContent = h[3] ? h[3](r[h[1]], r) : (r[h[1]] === null || r[h[1]] === undefined ? "–" : String(r[h[1]])); }
        tr.appendChild(cell);
      });
      tb.appendChild(tr);
    });
    tbl.appendChild(tb); box.appendChild(tbl);
    return box;
  }
  function renderStatus(d) {
    var t = d.tiles, parts = [];
    status.textContent = "";
    var add = function (text, cls) { var s = el("span", cls ? {"class": cls} : {}, text); status.appendChild(s); status.appendChild(document.createTextNode("   ")); };
    if (!t.samples) { add("No samples yet: run  manage.py run_watch  (or press Sample now).", "bad"); return; }
    add("Last sample " + (t.last_sample_age === null ? "–" : seconds(t.last_sample_age) + " ago"), t.last_sample_age > d.range.interval * 3 ? "bad" : "ok");
    add(d.range.samples + " samples in this period, every " + seconds(d.range.interval));
    if (t.failures) { add("The last " + t.failures + " attempt" + (t.failures === 1 ? "" : "s") + " failed: " + t.last_error, "bad"); }
    if (t.skipped && t.skipped.length) { add("Not collected: " + t.skipped.join("; "), "bad"); }
    if (!t.statements_extension) { add("pg_stat_statements is not installed here: no statement history (Monitor, Features)."); }
  }

  function render(d) {
    current = d;
    renderTiles(d.tiles); renderStatus(d);
    var selected = dbSel.value;
    dbSel.textContent = ""; dbSel.appendChild(el("option", {value: ""}, "All databases"));
    d.databases.forEach(function (name) { var o = el("option", {value: name}, name); if (name === d.database) { o.selected = true; } dbSel.appendChild(o); });
    body.textContent = "";
    SECTIONS.forEach(function (section) {
      body.appendChild(el("div", {"class": "dash-section"}, section[0]));
      var grid = el("div", {"class": "dash-grid"});
      section[1].forEach(function (id) {
        if (!d.panels[id]) { return; }
        var host = el("div", {"class": "dash-panel", "data-panel": id}); grid.appendChild(host); chart(host, id, d.panels[id], d.range);
      });
      body.appendChild(grid);
    });
    var tables = el("div", {"class": "dash-grid"});
    body.appendChild(el("div", {"class": "dash-section"}, "Details"));
    tables.appendChild(table("Costliest statements in this period", [["Statement", "query", "code"], ["Calls", "calls", "n"], ["Total", "total_ms", "n", function (v) { return fmt("ms", v); }],
      ["Mean", "mean_ms", "n", function (v) { return fmt("ms", v); }], ["Rows", "rows", "n"], ["Cache hit", "hit_percent", "n", function (v) { return v === null ? "–" : v + " %"; }]], d.tables.statements, true));
    tables.appendChild(table("Databases", [["Database", "database"], ["Size", "size", "n", function (v) { return fmt("bytes", v); }], ["Connections", "connections", "n"], ["Active", "active", "n"],
      ["TPS", "tps", "n"], ["Cache hit", "cache_hit", "n", function (v) { return v === null ? "–" : v + " %"; }], ["Deadlocks", "deadlocks", "n"], ["XID age", "xid_percent", "n", function (v) { return v + " %"; }]], d.tables.databases, true));
    tables.appendChild(table("Standbys", [["Name", "name"], ["Address", "client"], ["State", "state"], ["Sync", "sync"], ["Replay lag", "replay_lag", "n", function (v) { return fmt("bytes", v); }],
      ["Replay lag (time)", "replay_lag_s", "n", function (v) { return v === null ? "–" : fmt("s", v); }]], d.tables.standbys, false));
    tables.appendChild(table("Replication slots", [["Slot", "name"], ["Type", "type"], ["Active", "active", "", function (v) { return v ? "yes" : "NO"; }], ["WAL kept", "retained", "n", function (v) { return fmt("bytes", v); }],
      ["Status", "wal_status"]], d.tables.slots, false));
    var settings = Object.keys(d.tables.settings || {}).map(function (k) { return {name: k, value: d.tables.settings[k]}; });
    tables.appendChild(table("Settings that explain the charts", [["Setting", "name"], ["Value", "value"]], settings, false));
    body.appendChild(tables);
  }

  function load() {
    var url = body.getAttribute("data-url") + "?server=" + encodeURIComponent(serverSel.value) + "&range=" + encodeURIComponent(rangeSel.value) + "&db=" + encodeURIComponent(dbSel.value);
    fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}}).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) { status.textContent = d.error || "Could not load."; return; }
      render(d);
    }).catch(function () { status.textContent = "Could not load the dashboards. Try again."; });
  }
  function schedule() { clearInterval(timer); if (auto.checked) { timer = setInterval(function () { if (!document.hidden) { load(); } }, 30000); } }
  serverSel.addEventListener("change", function () { dbSel.value = ""; load(); });
  dbSel.addEventListener("change", load); rangeSel.addEventListener("change", load); auto.addEventListener("change", schedule);
  document.getElementById("dashSample").addEventListener("click", function () {
    var button = this, token = (document.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
    button.disabled = true; status.textContent = "Sampling…";
    var data = new FormData(); data.append("server", serverSel.value);
    fetch(body.getAttribute("data-sample-url"), {method: "POST", credentials: "same-origin", body: data, headers: {"X-CSRFToken": token, "X-Requested-With": "fetch"}})
      .then(function (r) { return r.json(); }).then(function (a) { if (!a.ok) { status.textContent = a.error || "Not sampled."; } else { load(); } })
      .catch(function () { status.textContent = "The request did not complete."; }).then(function () { button.disabled = false; });
  });
  load(); schedule();
})();