// SQL runner: Run without leaving the page. The result shows ten rows at a time; the other pages come from the server (also without a page
// load). Under the result: the figures (rows, time) on the left, the pages in the middle and Export on the right.
// Changing statements (INSERT, UPDATE, ...) keep their own reviewed flow (static/write_flow.js). Everything from the server is put in the page as text.
(function () {
  "use strict";
  var form = document.getElementById("sqlForm"), area = document.getElementById("sqlAjax"), box = document.getElementById("sql"), run = document.getElementById("runButton");
  if (!form || !area || !box || !window.fetch) { return; }
  var runUrl = form.getAttribute("data-run-url"), pageUrl = form.getAttribute("data-page-url");
  var exportButton = document.getElementById("exportButton"), exportFormat = form.querySelector("select[name=format]");
  var exportUrl = exportButton ? exportButton.getAttribute("formaction") : "";
  var csrf = (form.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";
  var current = null;                                  // {sql, db, token} of the result on show
  var busy = false;
  if (exportButton) { exportButton.hidden = true; }        // Export moves under the result (it is back here if this script is not running)
  if (exportFormat) { exportFormat.hidden = true; }

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function number(n) { return Number(n).toLocaleString(); }
  function post(url, body) {
    return fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf, "X-Requested-With": "fetch"},
                       body: JSON.stringify(body)}).then(function (r) { return r.json().catch(function () { return {ok: false, error: "The answer was not understood."}; }); });
  }
  function database() { var hidden = form.querySelector("input[name=db]"); return hidden ? hidden.value : ""; }
  function writes(text) {                              // the reviewed write flow takes these (when this person may change data)
    var bare = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/--[^\n]*/g, "").trim();
    return form.dataset.canWrite === "1" && /^(insert|update|delete|truncate|drop|create|alter|grant|revoke)\b/i.test(bare);
  }

  function clearOld() {
    var legacy = document.getElementById("sqlLegacy"), legacyResult = document.getElementById("sqlLegacyResult");
    if (legacy) { legacy.textContent = ""; }
    if (legacyResult) { legacyResult.textContent = ""; }
    var changes = document.getElementById("changeResult");
    if (changes) { changes.textContent = ""; }
  }

  function showError(data) {
    area.textContent = "";
    var list = el("ul", {"class": "messages", style: "margin-top:18px"}), item = el("li", {"class": "error"}, data.error || "That statement could not be run.");
    if (data.hint) { item.appendChild(el("br")); item.appendChild(el("span", {"class": "muted"}, data.hint)); }
    list.appendChild(item); area.appendChild(list);
  }

  function pager(data) {
    var bar = el("div", {"class": "sqlr-pages"});
    if (data.pages <= 1) { return bar; }
    var go = function (label, page, disabled, active) {
      var b = el("button", {type: "button", "class": "btn" + (active ? " primary" : ""), "aria-label": "Page " + page}, label);
      if (disabled) { b.disabled = true; }
      b.addEventListener("click", function () { load(page); });
      bar.appendChild(b);
    };
    go("‹ Prev", data.page - 1, data.page <= 1, false);
    var from = Math.max(1, data.page - 2), to = Math.min(data.pages, from + 4);
    from = Math.max(1, to - 4);
    if (from > 1) { go("1", 1, false, false); if (from > 2) { bar.appendChild(el("span", {"class": "muted"}, "…")); } }
    for (var p = from; p <= to; p += 1) { go(String(p), p, false, p === data.page); }
    if (to < data.pages) { if (to < data.pages - 1) { bar.appendChild(el("span", {"class": "muted"}, "…")); } go(String(data.pages), data.pages, false, false); }
    go("Next ›", data.page + 1, data.page >= data.pages, false);
    return bar;
  }

  function exporter() {
    var holder = el("div", {"class": "sqlr-export"});
    if (!exportUrl) { return holder; }
    var format = el("select", {"aria-label": "Export format"});
    [["csv", "CSV"], ["json", "JSON"], ["sql", "SQL (INSERT statements)"]].forEach(function (f) { format.appendChild(el("option", {value: f[0]}, f[1])); });
    var button = el("button", {type: "button", "class": "btn"}, "Export");
    button.addEventListener("click", function () {         // a normal form post, so the browser downloads the file; it exports the statement that made this result
      var f = el("form", {method: "post", action: exportUrl, hidden: "hidden"});
      [["csrfmiddlewaretoken", csrf], ["sql", current.sql], ["db", current.db], ["format", format.value]].forEach(function (pair) { f.appendChild(el("input", {type: "hidden", name: pair[0], value: pair[1]})); });
      document.body.appendChild(f); f.submit(); f.remove();
    });
    holder.appendChild(format); holder.appendChild(button);
    return holder;
  }

  function show(data) {
    area.textContent = "";
    if (!data.ok) { showError(data); return; }
    var panel = el("section", {"class": "panel pad sqlr-result", style: "margin-top:18px"});
    if (!data.columns.length) {                         // a statement with no rows to show
      panel.appendChild(el("p", {"class": "muted"}, (data.message || "Done") + " · " + number(data.ms) + " ms"));
      area.appendChild(panel); return;
    }
    var scroll = el("div", {"class": "scroll"}), table = el("table"), head = el("tr"), body = el("tbody");
    data.columns.forEach(function (name) { head.appendChild(el("th", {}, name)); });
    table.appendChild(el("thead")).appendChild(head);
    data.rows.forEach(function (row) {
      var tr = el("tr");
      row.forEach(function (value) { tr.appendChild(value === null ? el("td", {"class": "null"}, "NULL") : el("td", {"class": "cell"}, String(value))); });
      body.appendChild(tr);
    });
    if (!data.rows.length) { var empty = el("tr"); empty.appendChild(el("td", {colspan: String(data.columns.length), "class": "muted"}, "No rows.")); body.appendChild(empty); }
    table.appendChild(body); scroll.appendChild(table); panel.appendChild(scroll);
    var foot = el("div", {"class": "sqlr-foot"}), stats = el("div", {"class": "sqlr-stats muted"});
    stats.textContent = (data.total ? "Rows " + number(data.first) + "–" + number(data.last) + " of " + number(data.total) + (data.truncated ? "+" : "") : "0 rows") + " · " + number(data.ms) + " ms" +
      (data.truncated ? " · the result is limited, more rows exist" : "");
    foot.appendChild(stats); foot.appendChild(pager(data)); foot.appendChild(exporter());
    panel.appendChild(foot); area.appendChild(panel);
  }

  function finish() { busy = false; if (run) { run.disabled = false; run.textContent = "Run"; } }

  function load(page) {                                 // another page of the result on show
    if (busy || !current) { return; }
    busy = true;
    post(pageUrl, {token: current.token, page: page, sql: current.sql, db: current.db}).then(function (data) {
      if (data.ok && data.token) { current.token = data.token; }
      show(data);
    }).catch(function () { showError({error: "Could not reach PGDAS. Try again."}); }).then(function () { busy = false; });
  }

  form.addEventListener("submit", function (event) {
    if (event.submitter && event.submitter.id === "exportButton") { return; }
    var statement = box.value.trim();
    if (!statement || writes(statement)) { return; }    // an empty box falls back to the page's own answer; changes go to their review
    event.preventDefault();
    if (busy) { return; }
    busy = true;
    if (run) { run.disabled = true; run.textContent = "Running…"; }
    post(runUrl, {sql: statement, db: database()}).then(function (data) {
      clearOld();
      current = data.ok ? {sql: statement, db: database(), token: data.token || ""} : null;
      show(data);
    }).catch(function () { clearOld(); showError({error: "Could not reach PGDAS. Nothing was run."}); }).then(finish);
  });

  box.addEventListener("keydown", function (event) {    // Ctrl+Enter runs it
    if (event.ctrlKey && event.key === "Enter" && run && !run.disabled) { event.preventDefault(); run.click(); }
  });
})();
