// Interface sounds, ported from the AdminWeb template: short tones made in the browser (Web Audio), no sound files.
// A click gives a rising blip, success a higher one, an error a low falling one. The speaker button in the header
// mutes them; the choice is remembered. Browsers only allow audio after the visitor has clicked something on the page.
(function () {
  var KEY = "pgdeck.sound";
  // [start Hz, end Hz] of each tone, as in the template.
  var TONES = {click: [420, 580], success: [620, 960], error: [180, 120]};
  var context = null;
  var on = true;
  try { on = localStorage.getItem(KEY) !== "0"; } catch (e) { /* not remembered */ }

  function play(type) {
    if (!on) return;
    try {
      var Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) return;
      context = context || new Audio();
      if (context.state === "suspended") context.resume();
      var tone = TONES[type] || TONES.click, now = context.currentTime;
      var oscillator = context.createOscillator(), gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(tone[0], now);
      oscillator.frequency.exponentialRampToValueAtTime(tone[1], now + 0.075);
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.055, now + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.11);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(now);
      oscillator.stop(now + 0.12);
    } catch (e) { /* sound is a nicety: never break the page */ }
  }
  window.pgdeckSound = {play: play, isOn: function () { return on; }};

  // A click on anything clickable gives the click tone (capture phase, so it also covers handlers that stop propagation).
  document.addEventListener("click", function (event) {
    if (event.target.closest("button, a, .metric, .row, input[type=checkbox], select")) play("click");
  }, true);

  var button = document.getElementById("soundToggle");
  function paint() {
    if (!button) return;
    var label = on ? "Mute sounds" : "Turn sounds on";
    button.classList.toggle("muted", !on);
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", String(!on));
  }
  paint();
  if (button) button.addEventListener("click", function () {
    on = !on;
    try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) { /* ignore */ }
    paint();
    if (on) play("success");
  });

  // Feedback for what a freshly loaded page reports (a saved server, a failed login, a rejected SQL statement).
  // The browser may still be blocking audio right after a page load; then this is simply silent.
  document.addEventListener("DOMContentLoaded", function () {
    if (document.querySelector(".messages li.error, .errorlist")) play("error");
    else if (document.querySelector(".messages li.success")) play("success");
  });
})();
