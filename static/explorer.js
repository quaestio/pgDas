// Object browser: a lazy-loading tree on the left, details on the right. No libraries.
(function () {
  var root = document.getElementById("explorer");
  if (!root) return;
  var treeEl = document.getElementById("tree");
  var detailEl = document.getElementById("detail");
  var treeUrl = root.dataset.treeUrl;
  var detailUrl = root.dataset.detailUrl;
  var KEYS = ["type", "server", "db", "schema", "table"];
  var ICONS = {servers: "▣", server: "▥", databases: "▤", database: "◉", schemas: "▤", schema: "▦", tables: "▤", table: "▦",
               columns: "▤", column: "·", constraints: "▤", constraint: "·", indexes: "▤", index: "·"};
  var preselect = root.dataset.preselect || "";
  var selected = null;

  function query(node) {
    var params = new URLSearchParams();
    KEYS.forEach(function (key) { if (node[key]) params.set(key, node[key]); });
    return params.toString();
  }

  function get(url) {
    return fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}});
  }

  function showDetail(qs) {
    detailEl.innerHTML = '<p class="muted">Loading…</p>';
    get(detailUrl + "?" + qs)
      .then(function (r) { return r.text(); })
      .then(function (html) { detailEl.innerHTML = html; })
      .catch(function () { detailEl.innerHTML = '<ul class="messages"><li class="error">Could not load details.</li></ul>'; });
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
        if (!res.ok) { list.appendChild(note(res.body.error || "Could not load.", true)); done(false); return; }
        if (!res.body.nodes.length) list.appendChild(note("(empty)"));
        res.body.nodes.forEach(function (child) { list.appendChild(renderNode(child)); });
        done(true);
      })
      .catch(function () { list.innerHTML = ""; list.appendChild(note("Could not load.", true)); done(false); });
  }

  function select(row, node) {
    if (selected) selected.classList.remove("selected");
    selected = row;
    row.classList.add("selected");
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

    var list = null, open = false, loaded = false;
    function toggle() {
      if (!node.expandable) return;
      if (!list) { list = document.createElement("ul"); li.appendChild(list); }
      open = !open;
      twist.textContent = open ? "▾" : "▸";
      list.hidden = !open;
      if (open && !loaded) loadChildren(node, list, function (ok) {
        loaded = ok;
        // Opened from a link such as /?server=3: select that server once the list is there.
        if (ok && node.type === "servers" && preselect) openServer(preselect, list);
      });
    }
    row.addEventListener("click", function () { select(row, node); toggle(); });
    row.tabIndex = 0;
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); row.click(); }
    });
    return li;
  }

  function openServer(id, list) {
    var items = (list || treeEl).querySelectorAll("li");
    for (var i = 0; i < items.length; i++) {
      var data = items[i].nodeData;
      if (data && data.type === "server" && String(data.server) === String(id)) {
        var row = items[i].querySelector(".row");
        row.scrollIntoView({block: "nearest"});
        row.click();
        return;
      }
    }
  }

  // Tabs and pagers inside the detail pane carry their own query string; the server list can open a server.
  detailEl.addEventListener("click", function (event) {
    var open = event.target.closest("[data-open-server]");
    if (open) { event.preventDefault(); openServer(open.dataset.openServer); return; }
    var link = event.target.closest("[data-detail]");
    if (!link) return;
    event.preventDefault();
    showDetail(link.dataset.detail);
  });

  var rootNode = {type: "servers", server: "", db: "", schema: "", table: "", label: "Servers", expandable: true};
  var rootItem = renderNode(rootNode);
  treeEl.appendChild(rootItem);
  rootItem.querySelector(".row").click();
})();
