// SQL runner: "My saved statements" (kept on the server, per person and server) and keeping "My recent statements" up to date after a run
// that did not reload the page. Everything from the server is put in the page as text.
(function () {
  "use strict";
  var section = document.getElementById("sqlSavedSection"), box = document.getElementById("sql");
  if (!box) { return; }
  var form = document.getElementById("sqlForm");
  var csrf = (form && form.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function call(url, method, body) {
    return fetch(url, {method: method, credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": csrf, "X-Requested-With": "fetch"},
                       body: body ? JSON.stringify(body) : undefined}).then(function (r) { return r.json().catch(function () { return {ok: false, error: "The answer was not understood."}; }); });
  }

  // ---- the recent statements ------------------------------------------------------------------------------------------------------------
  window.pgdasHistoryAdd = function (statement, ok) {
    var holder = document.getElementById("sqlHistorySection"), list = holder && holder.querySelector("ul.history-list"), title = document.getElementById("sqlHistoryTitle");
    if (!list) { return; }
    var plain = function (s) { return s.split(/\s+/).join(" ").trim(); }, now = new Date(), pad = function (n) { return (n < 10 ? "0" : "") + n; };
    Array.prototype.slice.call(list.querySelectorAll("li")).forEach(function (li) {              // each statement once: the newest copy stays
      var b = li.querySelector("button");
      if (b && plain(b.dataset.sql) === plain(statement)) { li.remove(); }
    });
    var item = el("li"), button = el("button", {type: "button", "class": "btn history-item", style: "width:100%;text-align:left;margin-bottom:6px"});
    button.dataset.sql = statement;
    button.appendChild(el("span", {"class": "muted"}, now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()) + " " + pad(now.getHours()) + ":" + pad(now.getMinutes()) + " "));
    if (!ok) { button.appendChild(el("span", {"class": "badge bad"}, "failed")); button.appendChild(document.createTextNode(" ")); }
    var shown = plain(statement); button.appendChild(el("code", {}, shown.length > 110 ? shown.slice(0, 109) + "…" : shown));
    item.appendChild(button);
    list.insertBefore(item, list.firstChild);
    while (list.children.length > 20) { list.removeChild(list.lastChild); }
    holder.hidden = false;
    if (title) { title.textContent = "My recent statements on this server (" + list.children.length + ")"; }
  };

  if (!section) { return; }

  // ---- the saved statements -------------------------------------------------------------------------------------------------------------
  var url = section.getAttribute("data-url"), list = document.getElementById("sqlSavedList"), title = document.getElementById("sqlSavedTitle");
  var name = document.getElementById("sqlSaveName"), save = document.getElementById("sqlSaveButton"), message = document.getElementById("sqlSaveMessage");

  function render(queries) {
    list.textContent = "";
    title.textContent = "My saved statements (" + queries.length + ")";
    queries.forEach(function (q) {
      var item = el("li", {style: "display:flex;gap:6px;margin-bottom:6px"}), open = el("button", {type: "button", "class": "btn saved-item", style: "flex:1;text-align:left"});
      open.dataset.sql = q.sql;
      open.appendChild(el("strong", {}, q.name));
      open.appendChild(el("span", {"class": "muted"}, "  " + q.saved + "  "));
      var shown = q.sql.split(/\s+/).join(" ").trim(); open.appendChild(el("code", {}, shown.length > 90 ? shown.slice(0, 89) + "…" : shown));
      var remove = el("button", {type: "button", "class": "btn danger", title: "Delete this saved statement", "aria-label": "Delete " + q.name}, "✕");
      remove.addEventListener("click", function () {
        call(url + q.id + "/delete/", "POST").then(function (a) { if (a.ok) { load(); } else { message.textContent = a.error || "Could not delete it."; } });
      });
      item.appendChild(open); item.appendChild(remove); list.appendChild(item);
    });
    if (!queries.length) { list.appendChild(el("li", {"class": "muted"}, "Nothing saved yet.")); }
  }
  function load() {
    call(url, "GET").then(function (a) { if (a.ok) { render(a.queries); } }).catch(function () { message.textContent = "Could not read your saved statements."; });
  }
  save.addEventListener("click", function () {
    message.textContent = "";
    call(url, "POST", {name: name.value, sql: box.value}).then(function (a) {
      if (!a.ok) { message.textContent = a.error || "Could not save it."; return; }
      render(a.queries); name.value = ""; message.textContent = "Saved as “" + a.query.name + "”.";
      document.getElementById("sqlSaved").open = true;
    }).catch(function () { message.textContent = "Could not reach PGDAS. Nothing was saved."; });
  });
  name.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); save.click(); } });
  load();
})();
