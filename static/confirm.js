// A warning popup that makes you TYPE a name before something is deleted, in place of the browser's plain OK / Cancel box.
// A form opts in with data attributes:
//
//   <form method="post" action="..." data-typed-confirm="the name to type" data-confirm-title="DELETE BACKUP"
//         data-confirm-button="Delete backup" data-confirm-message="..." data-confirm-detail="..." data-confirm-warning="...">
//     <input type="hidden" name="confirm" value=""> ... <button type="submit">Delete</button></form>
//
// Pressing the button opens the popup (with a warning sound; the speaker button in the header mutes it). The red button stays
// disabled until the typed text is exactly the name, case included; then the form is sent with the typed text as `confirm`, which the
// server checks again, so skipping this script deletes nothing. Everything is put into the page as text, never as HTML.
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
    var label = make("label"), word = make("strong");
    label.setAttribute("for", "confirmInput");
    var input = make("input", "confirm-name");
    input.type = "text"; input.id = "confirmInput"; input.autocomplete = "off"; input.spellcheck = false; input.setAttribute("autocapitalize", "off");
    var hint = make("p", "confirm-hint", "Case matters. Nothing is deleted until the name matches.");

    var buttons = make("div", "modal-buttons"), cancel = make("button", "btn", "Cancel"), go = make("button", "btn danger", "Delete");
    cancel.type = "button"; go.type = "button"; go.disabled = true;
    buttons.appendChild(cancel); buttons.appendChild(go);

    label.appendChild(document.createTextNode("Type ")); label.appendChild(word); label.appendChild(document.createTextNode(" to confirm"));
    [top, make("div", "modal-icon", "⚠"), message, detail, warning, label, input, hint, buttons].forEach(function (n) { dialog.appendChild(n); });
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    parts = {dialog: dialog, title: title, message: message, detail: detail, warning: warning, word: word, input: input, go: go, cancel: cancel, close: close};

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

  function check() {
    var ok = !!pending && parts.input.value === pending.word;
    parts.go.disabled = !ok;
    parts.input.classList.toggle("match", ok);
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
    parts.word.textContent = pending.word;
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
