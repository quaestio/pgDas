// The person menu in the header: click the avatar and name to open it; Escape, a click elsewhere, or choosing an item closes it.
// Keyboard: Enter or Space opens it, the arrow keys move between the items, Escape closes it and returns to the button.
(function () {
  var wrap = document.getElementById("userMenu");
  if (!wrap) return;
  var button = document.getElementById("userMenuButton"), panel = document.getElementById("userMenuPanel");

  function items() { return Array.prototype.slice.call(panel.querySelectorAll('[role="menuitem"]')); }
  function setOpen(open) {
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    wrap.classList.toggle("open", open);
  }

  button.addEventListener("click", function () { setOpen(panel.hidden); });
  document.addEventListener("click", function (event) { if (!wrap.contains(event.target)) setOpen(false); });
  document.addEventListener("keydown", function (event) {
    if (panel.hidden) return;
    if (event.key === "Escape") { setOpen(false); button.focus(); return; }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    var list = items(), at = list.indexOf(document.activeElement);
    event.preventDefault();
    if (event.key === "ArrowDown") list[at < 0 ? 0 : (at + 1) % list.length].focus();
    else list[at <= 0 ? list.length - 1 : at - 1].focus();
  });
  button.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowDown") return;
    event.preventDefault();
    event.stopPropagation();                      // the document handler below must not also move on to the second item
    setOpen(true);
    items()[0].focus();
  });
})();
