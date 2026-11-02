// The home page (Servers node): fills the dashboard that templates/explorer/explorer.html lays out, from /dashboard/data/.
// Server rows carry the same actions the server list had: Open, SQL, Test, and (staff) Edit and Remove.
// Everything from the server is put into the page as text, never as HTML.
(function () {
  var root = document.getElementById("servers-overview");
  if (!root) return;
  var $ = function (id) { return document.getElementById(id); };
  var NS = "http://www.w3.org/2000/svg";
  var csrf = ((document.querySelector("#dCsrf [name=csrfmiddlewaretoken]") || {}).value) || "";

  function el(tag, attrs, text) { var e = document.createElement(tag); Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); }); if (text != null) e.textContent = text; return e; }
  function svg(tag, attrs) { var e = document.createElementNS(NS, tag); Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); }); return e; }
  function tone(percent) { return percent >= 90 ? "#ff7b8d" : percent >= 70 ? "#e9b100" : "#00e99c"; }

  function metrics(d) {
    var t = d.totals;
    $("mServers").textContent = t.servers; $("mServersNote").textContent = t.online + " online" + (t.offline ? " · " + t.offline + " offline" : "");
    $("mServersNote").className = t.offline ? "bad-text" : "";
    $("mDatabases").textContent = t.databases; $("mDatabasesNote").textContent = t.size_text + " in total";
    $("mUsers").textContent = t.users; $("mUsersNote").textContent = t.staff + " staff";
    $("mAlerts").textContent = d.is_staff ? t.alerts : "–";
    $("mAlertsNote").textContent = !d.is_staff ? "staff only" : t.alerts ? t.critical + " critical" : "nothing needs attention";
    $("mAlertsNote").className = d.is_staff && t.alerts ? "bad-text" : "";
    $("dStatus").textContent = t.offline ? "A SERVER IS DOWN" : t.servers ? "ALL SERVERS REACHABLE" : "NO SERVERS YET";
  }

  function actions(cell, s, canManage) {
    var box = el("div", {class: "dash-actions"});
    box.appendChild(el("a", {class: "btn", href: "#", "data-open-server": s.id}, "Open"));                 // explorer.js opens it in the tree
    box.appendChild(el("a", {class: "btn", href: s.urls.sql}, "SQL"));
    var test = el("form", {method: "post", action: s.urls.test});                                          // a POST, like the old Test button
    test.appendChild(el("input", {type: "hidden", name: "csrfmiddlewaretoken", value: csrf}));
    test.appendChild(el("button", {class: "btn", type: "submit"}, "Test"));
    box.appendChild(test);
    if (canManage) {
      box.appendChild(el("a", {class: "btn", href: s.urls.edit}, "Edit"));
      box.appendChild(el("a", {class: "btn danger", href: s.urls.remove}, "Remove"));
    }
    cell.appendChild(box);
  }

  function servers(d) {
    var body = $("dServers"); body.replaceChildren();
    if (!d.servers.length) {
      var c = body.insertRow().insertCell(); c.colSpan = 7; c.className = "muted"; c.textContent = "No servers yet. ";
      if (d.can_manage) { var add = el("a", {href: root.dataset.addUrl}, "Add your first PostgreSQL server."); c.appendChild(add); } else c.textContent += "Ask an administrator to add one.";
      return;
    }
    d.servers.forEach(function (s) {
      var tr = body.insertRow(), name = tr.insertCell();
      name.className = "dash-name";
      var link = el("a", {href: "#", "data-open-server": s.id}); link.appendChild(el("strong", {}, s.name)); name.appendChild(link);
      name.appendChild(el("div", {class: "muted"}, s.host));
      tr.insertCell().textContent = s.ok ? s.version : "";
      var sess = tr.insertCell();
      if (s.ok) {
        sess.textContent = s.sessions + " / " + s.max + (s.active ? " (" + s.active + " active)" : "");
        var pct = s.max ? s.sessions * 100 / s.max : 0, bar = el("div", {class: "dash-bar"}), fill = el("span", {});
        fill.style.width = Math.min(100, pct) + "%"; fill.style.background = tone(pct); bar.appendChild(fill); sess.appendChild(bar);
      }
      tr.insertCell().textContent = s.ok ? s.databases : ""; tr.insertCell().textContent = s.ok ? s.size_text : "";
      var st = tr.insertCell(); st.appendChild(el("span", {class: "badge " + (s.ok ? "ok" : "bad")}, s.ok ? "online" : "offline"));
      if (!s.ok) st.appendChild(el("div", {class: "muted"}, s.error));
      actions(tr.insertCell(), s, d.can_manage);
    });
  }

  // Rings like the ADMINWEB template's "Resource usage": the theme's own .gauge (a conic-gradient ring with a disc inside), the label
  // and a pulse icon underneath. The ring is the accent colour, and turns amber or red when the number needs a look.
  var COLORS = {warn: "#e9b100", bad: "#ff7b8d"};
  var PULSE = "0,9 4,9 6,4 9,14 11,9 16,9";
  function ring(short, label, g, sub, level) {
    var col = el("div", {class: "dash-col"}), gauge = el("div", {class: "gauge", role: "img", "aria-label": label + (g ? " " + g.percent + "%" : " not available")});
    gauge.style.setProperty("--val", g ? Math.max(0, Math.min(100, g.percent)) : 0);
    if (g && COLORS[level]) gauge.style.background = "conic-gradient(" + COLORS[level] + " calc(var(--val) * 1%), #07365f 0)";
    var inner = el("div", {}); inner.appendChild(document.createTextNode(short)); inner.appendChild(el("strong", {}, g ? g.percent + "%" : "–"));
    gauge.appendChild(inner); col.appendChild(gauge);
    col.appendChild(el("div", {class: "dash-col-label"}, label)); col.appendChild(el("div", {class: "dash-col-detail"}, g ? g.label : sub));
    var icon = el("i", {class: "dash-pulse", "aria-hidden": "true"}), s = svg("svg", {viewBox: "0 0 16 16", width: "27", height: "27"});
    s.appendChild(svg("polyline", {points: PULSE, fill: "none", stroke: "currentColor", "stroke-width": 1.3, "stroke-linecap": "round", "stroke-linejoin": "round"}));
    icon.appendChild(s); col.appendChild(icon);
    return col;
  }
  function gauges(d) {
    var box = $("dGauges"), g = d.gauges; box.replaceChildren();
    box.appendChild(ring("CONN", "CONNECTIONS", g.connections, "no server answered", g.connections && (g.connections.percent >= 90 ? "bad" : g.connections.percent >= 70 ? "warn" : "ok")));
    box.appendChild(ring("CACHE", "CACHE HIT", g.cache_hit, "no reads yet", g.cache_hit && (g.cache_hit.percent >= 95 ? "ok" : g.cache_hit.percent >= 85 ? "warn" : "bad")));
    box.appendChild(ring("BUSY", "BUSY SESSIONS", g.busy, "no sessions open", g.busy && (g.busy.percent >= 90 ? "warn" : "ok")));
  }

  function storage(d) {
    var box = $("dStorage"); box.replaceChildren();
    if (!d.storage.length) { box.appendChild(el("p", {class: "muted"}, "No databases to show.")); return; }
    var max = d.storage[0].size || 1;
    d.storage.forEach(function (s) {
      var row = el("div", {class: "dash-row"}), head = el("div", {class: "dash-row-head"}), bar = el("div", {class: "dash-bar"}), fill = el("span", {});
      head.appendChild(el("span", {}, s.database)); head.appendChild(el("span", {class: "muted"}, s.size_text));
      fill.style.width = Math.max(2, s.size * 100 / max) + "%"; bar.appendChild(fill); row.appendChild(head); row.appendChild(bar); box.appendChild(row);
    });
  }

  function staff(d) {
    document.querySelectorAll(".dash-staff").forEach(function (p) { p.hidden = !d.is_staff; });
    if (!d.is_staff) return;
    var body = $("dActivity"); body.replaceChildren();
    d.activity.forEach(function (a) {
      var tr = body.insertRow(); tr.insertCell().textContent = a.when.slice(0, 19).replace("T", " "); tr.insertCell().textContent = a.user;
      var act = tr.insertCell(); act.textContent = a.action; if (!a.ok) { act.className = "bad-text"; act.textContent += " (failed)"; }
      tr.insertCell().textContent = (a.server ? a.server + " · " : "") + a.detail;
    });
    if (!d.activity.length) { var c = body.insertRow().insertCell(); c.colSpan = 4; c.className = "muted"; c.textContent = "Nothing recorded yet."; }
    var list = $("dAttention"); list.replaceChildren();
    if (!d.attention.length) list.appendChild(el("li", {class: "muted"}, "Nothing needs attention."));
    d.attention.forEach(function (a) {
      var li = el("li", {class: "alert " + a.level}); li.appendChild(el("strong", {}, a.title)); if (a.detail) li.appendChild(el("div", {class: "muted"}, a.detail)); list.appendChild(li);
    });
  }

  function load() {
    fetch(root.dataset.url, {credentials: "same-origin"})
      .then(function (r) { return r.json(); })
      .then(function (d) { metrics(d); servers(d); gauges(d); storage(d); staff(d); })
      .catch(function () { $("dStatus").textContent = "COULD NOT LOAD"; });
  }
  load();
  setInterval(function () { if (!document.hidden && !root.hidden) load(); }, 30000);
  document.addEventListener("pgdas:changed", load);          // a server was added, changed or removed elsewhere on the page
})();
