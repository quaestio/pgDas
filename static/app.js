// Layout helpers: mobile menu, collapsible sidebar and a draggable sidebar width. The width and the
// collapsed state are kept in localStorage; templates/base.html applies them before first paint.
(function () {
  var KEY_WIDTH = "pgdas.sidebarWidth", KEY_COLLAPSED = "pgdas.sidebarCollapsed";
  var MIN = 220, MAX = 640, DEFAULT = 330, STEP = 20;
  var root = document.documentElement;

  function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode: just don't remember */ } }
  function currentWidth() { return parseInt(getComputedStyle(root).getPropertyValue("--sidebar-w"), 10) || DEFAULT; }
  function setWidth(px, save) {
    px = Math.max(MIN, Math.min(MAX, Math.round(px)));
    root.style.setProperty("--sidebar-w", px + "px");
    if (save) store(KEY_WIDTH, px);
    return px;
  }

  var toggle = document.getElementById("toggle");
  var mobile = window.matchMedia("(max-width: 800px)");
  function syncToggle() {
    if (toggle) toggle.setAttribute("aria-expanded", String(mobile.matches ?
      document.body.classList.contains("menu-open") : !root.classList.contains("sidebar-collapsed")));
  }
  function closeMenu() {
    document.body.classList.remove("menu-open");
    syncToggle();
    if (toggle) toggle.focus();
  }
  var close = document.getElementById("sidebarClose");
  if (close) close.addEventListener("click", closeMenu);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && mobile.matches && document.body.classList.contains("menu-open")) closeMenu();
  });
  mobile.addEventListener("change", function () {
    document.body.classList.remove("menu-open");
    syncToggle();
  });
  syncToggle();
  if (toggle) toggle.addEventListener("click", function () {
    if (window.matchMedia("(max-width: 800px)").matches) {
      document.body.classList.toggle("menu-open");          // phones: the sidebar slides over the page
      if (document.body.classList.contains("menu-open") && close) close.focus();
    } else {
      var collapsed = root.classList.toggle("sidebar-collapsed");
      store(KEY_COLLAPSED, collapsed ? "1" : "0");
    }
    syncToggle();
  });

  var aside = document.getElementById("sidebar");
  var resizer = document.getElementById("sidebarResizer");
  if (aside && resizer) {
    var dragging = false;
    resizer.addEventListener("pointerdown", function (event) {
      dragging = true;
      try { resizer.setPointerCapture(event.pointerId); } catch (e) { /* synthetic events */ }
      document.body.classList.add("resizing");
      event.preventDefault();
    });
    resizer.addEventListener("pointermove", function (event) {
      if (dragging) setWidth(event.clientX - aside.getBoundingClientRect().left, false);
    });
    function stop() {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("resizing");
      store(KEY_WIDTH, currentWidth());
    }
    resizer.addEventListener("pointerup", stop);
    resizer.addEventListener("pointercancel", stop);
    resizer.addEventListener("dblclick", function () { setWidth(DEFAULT, true); });   // double-click: back to the default
    resizer.addEventListener("keydown", function (event) {
      if (event.key === "ArrowLeft") { event.preventDefault(); setWidth(currentWidth() - STEP, true); }
      if (event.key === "ArrowRight") { event.preventDefault(); setWidth(currentWidth() + STEP, true); }
      if (event.key === "Home") { event.preventDefault(); setWidth(DEFAULT, true); }
    });
  }
})();
