// The DELETE flow of the SQL runner:  Run → preview (nothing is changed) → warning popup → type ZAP → delete.
// Only runs on servers that allow deleting rows and for staff users (the page does not include this script otherwise).
// Row values are always inserted as text, never as HTML.
(function () {
  var form = document.getElementById("sqlForm");
  if (!form || form.dataset.canDelete !== "1") return;
  var textarea = document.getElementById("sql");
  var runButton = document.getElementById("runButton");
  var resultBox = document.getElementById("deleteResult");
  var modal = document.getElementById("deleteModal");
  var confirmInput = document.getElementById("deleteConfirm");
  var goButton = document.getElementById("deleteGo");
  var errorLine = document.getElementById("deleteError");
  var CONFIRM = form.dataset.confirmWord;
  var csrf = form.querySelector("input[name=csrfmiddlewaretoken]").value;
  var db = form.querySelector("input[name=db]").value;
  var checked = null;       // what the person is being asked to confirm: {sql, count, side_effects}

  function tone(type) { if (window.pgdeckSound) window.pgdeckSound.play(type); }

  function startsWithDelete(text) {
    var bare = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/--[^\n]*/g, "").trim();
    return /^delete\b/i.test(bare);
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf, "X-Requested-With": "fetch"},
      body: JSON.stringify(body),
    }).then(function (response) {
      return response.json().then(function (json) { return {ok: response.ok && json.ok, body: json}; });
    });
  }

  function showMessage(kind, text, linkUrl, linkText) {
    resultBox.innerHTML = "";
    var list = document.createElement("ul");
    list.className = "messages";
    var item = document.createElement("li");
    item.className = kind;
    item.textContent = text + " ";
    if (linkUrl) {
      var link = document.createElement("a");
      link.href = linkUrl;
      link.textContent = linkText;
      item.appendChild(link);
    }
    list.appendChild(item);
    resultBox.appendChild(list);
  }

  function closeModal() { modal.hidden = true; checked = null; confirmInput.value = ""; goButton.disabled = true; errorLine.hidden = true; }

  function cell(tag, text) {
    var element = document.createElement(tag);
    if (text === null) { element.textContent = "NULL"; element.className = "null"; }
    else { element.textContent = typeof text === "object" ? JSON.stringify(text) : String(text); }
    return element;
  }

  function fillSample(preview) {
    var head = document.getElementById("deleteSampleHead"), body = document.getElementById("deleteSampleBody");
    head.innerHTML = ""; body.innerHTML = "";
    document.getElementById("deleteSampleBox").hidden = !preview.sample.length;
    if (!preview.sample.length) return;
    var headRow = document.createElement("tr");
    preview.columns.forEach(function (name) { headRow.appendChild(cell("th", name)); });
    head.appendChild(headRow);
    preview.sample.forEach(function (row) {
      var tr = document.createElement("tr");
      preview.columns.forEach(function (name) { var td = cell("td", row[name]); td.className = (td.className + " cell").trim(); tr.appendChild(td); });
      body.appendChild(tr);
    });
  }

  function fillNotes(preview) {
    var notes = document.getElementById("deleteNotes");
    notes.innerHTML = "";
    function note(text, bad) { var p = document.createElement("p"); p.className = bad ? "warn-line" : "muted"; p.textContent = text; notes.appendChild(p); }
    if (!preview.has_where) note("This statement has no WHERE clause: it deletes EVERY row in the table.", true);
    preview.side_effects.forEach(function (effect) {
      var parts = [];
      if (effect.deleted) parts.push(effect.deleted + " deleted");
      if (effect.updated) parts.push(effect.updated + " updated");
      if (effect.inserted) parts.push(effect.inserted + " inserted");
      note("It also changes " + effect.table + " (" + parts.join(", ") + ") through cascades or triggers. Those rows are not saved in the audit log and cannot be re-inserted from it.", true);
    });
    if (preview.count > preview.sample.length) note("Showing the first " + preview.sample.length + " of " + preview.count + " rows. All of them are saved in the audit log.");
  }

  function openModal(preview) {
    checked = {sql: textarea.value, count: preview.count, side_effects: preview.side_effects};
    var plural = preview.count === 1 ? "record" : "records";
    document.getElementById("deleteCount").textContent = preview.count.toLocaleString();
    document.getElementById("deleteMessage").textContent = preview.count === 0
      ? "No rows match, so there is nothing to delete."
      : "will be permanently deleted from " + preview.schema + "." + preview.table + ".";
    document.getElementById("deleteTitle").textContent = preview.count === 0 ? "DELETE — NOTHING TO DELETE" : "DELETE " + preview.count.toLocaleString() + " " + plural.toUpperCase() + "?";
    document.getElementById("deleteStatement").textContent = preview.statement;
    document.getElementById("deleteConfirmBox").hidden = preview.count === 0;
    goButton.hidden = preview.count === 0;
    goButton.textContent = "Delete " + preview.count.toLocaleString() + " " + plural;
    fillSample(preview);
    fillNotes(preview);
    errorLine.hidden = true;
    confirmInput.value = "";
    goButton.disabled = true;
    modal.hidden = false;
    tone("error");
    (preview.count === 0 ? document.getElementById("deleteCancel") : confirmInput).focus();
  }

  // The delete button works only when the box holds exactly the word: same letters, same case, nothing else.
  confirmInput.addEventListener("input", function () { goButton.disabled = confirmInput.value !== CONFIRM; });
  confirmInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter") { event.preventDefault(); if (!goButton.disabled) goButton.click(); }
  });

  form.addEventListener("submit", function (event) {
    if (!startsWithDelete(textarea.value)) return;          // anything else runs read-only, as before
    event.preventDefault();
    resultBox.innerHTML = "";
    runButton.disabled = true;
    runButton.textContent = "Checking…";
    post(form.dataset.previewUrl, {sql: textarea.value, db: db}).then(function (response) {
      if (!response.ok) { showMessage("error", response.body.error || "Could not check this statement."); tone("error"); return; }
      openModal(response.body);
    }).catch(function () {
      showMessage("error", "Could not reach PgDeck to check this statement.");
      tone("error");
    }).then(function () { runButton.disabled = false; runButton.textContent = "Run"; });
  });

  goButton.addEventListener("click", function () {
    if (!checked || confirmInput.value !== CONFIRM) return;
    goButton.disabled = true;
    goButton.textContent = "Deleting…";
    post(form.dataset.executeUrl, {sql: checked.sql, db: db, expected_count: checked.count,
                                    expected_side_effects: checked.side_effects, confirm: confirmInput.value}).then(function (response) {
      if (response.ok) {
        var done = response.body;
        closeModal();
        showMessage("success", "Deleted " + done.count.toLocaleString() + " row" + (done.count === 1 ? "" : "s") + " from " + done.schema + "." + done.table +
                    ". Saved in the audit log as entry #" + done.entry_id + ", where it can be re-inserted.", form.dataset.auditUrl || "/audit/", "Open the audit log");
        tone("success");
      } else {
        errorLine.textContent = response.body.error || "The delete did not go through.";
        errorLine.hidden = false;
        tone("error");
        if (/changed since you checked|more than/i.test(errorLine.textContent)) {      // the person must look again
          closeModal();
          showMessage("error", response.body.error);
        }
      }
    }).catch(function () {
      errorLine.textContent = "Could not reach PgDeck. Nothing was deleted.";
      errorLine.hidden = false;
    }).then(function () {
      if (checked) { goButton.textContent = "Delete " + checked.count.toLocaleString() + " " + (checked.count === 1 ? "record" : "records"); goButton.disabled = confirmInput.value !== CONFIRM; }
    });
  });

  document.getElementById("deleteCancel").addEventListener("click", closeModal);
  document.getElementById("deleteClose").addEventListener("click", closeModal);
  modal.addEventListener("click", function (event) { if (event.target === modal) closeModal(); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !modal.hidden) closeModal(); });
})();
