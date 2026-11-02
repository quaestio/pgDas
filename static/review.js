// The review popup for a changing statement, shared by the SQL runner and the Create / Rename / Delete dialogs.
//   pgdasReview.start({server, db, sql, onError(message), onSuccess(result)})
// asks PGDAS what the statement would do (nothing changes: the server rolls the check back), shows the popup with
// the facts and the warning, and runs it for real only when the person confirms (typing the word when one is needed).
// Values from the database are inserted as text, never as HTML.
(function () {
  var modal = document.getElementById("changeModal");
  if (!modal) return;
  var confirmInput = document.getElementById("changeConfirm");
  var confirmBox = document.getElementById("changeConfirmBox");
  var goButton = document.getElementById("changeGo");
  var errorLine = document.getElementById("changeError");
  var csrf = (modal.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";
  var checked = null;        // what the person is being asked to confirm: {request, fingerprint, word, button}

  function tone(type) { if (window.pgdasSound) window.pgdasSound.play(type); }

  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf, "X-Requested-With": "fetch"},
      body: JSON.stringify(body),
    }).then(function (response) {
      return response.json().then(function (json) { return {ok: response.ok && json.ok, body: json}; });
    });
  }

  function urls(server) {
    return {previewUrl: modal.dataset.previewUrl.replace("/c/0/", "/c/" + server + "/"),
            executeUrl: modal.dataset.executeUrl.replace("/c/0/", "/c/" + server + "/"),
            auditUrl: modal.dataset.auditUrl};
  }

  function cell(tag, value) {
    var element = document.createElement(tag);
    if (value === null) { element.textContent = "NULL"; element.className = "null"; }
    else { element.textContent = typeof value === "object" ? JSON.stringify(value) : String(value); }
    return element;
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

  function openModal(request, preview) {
    var nothing = !preview.button;                     // e.g. an UPDATE or DELETE that matches no rows
    checked = {request: request, fingerprint: preview.fingerprint, word: preview.confirm_word, button: preview.button};
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

  goButton.addEventListener("click", function () {
    if (!checked || (checked.word && confirmInput.value !== checked.word)) return;
    var request = checked.request;
    goButton.disabled = true;
    goButton.textContent = "Working…";
    post(request.executeUrl, {sql: request.sql, db: request.db, fingerprint: checked.fingerprint, confirm: confirmInput.value}).then(function (response) {
      if (response.ok) {
        closeModal();
        tone("success");
        if (request.onSuccess) request.onSuccess(response.body);
        return;
      }
      errorLine.textContent = response.body.error || "The change did not go through.";
      errorLine.hidden = false;
      tone("error");
      if (/changed since you checked|more than/i.test(errorLine.textContent)) {   // the person must look again
        var message = response.body.error;
        closeModal();
        if (request.onError) request.onError(message);
      }
    }).catch(function () {
      errorLine.textContent = "Could not reach PGDAS. Nothing was changed.";
      errorLine.hidden = false;
    }).then(function () {
      if (checked) { goButton.textContent = checked.button; goButton.disabled = !!checked.word && confirmInput.value !== checked.word; }
    });
  });

  document.getElementById("changeCancel").addEventListener("click", closeModal);
  document.getElementById("changeClose").addEventListener("click", closeModal);
  modal.addEventListener("click", function (event) { if (event.target === modal) closeModal(); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !modal.hidden) { event.stopPropagation(); closeModal(); } }, true);

  window.pgdasReview = {
    urls: urls,
    post: post,
    start: function (options) {
      var u = urls(options.server);
      var request = {sql: options.sql, db: options.db || "", previewUrl: u.previewUrl, executeUrl: u.executeUrl,
                     onSuccess: options.onSuccess, onError: options.onError};
      return post(u.previewUrl, {sql: request.sql, db: request.db}).then(function (response) {
        if (!response.ok) { if (options.onError) options.onError(response.body.error || "That statement could not be run."); return; }
        openModal(request, response.body);
      }).catch(function () {
        if (options.onError) options.onError("Could not reach PGDAS to run this statement.");
      });
    },
  };
})();
