/* Maintenance buttons (Vacuum / Analyze / Reindex) on a table's page. */
(function () {
  "use strict";
  function cookie(name) {
    var found = document.cookie.split("; ").filter(function (item) { return item.indexOf(name + "=") === 0; })[0];
    return found ? decodeURIComponent(found.split("=")[1]) : "";
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-maint]");
    if (!button) { return; }
    var box = button.closest("[data-maintenance]");
    if (!box) { return; }
    var where;
    try { where = JSON.parse(box.getAttribute("data-maintenance")); } catch (e) { return; }
    var operation = button.getAttribute("data-maint");
    var status = box.querySelector(".maintenance-status");
    if (!window.confirm("Run " + button.textContent.trim().toUpperCase() + " on " + where.schema + "." + where.table + "? It can take a while on a large table.")) { return; }
    var buttons = box.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });
    status.textContent = " Running…";
    fetch(where.url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken"), "X-Requested-With": "fetch"},
      body: JSON.stringify({db: where.db, schema: where.schema, table: where.table, operation: operation})
    }).then(function (response) { return response.json(); }).then(function (answer) {
      status.textContent = " " + (answer.ok ? answer.message : (answer.error || "Not done."));
      status.className = "maintenance-status " + (answer.ok ? "ok" : "error");
    }).catch(function () {
      status.textContent = " The request did not complete.";
      status.className = "maintenance-status error";
    }).then(function () { buttons.forEach(function (b) { b.disabled = false; }); });
  });
})();