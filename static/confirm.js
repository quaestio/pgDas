// A warning popup that makes you TYPE a name before something is deleted, in place of the browser's plain OK / Cancel box.
// A form opts in with data attributes:
//
//   <form method="post" action="..." data-typed-confirm="the name to type" data-confirm-title="DELETE BACKUP"
//         data-confirm-button="Delete backup" data-confirm-prompt="Type the database name to confirm" data-confirm-message="..."
//         data-confirm-detail="..." data-confirm-warning="...">
//     <input type="hidden" name="confirm" value=""> ... <button type="submit">Delete</button></form>
//
// Pressing the button opens the popup (with a warning sound; the speaker button in the header mutes it). The prompt does not repeat
// the name: the person has to type it. A line under the box says how they are doing. The red button stays disabled until the typed
// text is exactly the name, case included; then the form is sent with the typed text as `confirm`, which the server checks again,
// so skipping this script deletes nothing. Everything is put into the page as text, never as HTML.
(function () {
  var overlay = null, parts = null, pending = null;

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function build() {
    overlay = make("div", "cyber-modal");
    overlay.hidden = true;
    var dialog = make("div", "cyber-dialog wide confirm-dialog");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "confirmTitle");
    var top = make("div", "modal-top"), title = make("span", null, "CONFIRM");
    title.id = "confirmTitle";
    var close = make("button", null, "✕");
    close.type = "button"; close.setAttribute("aria-label", "Close");
    top.appendChild(title); top.appendChild(close);

    var message = make("p", "confirm-message"), detail = make("p", "confirm-detail"), warning = make("div", "warn-box");
    var label = make("label");
    label.setAttribute("for", "confirmInput");
    var input = make("input", "confirm-name");
    input.type = "text"; input.id = "confirmInput"; input.autocomplete = "off"; input.spellcheck = false; input.setAttribute("autocapitalize", "off");
    var status = make("p", "confirm-status idle");
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");

    var buttons = make("div", "modal-buttons"), cancel = make("button", "btn", "Cancel"), go = make("button", "btn danger", "Delete");
    cancel.type = "button"; go.type = "button"; go.disabled = true;
    buttons.appendChild(cancel); buttons.appendChild(go);

    [top, make("div", "modal-icon", "⚠"), message, detail, warning, label, input, status, buttons].forEach(function (n) { dialog.appendChild(n); });
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    parts = {dialog: dialog, title: title, message: message, detail: detail, warning: warning, label: label, status: status, input: input, go: go, cancel: cancel, close: close};

    parts.input.addEventListener("input", check);
    parts.input.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); if (!parts.go.disabled) confirmed(); } });
    parts.go.addEventListener("click", confirmed);
    parts.cancel.addEventListener("click", function () { closeDialog(); });
    parts.close.addEventListener("click", function () { closeDialog(); });
    overlay.addEventListener("mousedown", function (event) { if (event.target === overlay) closeDialog(); });
    document.addEventListener("keydown", function (event) {
      if (overlay.hidden) return;
      if (event.key === "Escape") { event.preventDefault(); closeDialog(); return; }
      if (event.key !== "Tab") return;                                      // keep the focus inside the popup
      var order = [parts.close, parts.input, parts.cancel, parts.go].filter(function (n) { return !n.disabled; });
      var at = order.indexOf(document.activeElement), next = event.shiftKey ? at - 1 : at + 1;
      if (at < 0 || next < 0 || next >= order.length) { event.preventDefault(); order[next < 0 || at < 0 ? order.length - 1 : 0].focus(); }
    });
  }

  // Tells the person where they stand without ever spelling the name out.
  function check() {
    var typed = parts.input.value, word = pending ? pending.word : "", ok = !!pending && typed === word, text, state;
    if (!typed) { text = "Type the name to enable \u201c" + parts.go.textContent + "\u201d."; state = "idle"; }
    else if (ok) { text = "Matches. Press \u201c" + parts.go.textContent + "\u201d to continue."; state = "ok"; }
    else if (word.indexOf(typed) === 0) { text = "Keep typing\u2026 (" + typed.length + " of " + word.length + " characters)"; state = "partial"; }
    else if (typed.indexOf(word) === 0) { text = "Remove the extra characters after the name."; state = "wrong"; }
    else if (typed.toLowerCase() === word.toLowerCase()) { text = "Capital letters matter: the name must match exactly."; state = "wrong"; }
    else { text = "That does not match. Check the spelling."; state = "wrong"; }
    parts.go.disabled = !ok;
    parts.input.classList.toggle("match", ok);
    parts.input.classList.toggle("wrong", state === "wrong");
    parts.status.textContent = text;
    parts.status.className = "confirm-status " + state;
  }

  function openDialog(form, trigger) {
    if (!overlay) build();
    var data = form.dataset;
    pending = {form: form, trigger: trigger, word: data.typedConfirm};
    parts.title.textContent = data.confirmTitle || "CONFIRM";
    parts.message.textContent = data.confirmMessage || "";
    parts.detail.textContent = data.confirmDetail || "";
    parts.detail.hidden = !data.confirmDetail;
    parts.warning.textContent = data.confirmWarning || "This cannot be undone.";
    parts.label.textContent = data.confirmPrompt || "Type the name to confirm";
    parts.go.textContent = data.confirmButton || "Delete";
    parts.input.value = "";
    check();
    overlay.hidden = false;
    if (window.pgdasSound) window.pgdasSound.play("warning");
    parts.input.focus();
  }

  function closeDialog() {
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    var back = pending && pending.trigger;
    pending = null;
    if (back && back.focus) back.focus();
  }

  function confirmed() {
    if (!pending || parts.input.value !== pending.word) return;
    var form = pending.form, field = form.querySelector('input[name="confirm"]');
    if (!field) { field = document.createElement("input"); field.type = "hidden"; field.name = "confirm"; form.appendChild(field); }
    field.value = parts.input.value;
    pending = null;
    overlay.hidden = true;
    HTMLFormElement.prototype.submit.call(form);                            // submit() does not run the submit handler below again
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.dataset || form.dataset.typedConfirm === undefined) return;
    event.preventDefault();
    openDialog(form, event.submitter || document.activeElement);
  }, true);
})();
