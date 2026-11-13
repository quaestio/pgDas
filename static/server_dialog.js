// Add server as a popup: Connection and SSH tunnel tabs, a Test button, and a Save button that works only after a successful test of exactly
// what is typed (changing any field switches Save off again until the next test). Values are shown as text, never as HTML.
(function () {
  "use strict";
  var box = document.querySelector("[data-server-dialog-box]");
  if (!box) { return; }
  var form = box.querySelector("form");
  var status = box.querySelector("[data-status]");
  var testButton = box.querySelector("[data-server-test]");
  var saveButton = box.querySelector("[data-server-save]");
  var token = form.elements.test_token;
  var sshOn = form.elements.ssh_enabled, sshAuth = form.elements.ssh_auth;

  function csrf() { return (form.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || ""; }
  function say(text, kind) { status.textContent = text || ""; status.className = "dlg-status" + (kind ? " " + kind : ""); }
  function clearErrors() { box.querySelectorAll("[data-error-for]").forEach(function (e) { e.textContent = ""; }); }
  function showTab(name) {
    box.querySelectorAll(".dlg-tab").forEach(function (t) { t.classList.toggle("active", t.getAttribute("data-tab") === name); });
    box.querySelectorAll("[data-panel]").forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
  }
  function syncSsh() {
    box.querySelector("[data-ssh-fields]").hidden = !sshOn.checked;
    box.querySelectorAll("[data-ssh-auth]").forEach(function (row) { row.hidden = row.getAttribute("data-ssh-auth") !== sshAuth.value; });
  }
  function untested() {
    token.value = "";
    saveButton.disabled = true;
    saveButton.title = "Available after a successful test";
  }
  function open() {
    form.reset(); token.value = ""; clearErrors(); say(""); showTab("connection"); syncSsh(); untested();
    box.hidden = false;
    form.elements.name.focus();
  }
  function close() { box.hidden = true; }
  function post(url) {
    return fetch(url, {method: "POST", credentials: "same-origin", body: new FormData(form),
                       headers: {"X-CSRFToken": csrf(), "X-Requested-With": "fetch"}})
      .then(function (response) { return response.json().catch(function () { return {ok: false, error: "The server answered something unexpected."}; }); });
  }
  function showErrors(answer) {
    var first = null;
    Object.keys(answer.errors || {}).forEach(function (name) {
      var slot = box.querySelector('[data-error-for="' + name + '"]');
      if (slot) { slot.textContent = answer.errors[name].join(" "); first = first || name; }
    });
    if (first) { showTab(first.indexOf("ssh_") === 0 ? "ssh" : "connection"); }
  }
  function busy(on) { testButton.disabled = on; if (on) { saveButton.disabled = true; } }

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-server-dialog]")) { event.preventDefault(); open(); }
  });
  box.addEventListener("click", function (event) {
    var tab = event.target.closest(".dlg-tab");
    if (tab) { showTab(tab.getAttribute("data-tab")); return; }
    if (event.target.closest("[data-dlg-close]") || event.target === box) { close(); }
  });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !box.hidden) { close(); } });
  sshOn.addEventListener("change", syncSsh);
  sshAuth.addEventListener("change", syncSsh);
  form.addEventListener("input", function () { if (token.value) { untested(); say("Details changed: press Test connection again.", ""); } });
  form.addEventListener("change", function () { if (token.value) { untested(); say("Details changed: press Test connection again.", ""); } });

  testButton.addEventListener("click", function () {
    clearErrors(); token.value = ""; say("Testing…", "");
    busy(true);
    post(box.getAttribute("data-test-url")).then(function (answer) {
      if (answer.ok) {
        token.value = answer.token;
        saveButton.disabled = false; saveButton.title = "";
        say(answer.message + (answer.ssh_fingerprint ? " SSH server key " + answer.ssh_fingerprint + " (remembered on save)." : "") + " You can save it now.", "ok");
      } else {
        showErrors(answer);
        say(answer.error || "The connection failed.", "err");
      }
    }).catch(function () { say("The request did not complete. Try again.", "err"); }).then(function () { testButton.disabled = false; });
  });
  saveButton.addEventListener("click", function () {
    if (!token.value) { return; }
    saveButton.disabled = true; say("Saving…", "");
    post(box.getAttribute("data-create-url")).then(function (answer) {
      if (answer.ok) { window.location.reload(); return; }
      showErrors(answer);
      say(answer.error || "Not saved.", "err");
      if (token.value) { saveButton.disabled = false; }
    }).catch(function () { say("The request did not complete. Try again.", "err"); saveButton.disabled = false; });
  });
})();