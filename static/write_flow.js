// Changing data from the SQL runner. Only included for staff users on servers that allow changes.
//   INSERT                  → runs at once.
//   UPDATE                  → preview (nothing changes) → popup with the number of rows and a warning → button.
//   DELETE / TRUNCATE / DROP → preview → popup with a warning → type ZAP / TRUNCATE / DROP → go.
//   CREATE / ALTER          → preview → popup listing exactly what would be created or changed → button
//                             (an ALTER that is risky asks for the word ALTER first).
// Anything else (SELECT, ...) is submitted normally and runs read-only. Values from the database are inserted as text,
// never as HTML.
(function () {
  var form = document.getElementById("sqlForm");
  if (!form || form.dataset.canWrite !== "1") return;
  var textarea = document.getElementById("sql");
  var runButton = document.getElementById("runButton");
  var resultBox = document.getElementById("changeResult");
  var modal = document.getElementById("changeModal");
  var confirmInput = document.getElementById("changeConfirm");
  var confirmBox = document.getElementById("changeConfirmBox");
  var goButton = document.getElementById("changeGo");
  var errorLine = document.getElementById("changeError");
  var csrf = form.querySelector("input[name=csrfmiddlewaretoken]").value;
  var db = form.querySelector("input[name=db]").value;
  var checked = null;        // what the person is being asked to confirm: {sql, fingerprint, word, button}

  function tone(type) { if (window.pgdeckSound) window.pgdeckSound.play(type); }

  function kindOf(text) {
    var bare = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/--[^\n]*/g, "").trim();
    var match = /^(insert|update|delete|truncate|drop|create|alter)\b/i.exec(bare);
    return match ? match[1].toLowerCase() : null;
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

  function cell(tag, value) {
    var element = document.createElement(tag);
    if (value === null) { element.textContent = "NULL"; element.className = "null"; }
    else { element.textContent = typeof value === "object" ? JSON.stringify(value) : String(value); }
    return element;
  }

  function showMessage(kind, text, extra) {
    resultBox.innerHTML = "";
    var list = document.createElement("ul");
    list.className = "messages";
    var item = document.createElement("li");
    item.className = kind;
    item.textContent = text + " ";
    if (extra && extra.url) {
      var link = document.createElement("a");
      link.href = extra.url;
      link.textContent = extra.text;
      item.appendChild(link);
    }
    list.appendChild(item);
    resultBox.appendChild(list);
    if (extra && extra.table) {                       // INSERT ... RETURNING: show what came back
      var wrap = document.createElement("section");
      wrap.className = "panel pad scroll";
      wrap.style.marginTop = "12px";
      var table = document.createElement("table"), head = document.createElement("tr");
      extra.table.columns.forEach(function (name) { head.appendChild(cell("th", name)); });
      var thead = document.createElement("thead"), tbody = document.createElement("tbody");
      thead.appendChild(head);
      extra.table.rows.forEach(function (row) {
        var tr = document.createElement("tr");
        row.forEach(function (value) { var td = cell("td", value); td.className = (td.className + " cell").trim(); tr.appendChild(td); });
        tbody.appendChild(tr);
      });
      table.appendChild(thead); table.appendChild(tbody); wrap.appendChild(table);
      resultBox.appendChild(wrap);
    }
  }

  function closeModal() {
    modal.hidden = true; checked = null; confirmInput.value = ""; goButton.disabled = true; errorLine.hidden = true;
  }

  function text(id, value) { document.getElementById(id).textContent = value; }

  function fillSample(preview) {
    var head = document.getElementById("changeSampleHead"), body = document.getElementById("changeSampleBody");
    head.innerHTML = ""; body.innerHTML = "";
    var rows = preview.sample || [];
    document.getElementById("changeSampleBox").hidden = !rows.length;
    if (!rows.length) return;
    var headRow = document.createElement("tr");
    preview.columns.forEach(function (name) { headRow.appendChild(cell("th", name)); });
    head.appendChild(headRow);
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      preview.columns.forEach(function (name) { var td = cell("td", row[name]); td.className = (td.className + " cell").trim(); tr.appendChild(td); });
      body.appendChild(tr);
    });
  }

  function fillObjects(preview) {
    var list = document.getElementById("changeObjects");
    list.innerHTML = "";
    var objects = preview.objects || [];
    list.hidden = !objects.length;
    objects.forEach(function (object) {
      var item = document.createElement("li");
      // CREATE / ALTER send ready-made text ("+ create table public.x"); the others send a name and a row count.
      item.textContent = object.text ? object.text :
        object.name + " — " + Number(object.rows).toLocaleString() + " row" + (object.rows === 1 ? "" : "s") +
        (object.cascade ? " (also emptied by CASCADE)" : "");
      list.appendChild(item);
    });
  }

  function fillNotes(preview) {
    var notes = document.getElementById("changeNotes");
    notes.innerHTML = "";
    (preview.notes || []).forEach(function (note) {
      var p = document.createElement("p");
      p.className = note.bad ? "warn-line" : "muted";
      p.textContent = note.text;
      notes.appendChild(p);
    });
  }

  function openModal(preview) {
    var nothing = !preview.button;                     // e.g. an UPDATE or DELETE that matches no rows
    checked = {sql: textarea.value, fingerprint: preview.fingerprint, word: preview.confirm_word, button: preview.button};
    text("changeTitle", preview.title);
    text("changeCount", preview.headline);
    text("changeLabel", preview.headline_label);
    text("changeWarning", nothing ? "" : preview.warning);
    document.getElementById("changeWarning").hidden = nothing;
    text("changeStatement", preview.statement);
    fillNotes(preview); fillObjects(preview); fillSample(preview);
    confirmBox.hidden = nothing || !preview.confirm_word;
    if (preview.confirm_word) text("changeWord", preview.confirm_word);
    goButton.hidden = nothing;
    goButton.textContent = preview.button || "";
    errorLine.hidden = true;
    confirmInput.value = "";
    goButton.disabled = !nothing && !!preview.confirm_word;     // with a word to type, the button waits for it
    modal.hidden = false;
    tone("error");
    (nothing ? document.getElementById("changeCancel") : (preview.confirm_word ? confirmInput : goButton)).focus();
  }

  // With a word to type, the button works only when the box holds exactly that word: same letters, same case, nothing else.
  confirmInput.addEventListener("input", function () { goButton.disabled = !checked || confirmInput.value !== checked.word; });
  confirmInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter") { event.preventDefault(); if (!goButton.disabled) goButton.click(); }
  });

  function failed(message) { showMessage("error", message); tone("error"); }

  form.addEventListener("submit", function (event) {
    var kind = kindOf(textarea.value);
    if (!kind) return;                                    // anything else runs read-only, as before
    event.preventDefault();
    resultBox.innerHTML = "";
    runButton.disabled = true;
    runButton.textContent = kind === "insert" ? "Inserting…" : "Checking…";
    var url = kind === "insert" ? form.dataset.executeUrl : form.dataset.previewUrl;
    post(url, {sql: textarea.value, db: db}).then(function (response) {
      if (!response.ok) { failed(response.body.error || "That statement could not be run."); return; }
      if (kind === "insert") {
        showMessage("success", response.body.message, {url: form.dataset.auditUrl, text: "Open the audit log", table: response.body.returned});
        tone("success");
      } else {
        openModal(response.body);
      }
    }).catch(function () {
      failed("Could not reach PgDeck to run this statement.");
    }).then(function () { runButton.disabled = false; runButton.textContent = "Run"; });
  });

  goButton.addEventListener("click", function () {
    if (!checked || (checked.word && confirmInput.value !== checked.word)) return;
    goButton.disabled = true;
    goButton.textContent = "Working…";
    post(form.dataset.executeUrl, {sql: checked.sql, db: db, fingerprint: checked.fingerprint, confirm: confirmInput.value}).then(function (response) {
      if (response.ok) {
        closeModal();
        showMessage("success", response.body.message, {url: form.dataset.auditUrl, text: "Open the audit log"});
        tone("success");
        return;
      }
      errorLine.textContent = response.body.error || "The change did not go through.";
      errorLine.hidden = false;
      tone("error");
      if (/changed since you checked|more than/i.test(errorLine.textContent)) {   // the person must look again
        closeModal();
        showMessage("error", response.body.error);
      }
    }).catch(function () {
      errorLine.textContent = "Could not reach PgDeck. Nothing was changed.";
      errorLine.hidden = false;
    }).then(function () {
      if (checked) { goButton.textContent = checked.button; goButton.disabled = !!checked.word && confirmInput.value !== checked.word; }
    });
  });

  document.getElementById("changeCancel").addEventListener("click", closeModal);
  document.getElementById("changeClose").addEventListener("click", closeModal);
  modal.addEventListener("click", function (event) { if (event.target === modal) closeModal(); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !modal.hidden) closeModal(); });
})();
