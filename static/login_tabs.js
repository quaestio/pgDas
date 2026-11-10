// The sign-in page with a second tab, "Register your organization" (only when registering is switched on). Without JavaScript both
// parts are simply shown one under the other. The tab can also be opened with the address #register.
(function () {
  var card = document.getElementById("loginCard");
  if (!card) return;
  var tabs = card.querySelectorAll('[role="tab"]');
  if (!tabs.length) return;
  card.classList.add("js");
  function show(name) {
    tabs.forEach(function (tab) {
      var on = tab.getAttribute("data-tab") === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
    });
    card.querySelectorAll('[role="tabpanel"]').forEach(function (panel) { panel.hidden = panel.getAttribute("data-panel") !== name; });
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () { show(tab.getAttribute("data-tab")); });
    tab.addEventListener("keydown", function (event) {                      // ← → move between the tabs
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      var next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus(); show(next.getAttribute("data-tab"));
    });
  });
  show(window.location.hash === "#register" || card.getAttribute("data-active") === "register" ? "register" : "signin");
})();