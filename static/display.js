// Browser-local appearance preferences and native fullscreen controls.
(function () {
  var root = document.documentElement;
  var dialog = document.getElementById('appearanceSettings');
  var open = document.getElementById('appearanceOpen');
  var accent = document.getElementById('appearanceAccent');
  var effects = document.getElementById('appearanceEffects');
  function read(key) { try { return localStorage.getItem('pgdas.' + key); } catch (e) { return null; } }
  function save(key, value) { try { localStorage.setItem('pgdas.' + key, value); } catch (e) { /* Session only. */ } }
  function apply(color, enabled) {
    root.style.setProperty('--accent', color);
    document.body.classList.toggle('no-motion', !enabled);
    if (accent) accent.value = color;
    if (effects) effects.checked = enabled;
  }
  var color = read('accent');
  apply(/^#[0-9a-f]{6}$/i.test(color || '') ? color : '#00bfff', read('effects') !== 'off');
  if (dialog && open) {
    open.addEventListener('click', function () { dialog.showModal(); });
    dialog.addEventListener('close', function () { open.focus(); });
    accent.addEventListener('input', function () { apply(accent.value, effects.checked); save('accent', accent.value); });
    effects.addEventListener('change', function () { apply(accent.value, effects.checked); save('effects', effects.checked ? 'on' : 'off'); });
    document.getElementById('appearanceReset').addEventListener('click', function () {
      apply('#00bfff', true); save('accent', '#00bfff'); save('effects', 'on');
    });
  }
  var fullscreen = document.getElementById('fullscreen');
  var notice = document.getElementById('displayNotice');
  var noticeTimer;
  function notify(message) {
    if (!notice) return;
    notice.textContent = message; notice.hidden = false;
    clearTimeout(noticeTimer); noticeTimer = setTimeout(function () { notice.hidden = true; }, 6000);
  }
  function sync() {
    if (!fullscreen) return;
    var active = !!document.fullscreenElement;
    fullscreen.setAttribute('aria-pressed', String(active));
    fullscreen.title = active ? 'Exit Full Screen Mode' : 'Full Screen Mode';
    fullscreen.setAttribute('aria-label', active ? 'Exit Full Screen Mode' : 'Enter Full Screen Mode');
  }
  if (fullscreen) fullscreen.addEventListener('click', function () {
    if (!document.fullscreenEnabled || !root.requestFullscreen) {
      notify('Full Screen Mode is unavailable in this browser. Open PGDAS in a browser that supports fullscreen.'); return;
    }
    try {
      var result = document.fullscreenElement ? document.exitFullscreen() : root.requestFullscreen();
      Promise.resolve(result).catch(function () { notify('The browser could not enter or exit Full Screen Mode.'); });
    } catch (e) { notify('Full Screen Mode is unavailable in this browser.'); }
  });
  document.addEventListener('fullscreenchange', sync);
  sync();
})();
