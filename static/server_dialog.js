// Add / Edit server as a popup with four tabs (General, Connection, SSH tunnel, Connection string), a Test button, and a Save button that works only
// after a successful test of exactly what is typed (changing a connection detail switches Save off again until the next test; renaming or editing
// comments needs no test). Values are shown as text, never as HTML.
(function () {
  "use strict";
  var box = document.querySelector("[data-server-dialog-box]");
  if (!box) { return; }
  var form = box.querySelector("form");
  var status = box.querySelector("[data-status]");
  var testButton = box.querySelector("[data-server-test]");
  var saveButton = box.querySelector("[data-server-save]");
  var token = form.elements.test_token;
  var el = form.elements;
  var mode = "add", updateUrl = "", connectionDirty = false;
  var CONNECTION = ["host", "port", "database", "username", "kerberos", "krbsrvname", "sslmode", "save_password", "ssh_enabled", "ssh_host", "ssh_port",
                    "ssh_username", "ssh_auth", "ssh_prompt_passphrase", "password", "ssh_password", "ssh_key", "ssh_passphrase"];

  function csrf() { return (form.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || ""; }
  function say(text, kind) { status.textContent = text || ""; status.className = "dlg-status" + (kind ? " " + kind : ""); }
  function clearErrors() {
    box.querySelectorAll("[data-error-for]").forEach(function (e) { e.textContent = ""; });
    box.querySelectorAll(".dlg-tab").forEach(function (t) { t.classList.remove("has-error"); });
  }
  function showTab(name) {
    box.querySelectorAll(".dlg-tab").forEach(function (t) { t.classList.toggle("active", t.getAttribute("data-tab") === name); });
    box.querySelectorAll("[data-panel]").forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
  }
  function refreshSave() {
    var allowed = mode === "add" ? !!token.value : (connectionDirty ? !!token.value : true);
    saveButton.disabled = !allowed;
    saveButton.title = allowed ? "" : "Available after a successful test";
  }
  function untested() { token.value = ""; refreshSave(); }

  // What the tabs show depends on the switches: SSH tunnel, Kerberos, SSH sign-in type, Save password.
  function syncUi() {
    var ssh = el.ssh_enabled.checked, kerberos = el.kerberos.checked;
    box.querySelector("[data-ssh-fields]").hidden = !ssh;
    box.querySelectorAll("[data-ssh-auth]").forEach(function (row) { row.hidden = row.getAttribute("data-ssh-auth") !== el.ssh_auth.value; });
    box.querySelector("[data-host-label]").textContent = ssh ? "Host (as seen from the SSH server)" : "Host";
    box.querySelector("[data-host-hint]").hidden = !ssh;
    box.querySelectorAll("[data-kerberos-only]").forEach(function (row) { row.hidden = !kerberos; });
    box.querySelectorAll("[data-password-only]").forEach(function (row) { row.hidden = kerberos; });
    el.save_password.value = box.querySelector("#asSavePw").checked ? "1" : "0";
    refreshString();
  }
  function refreshString() {
    var host = el.host.value.trim(), user = el.username.value.trim();
    var text = "";
    if (host || user) {
      var shown = host.indexOf(":") >= 0 ? "[" + host + "]" : host;
      text = "postgresql://" + (user ? encodeURIComponent(user) + (el.password.value ? ":****" : "") + "@" : "") + shown +
        (el.port.value && el.port.value !== "5432" ? ":" + el.port.value : "") + "/" + (el.database.value.trim() ? encodeURIComponent(el.database.value.trim()) : "") +
        "?sslmode=" + el.sslmode.value;
    }
    box.querySelector("[data-current-string]").value = text;
  }

  function open(title) {
    form.reset(); token.value = ""; clearErrors(); say(""); showTab("general"); connectionDirty = false;
    el.server_id.value = ""; mode = "add"; updateUrl = "";
    box.querySelector("#asSavePw").checked = true;
    box.querySelector("[data-title]").textContent = title || "ADD — SERVER";
    box.querySelectorAll("input[type=password], textarea[name=ssh_key]").forEach(function (f) { f.placeholder = f.name === "ssh_key" ? "-----BEGIN OPENSSH PRIVATE KEY-----" : ""; });
    box.querySelector("[data-saved-key]").hidden = true;
    syncUi(); refreshSave();
    box.hidden = false;
    el.name.focus();
  }
  function close() { box.hidden = true; }

  function openEdit(jsonUrl, url) {
    open("EDIT — SERVER");
    say("Loading…");
    fetch(jsonUrl, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}}).then(function (r) { return r.json(); }).then(function (answer) {
      if (!answer.ok) { say(answer.error || "Could not load the server.", "err"); return; }
      mode = "edit"; updateUrl = url; el.server_id.value = answer.id;
      Object.keys(answer.values).forEach(function (name) {
        var field = el[name];
        if (!field) { return; }
        var value = answer.values[name];
        if (field.type === "checkbox") { field.checked = !!value; } else { field.value = value === null ? "" : value; }
      });
      box.querySelector("#asSavePw").checked = !!answer.values.save_password;
      var kept = "saved, leave blank to keep";
      if (answer.saved.password) { el.password.placeholder = kept; }
      if (answer.saved.ssh_secret) { el.ssh_password.placeholder = kept; el.ssh_key.placeholder = "(" + kept + ")"; }
      if (answer.saved.ssh_passphrase) { el.ssh_passphrase.placeholder = kept; }
      var keyNote = box.querySelector("[data-saved-key]");
      keyNote.hidden = !answer.saved.ssh_host_key;
      keyNote.textContent = answer.saved.ssh_host_key ? "Remembered SSH server key: " + answer.saved.ssh_host_key : "";
      say(""); syncUi(); refreshSave();
    }).catch(function () { say("Could not load the server.", "err"); });
  }

  function post(url) {
    return fetch(url, {method: "POST", credentials: "same-origin", body: new FormData(form),
                       headers: {"X-CSRFToken": csrf(), "X-Requested-With": "fetch"}})
      .then(function (response) { return response.json().catch(function () { return {ok: false, error: "The server answered something unexpected."}; }); });
  }
  function showErrors(answer) {
    var first = null;
    Object.keys(answer.errors || {}).forEach(function (name) {
      var slot = box.querySelector('[data-error-for="' + name + '"]');
      if (!slot) { return; }
      slot.textContent = answer.errors[name].join(" ");
      var panel = slot.closest("[data-panel]");
      if (panel) {
        var tab = box.querySelector('.dlg-tab[data-tab="' + panel.getAttribute("data-panel") + '"]');
        if (tab) { tab.classList.add("has-error"); }
        first = first || panel.getAttribute("data-panel");
      }
    });
    if (first) { showTab(first); }                                  // the first tab with a problem opens by itself
  }
  function busy(on) { testButton.disabled = on; if (on) { saveButton.disabled = true; } }

  document.addEventListener("click", function (event) {
    var adding = event.target.closest("[data-server-dialog]"), editing = event.target.closest("[data-server-edit]");
    if (adding) { event.preventDefault(); open(); }
    else if (editing) { event.preventDefault(); openEdit(editing.getAttribute("data-json-url"), editing.getAttribute("data-update-url")); }
  });
  box.addEventListener("click", function (event) {
    var tab = event.target.closest(".dlg-tab");
    if (tab) { showTab(tab.getAttribute("data-tab")); return; }
    if (event.target.closest("[data-dlg-close]") || event.target === box) { close(); }
  });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !box.hidden) { close(); } });
  box.querySelector("#asSavePw").addEventListener("change", function () { syncUi(); });
  ["ssh_enabled", "ssh_auth", "kerberos"].forEach(function (name) { el[name].addEventListener("change", syncUi); });
  function changed(event) {
    var name = event.target.name || (event.target.id === "asSavePw" ? "save_password" : "");
    if (CONNECTION.indexOf(name) >= 0) {
      connectionDirty = true;
      if (token.value) { say("Details changed: press Test connection again.", ""); }
      untested();
    }
    refreshString();
  }
  form.addEventListener("input", changed);
  form.addEventListener("change", changed);

  testButton.addEventListener("click", function () {
    clearErrors(); token.value = ""; say("Testing…", "");
    busy(true);
    post(box.getAttribute("data-test-url")).then(function (answer) {
      if (answer.ok) {
        token.value = answer.token;
        refreshSave();
        say(answer.message + (answer.ssh_fingerprint ? " SSH server key " + answer.ssh_fingerprint + " (remembered on save)." : "") + " You can save it now.", "ok");
      } else {
        showErrors(answer);
        say(answer.error || "The connection failed.", "err");
      }
    }).catch(function () { say("The request did not complete. Try again.", "err"); }).then(function () { testButton.disabled = false; refreshSave(); });
  });
  saveButton.addEventListener("click", function () {
    if (saveButton.disabled) { return; }
    saveButton.disabled = true; say("Saving…", "");
    post(mode === "edit" ? updateUrl : box.getAttribute("data-create-url")).then(function (answer) {
      if (answer.ok) { window.location.reload(); return; }
      showErrors(answer);
      say(answer.error || "Not saved.", "err");
      refreshSave();
    }).catch(function () { say("The request did not complete. Try again.", "err"); refreshSave(); });
  });

  // ---- Connection string tab: paste to fill (then the Connection tab opens), and the current string to copy ----
  var stringBox = box.querySelector("[data-connection-string]"), note = box.querySelector("[data-string-note]");
  function fill() {
    if (!window.pgdasParseConnection) { return; }
    var result = window.pgdasParseConnection(stringBox.value);
    if (result.error) { note.textContent = result.error; note.className = "dlg-note err"; return; }
    var labels = {host: "host", port: "port", username: "user", password: "password", sslmode: "SSL mode"}, done = [];
    Object.keys(result.values).forEach(function (name) {
      var field = el[name];
      if (!field) { return; }
      field.value = result.values[name];
      field.dispatchEvent(new Event("input", {bubbles: true}));    // a changed field switches Save off until the next test
      done.push(labels[name]);
    });
    stringBox.value = "";
    note.className = "dlg-note ok";
    note.textContent = "Filled in: " + done.join(", ") + "." + (result.ignored.length ? " Left out: " + result.ignored.join("; ") + "." : "") + " Check the Connection tab, then Test connection.";
    showTab("connection");
  }
  box.querySelector("[data-connection-fill]").addEventListener("click", fill);
  stringBox.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); fill(); } });
  stringBox.addEventListener("paste", function () { setTimeout(function () { if (/^\s*(jdbc:)?postgres(ql)?:\/\//i.test(stringBox.value)) { fill(); } }, 0); });
  box.querySelector("[data-current-copy]").addEventListener("click", function () {
    var field = box.querySelector("[data-current-string]");
    field.select();
    if (navigator.clipboard && field.value) { navigator.clipboard.writeText(field.value); }
  });

  // ---- reading a key file in the browser (the file itself is never sent anywhere; its text goes into the box) ----
  var picker = box.querySelector("[data-key-file]");
  if (picker) {
    picker.addEventListener("change", function () {
      var file = picker.files && picker.files[0];
      if (!file) { return; }
      var slot = box.querySelector('[data-error-for="ssh_key"]');
      if (file.size > 65536) { slot.textContent = "That file is too large to be a private key."; picker.value = ""; return; }
      var reader = new FileReader();
      reader.onload = function () {
        el.ssh_key.value = String(reader.result || "");
        slot.textContent = "";
        el.ssh_key.dispatchEvent(new Event("input", {bubbles: true}));
        picker.value = "";
      };
      reader.readAsText(file);
    });
  }
})();

// ---- Unlock / Lock for a server whose password or SSH key passphrase is not saved ----
(function () {
  "use strict";
  var box = document.querySelector("[data-unlock-box]");
  if (!box) { return; }
  var form = box.querySelector("form"), status = box.querySelector("[data-status]"), current = null;
  function csrf() { return (form.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || ""; }
  function say(text, kind) { status.textContent = text || ""; status.className = "dlg-status" + (kind ? " " + kind : ""); }
  function send(url, body) {
    return fetch(url, {method: "POST", credentials: "same-origin", body: body, headers: {"X-CSRFToken": csrf(), "X-Requested-With": "fetch"}})
      .then(function (r) { return r.json().catch(function () { return {ok: false, error: "The server answered something unexpected."}; }); });
  }
  document.addEventListener("click", function (event) {
    var open = event.target.closest("[data-server-unlock]"), lock = event.target.closest("[data-server-lock]");
    if (open) {
      current = open.getAttribute("data-url");
      var wantsPassword = open.hasAttribute("data-needs-password"), wantsPassphrase = open.hasAttribute("data-needs-passphrase");
      box.querySelector("[data-unlock-intro]").textContent = "Enter " + (wantsPassword && wantsPassphrase ? "the password and the SSH key passphrase" : wantsPassword ? "the password" : "the passphrase of the SSH key") +
        " of " + open.getAttribute("data-name") + ". It is tested with a real connection, kept (encrypted) for a few hours so employees, backups and alerts can use the server, then deleted: the server locks again.";
      form.reset(); say("");
      box.querySelector('[data-unlock-field="password"]').hidden = !wantsPassword;
      box.querySelector('[data-unlock-field="passphrase"]').hidden = !wantsPassphrase;
      box.hidden = false; (wantsPassword ? form.elements.password : form.elements.passphrase).focus();
    } else if (lock) {
      send(lock.getAttribute("data-url"), new FormData()).then(function () { window.location.reload(); });
    }
  });
  box.addEventListener("click", function (event) {
    if (event.target.closest("[data-dlg-close]") || event.target === box) { box.hidden = true; return; }
    if (!event.target.closest("[data-unlock-go]")) { return; }
    say("Checking…");
    send(current, new FormData(form)).then(function (answer) {
      if (answer.ok) { window.location.reload(); } else { say(answer.error || "Not unlocked.", "err"); }
    }).catch(function () { say("The request did not complete. Try again.", "err"); });
  });
  form.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); box.querySelector("[data-unlock-go]").click(); } });
})();