// Object tree in the sidebar (on every page) and the details pane (on the Object Browser page). No libraries.
//  - Clicking the arrow only opens or closes a node.
//  - Clicking a node selects it. On the Object Browser page its details load in place; on any other page
//    PGDAS goes to the Object Browser with that node (and its path in the tree) selected.
(function () {
  var treeEl = document.getElementById("tree");
  if (!treeEl) return;
  var detailEl = document.getElementById("detail");          // only on the Object Browser page
  var overview = document.getElementById("servers-overview");
  var treeUrl = treeEl.dataset.treeUrl;
  var detailUrl = treeEl.dataset.detailUrl;
  var homeUrl = treeEl.dataset.homeUrl;
  // table = the relation (table, view, sequence); name = a type or trigger; oid = a function or procedure;
  // rel = the kind of relation a Columns / Indexes / Triggers folder belongs to.
  var KEYS = ["type", "server", "db", "schema", "table", "name", "oid", "rel"];
  var ICONS = {servers: "▣", server: "▥", databases: "▤", database: "◉", schemas: "▤", schema: "▦",
               roles: "▤", role: "☻", tablespaces: "▤", tablespace: "▭", catalogs: "▤", catalog: "▦",
               extensions: "▤", extension: "⬢", eventtriggers: "▤", eventtrigger: "↯", publications: "▤", publication: "⇉",
               tables: "▤", table: "▦", views: "▤", view: "◧", matviews: "▤", matview: "◨",
               functions: "▤", function: "ƒ", procedures: "▤", procedure: "ƒ", sequences: "▤", sequence: "№",
               types: "▤", type: "◈", columns: "▤", column: "·", constraints: "▤", constraint: "·",
               indexes: "▤", index: "·", triggers: "▤", trigger: "↯"};
  var FOLDER_OF = {column: "columns", constraint: "constraints", index: "indexes", trigger: "triggers"};
  var SCHEMA_FOLDER_OF = {table: "tables", view: "views", matview: "matviews", sequence: "sequences",
                          function: "functions", procedure: "procedures", type: "types"};
  var SCHEMA_FOLDERS = ["tables", "views", "matviews", "functions", "procedures", "sequences", "types"];
  // Roles and Tablespaces hang off the server; Catalogs, Extensions, Event Triggers and Publications off a database.
  var SERVER_FOLDERS = ["roles", "tablespaces"];
  var SERVER_FOLDER_OF = {role: "roles", tablespace: "tablespaces"};
  var DB_FOLDERS = ["catalogs", "extensions", "eventtriggers", "publications"];
  var DB_FOLDER_OF = {extension: "extensions", eventtrigger: "eventtriggers", publication: "publications"};
  var CATALOG_SCHEMAS = ["pg_catalog", "information_schema"];   // these sit under Catalogs, not Schemas
  var selected = null;
  // "Show system objects" (extension members, pg_catalog, information_schema) is remembered per browser.
  var showSystem = false;
  try { showSystem = localStorage.getItem("pgdas.showSystem") === "1"; } catch (e) { /* not remembered */ }
  var systemBox = document.getElementById("showSystem");
  if (systemBox) {
    systemBox.checked = showSystem;
    systemBox.addEventListener("change", function () {
      try { localStorage.setItem("pgdas.showSystem", systemBox.checked ? "1" : "0"); } catch (e) { /* ignore */ }
      window.location.reload();
    });
  }

  function query(node) {
    var params = new URLSearchParams();
    KEYS.forEach(function (key) { if (node[key]) params.set(key, node[key]); });
    if (showSystem) params.set("system", "1");
    return params.toString();
  }

  function get(url) {
    return fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}});
  }

  // Error tone (see sound.js) when something could not be loaded; silent if sounds are muted or unavailable.
  function errorTone() { if (window.pgdasSound) window.pgdasSound.play("error"); }

  function showDetail(qs) {
    if (showSystem && qs.indexOf("system=") < 0) qs += "&system=1";
    detailEl.innerHTML = '<p class="muted">Loading…</p>';
    get(detailUrl + "?" + qs)
      .then(function (r) { return r.text(); })
      .then(function (html) {
        detailEl.innerHTML = html;
        if (detailEl.querySelector(".messages .error")) errorTone();
      })
      .catch(function () {
        detailEl.innerHTML = '<ul class="messages"><li class="error">Could not load details.</li></ul>';
        errorTone();
      });
  }

  function note(text, isError) {
    var li = document.createElement("li");
    var div = document.createElement("div");
    div.className = "note" + (isError ? " err" : "");
    div.textContent = text;
    li.appendChild(div);
    return li;
  }

  function loadChildren(node, list, done) {
    list.innerHTML = "";
    list.appendChild(note("Loading…"));
    get(treeUrl + "?" + query(node))
      .then(function (r) { return r.json().then(function (body) { return {ok: r.ok, body: body}; }); })
      .then(function (res) {
        list.innerHTML = "";
        if (!res.ok) { list.appendChild(note(res.body.error || "Could not load.", true)); errorTone(); done(false); return; }
        if (!res.body.nodes.length) list.appendChild(note("(empty)"));
        res.body.nodes.forEach(function (child) { list.appendChild(renderNode(child)); });
        done(true);
      })
      .catch(function () { list.innerHTML = ""; list.appendChild(note("Could not load.", true)); errorTone(); done(false); });
  }

  function select(row, node) {
    if (!detailEl) { window.location.href = homeUrl + "?" + query(node); return; }
    if (selected) selected.classList.remove("selected");
    selected = row;
    row.classList.add("selected");
    // The title, "Add server" button and tiles belong to the top "Servers" node only.
    if (overview) overview.hidden = node.type !== "servers";
    showDetail(query(node));
  }

  function renderNode(node) {
    var li = document.createElement("li");
    li.setAttribute("role", "treeitem");
    li.nodeData = node;
    var row = document.createElement("div");
    row.className = "row";
    var twist = document.createElement("span");
    twist.className = "twist" + (node.expandable ? "" : " leaf");
    twist.textContent = node.expandable ? "▸" : "";
    var kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = ICONS[node.type] || "·";
    var label = document.createElement("span");
    label.className = "label";
    label.textContent = node.label;
    label.title = node.label;
    row.appendChild(twist); row.appendChild(kind); row.appendChild(label);
    if (node.hint) { var hint = document.createElement("span"); hint.className = "hint"; hint.textContent = node.hint; row.appendChild(hint); }
    li.appendChild(row);

    var list = null, open = false;
    li.loaded = false;
    li.row = row;
    function toggle() {
      if (!node.expandable) return;
      if (!list) { list = document.createElement("ul"); li.appendChild(list); }
      open = !open;
      twist.textContent = open ? "▾" : "▸";
      list.hidden = !open;
      if (open && !li.loaded) loadChildren(node, list, function (ok) { li.loaded = ok; });
    }
    li.expand = function () { if (!open) toggle(); };
    li.reload = function () {                          // after a change: list the children again (only if they were ever shown)
      li.loaded = false;
      if (list && open) loadChildren(node, list, function (ok) { li.loaded = ok; });
    };
    twist.addEventListener("click", function (event) { event.stopPropagation(); toggle(); });
    row.addEventListener("click", function () { select(row, node); if (detailEl) li.expand(); });
    row.tabIndex = 0;
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); row.click(); }
    });
    return li;
  }

  function waitFor(test, timeout) {
    return new Promise(function (resolve) {
      var waited = 0;
      (function check() {
        var found = test();
        if (found) return resolve(found);
        if ((waited += 100) > (timeout || 10000)) return resolve(null);
        setTimeout(check, 100);
      })();
    });
  }

  function findItem(match) {
    var items = treeEl.querySelectorAll("li");
    for (var i = 0; i < items.length; i++) {
      var data = items[i].nodeData;
      if (data && KEYS.every(function (key) { return !match[key] || String(data[key]) === String(match[key]); })) return items[i];
    }
    return null;
  }

  // The nodes from the top of the tree down to `target`: all but the last are opened, the last is selected.
  function pathTo(target) {
    var t = target.type, s = target.server, path = [];
    if (t === "servers") return [{type: "servers"}];
    path.push({type: "server", server: s});
    if (t === "server") return path;
    if (SERVER_FOLDERS.indexOf(t) >= 0) { path.push({type: t, server: s}); return path; }
    if (SERVER_FOLDER_OF[t]) {
      path.push({type: SERVER_FOLDER_OF[t], server: s});
      path.push({type: t, server: s, name: target.name});
      return path;
    }
    path.push({type: "databases", server: s});
    if (t === "databases") return path;
    path.push({type: "database", server: s, db: target.db});
    if (t === "database") return path;
    if (DB_FOLDERS.indexOf(t) >= 0) { path.push({type: t, server: s, db: target.db}); return path; }
    if (DB_FOLDER_OF[t]) {
      path.push({type: DB_FOLDER_OF[t], server: s, db: target.db});
      path.push({type: t, server: s, db: target.db, name: target.name});
      return path;
    }
    var where = {server: s, db: target.db, schema: target.schema};
    if (CATALOG_SCHEMAS.indexOf(target.schema) >= 0) {
      path.push({type: "catalogs", server: s, db: target.db});
      path.push(Object.assign({type: "catalog"}, where));
      if (t === "catalog") return path;
    } else {
      path.push({type: "schemas", server: s, db: target.db});
      if (t === "schemas") return path;
      path.push(Object.assign({type: "schema"}, where));
      if (t === "schema") return path;
    }
    // Folders under a schema (Tables, Views, Functions, ...) and the items inside them.
    if (SCHEMA_FOLDERS.indexOf(t) >= 0) { path.push(Object.assign({type: t}, where)); return path; }
    if (SCHEMA_FOLDER_OF[t]) {
      path.push(Object.assign({type: SCHEMA_FOLDER_OF[t]}, where));
      var item = Object.assign({type: t}, where);
      ["table", "name", "oid"].forEach(function (key) { if (target[key]) item[key] = target[key]; });
      path.push(item);
      return path;
    }
    // Columns / Constraints / Indexes / Triggers (or one of their items) belong to a table, view or materialized view.
    var rel = target.rel || "table";
    path.push(Object.assign({type: SCHEMA_FOLDER_OF[rel] || "tables"}, where));
    path.push(Object.assign({type: rel, table: target.table}, where));
    path.push(Object.assign({type: FOLDER_OF[t] || t, table: target.table}, where));
    return path;
  }

  function openPath(target) {
    var steps = pathTo(target);
    function next(index) {
      var match = steps[index];
      return waitFor(function () { return findItem(match); }).then(function (item) {
        if (!item) return null;
        if (index === steps.length - 1) {
          item.row.scrollIntoView({block: "nearest"});
          item.row.click();
          return item;
        }
        item.expand();
        return waitFor(function () { return item.loaded; }).then(function () { return next(index + 1); });
      });
    }
    return next(0);
  }

  // Tabs and pagers inside the detail pane carry their own query string; the server list can open a server.
  if (detailEl) {
    detailEl.addEventListener("click", function (event) {
      var open = event.target.closest("[data-open-server]");
      if (open) { event.preventDefault(); openPath({type: "server", server: open.dataset.openServer}); return; }
      var details = event.target.closest("[data-open-database]");       // "Details" in the Databases list: open it in the tree
      if (details) { event.preventDefault(); openPath({type: "database", server: details.dataset.server, db: details.dataset.openDatabase}); return; }
      var link = event.target.closest("[data-detail]");
      if (!link) return;
      event.preventDefault();
      showDetail(link.dataset.detail);
    });
  }

  // After a database or role was created, renamed or dropped: list that folder again and redraw the details if they show it.
  document.addEventListener("pgdas:changed", function (event) {
    var change = event.detail || {};
    var item = findItem({type: change.what, server: String(change.server)});
    if (item) item.reload();
    var current = selected && selected.parentNode;
    if (detailEl && current && current.nodeData && current.nodeData.type === change.what) showDetail(query(current.nodeData));
  });

  // What to show first: a node named in the address (/?type=table&server=1&db=...), an old /?server=3 link, or Servers.
  function startNode() {
    var params = new URLSearchParams(window.location.search);
    var type = params.get("type");
    if (!detailEl || (!type && !params.get("server"))) return null;
    var node = {type: type || "server"};
    KEYS.forEach(function (key) { if (key !== "type" && params.get(key)) node[key] = params.get(key); });
    return node.type === "servers" ? null : node;
  }

  var rootItem = renderNode({type: "servers", server: "", db: "", schema: "", table: "", label: "Servers", expandable: true});
  treeEl.appendChild(rootItem);
  var start = startNode();
  if (!detailEl) {
    rootItem.expand();                       // other pages: just list the servers; clicking a node goes to the browser
  } else if (start) {
    rootItem.expand();
    openPath(start);
  } else {
    rootItem.row.click();
  }
})();
