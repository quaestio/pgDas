// "N sessions" on a row of Server > Databases opens this popup: the sessions connected to THAT database right now, each with
// Cancel query (stops the running query, keeps the connection) and End session (closes the connection), and End all sessions.
// It reads the same data as the Monitor page (/monitor/data/) and uses its stop address, so the same PostgreSQL rights apply and
// PostgreSQL's own refusal is shown when the login may not do it. Ending anything takes two clicks (the first arms the button for a few
// seconds), instead of the browser's plain OK box. Everything from the server is put into the page as text, never as HTML.
(function () {
  var overlay = document.getElementById("dlgDbSessions");
  if (!overlay) return;
  var dataUrl = overlay.dataset.dataUrl, stopUrl = overlay.dataset.stopUrl;
  var title = document.getElementById("dbsTitle"), summary = document.getElementById("dbsSummary"), rows = document.getElementById("dbsRows");
  var message = document.getElementById("dbsMessage"), endAll = overlay.querySelector("[data-dbs-end-all]");
  var csrf = ((document.querySelector("#changeModal input[name=csrfmiddlewaretoken]") || {}).value) || "";
  var server = null, db = null, timer = null, sessions = [], opener = null, armed = null, changed = false;

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function duration(seconds) {
    if (seconds == null) return "";
    if (seconds < 60) return seconds.toFixed(1) + " s";
    if (seconds < 3600) return Math.floor(seconds / 60) + " m " + Math.round(seconds % 60) + " s";
    return Math.floor(seconds / 3600) + " h " + Math.floor(seconds % 3600 / 60) + " m";
  }

  function say(text, bad) { message.textContent = text; message.hidden = !text; message.className = "dlg-problem" + (bad ? "" : " ok"); }

  // A button that needs a second click: the first click arms it for 4 seconds ("End session" becomes "Click again to end").
  function twoStep(button, armedText, action, warn) {
    var normal = button.textContent, timeout = null;
    function disarm() { clearTimeout(timeout); button.textContent = normal; button.classList.remove("armed"); if (armed === disarm) armed = null; }
    button.addEventListener("click", function () {
      if (button.classList.contains("armed")) { disarm(); action(); return; }
      if (armed) armed();
      armed = disarm; button.textContent = armedText; button.classList.add("armed");
      if (warn && window.pgdasSound) window.pgdasSound.play("warning");
      timeout = setTimeout(disarm, 4000);
    });
  }

  function stop(pid, how) {
    var body = new URLSearchParams({server: server, pid: pid, how: how});
    return fetch(stopUrl, {method: "POST", credentials: "same-origin", headers: {"X-CSRFToken": csrf}, body: body})
      .then(function (response) { return response.json(); });
  }

  function act(pid, how) {
    stop(pid, how).then(function (result) {
      say(result.ok ? result.message : (result.error || "PostgreSQL did not allow that."), !result.ok);
      if (result.ok) changed = true;
      load();
    }).catch(function () { say("Could not reach PGDAS. Nothing was changed.", true); });
  }

  function endEverySession() {
    var list = sessions.slice(), ended = 0;
    (function next() {
      if (!list.length) { say("Ended " + ended + " session" + (ended === 1 ? "" : "s") + ".", false); changed = changed || ended > 0; load(); return; }
      var session = list.shift();
      stop(session.pid, "end").then(function (result) {
        if (!result.ok) { say("Stopped after " + ended + ": " + (result.error || "PostgreSQL did not allow that."), true); changed = changed || ended > 0; load(); return; }
        ended += 1; next();
      }).catch(function () { say("Could not reach PGDAS. Nothing more was changed.", true); load(); });
    })();
  }

  function render() {
    rows.replaceChildren();
    summary.textContent = sessions.length
      ? sessions.length + " other session" + (sessions.length === 1 ? " is" : "s are") + " connected to this database right now. The list refreshes every few seconds."
      : "No other sessions are connected to this database right now. PGDAS’s own connection is not listed. Anything that connects appears here within a few seconds.";
    endAll.disabled = !sessions.length;
    if (!sessions.length) { var empty = rows.insertRow().insertCell(); empty.colSpan = 8; empty.className = "muted"; empty.textContent = "None."; return; }
    sessions.forEach(function (s) {
      var tr = rows.insertRow();
      tr.insertCell().textContent = s.pid; tr.insertCell().textContent = s.user || ""; tr.insertCell().textContent = s.application || "";
      tr.insertCell().textContent = s.client || "";
      var state = tr.insertCell(); state.textContent = s.state + (s.wait ? " (" + s.wait + ")" : "");
      if (s.state.indexOf("idle in transaction") === 0) state.className = "bad-text";
      tr.insertCell().textContent = s.state === "active" ? duration(s.running) : (s.in_transaction != null ? "txn " + duration(s.in_transaction) : "");
      var query = tr.insertCell(); query.className = "dbs-query"; query.textContent = (s.query || "").slice(0, 120); query.title = s.query || "";
      var actions = tr.insertCell(); actions.className = "dbs-actions";
      if (s.state === "active") {
        var cancel = make("button", "btn", "Cancel query"); cancel.type = "button";
        twoStep(cancel, "Click again to cancel", function () { act(s.pid, "cancel"); }, false);
        actions.appendChild(cancel);
      }
      var end = make("button", "btn danger", "End session"); end.type = "button";
      twoStep(end, "Click again to end", function () { act(s.pid, "end"); }, true);
      actions.appendChild(end);
    });
  }

  function load() {
    if (overlay.hidden) return;
    fetch(dataUrl + "?server=" + encodeURIComponent(server), {credentials: "same-origin"})
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data.ok) { say(data.error || "Could not read the sessions.", true); return; }
        sessions = data.sessions.filter(function (s) { return s.database === db; });
        if (armed) return;                                    // do not redraw a button the person has just armed
        render();
      })
      .catch(function () { say("Could not reach PGDAS.", true); });
  }

  function open(trigger) {
    opener = trigger; server = trigger.dataset.server; db = trigger.dataset.dbSessions; changed = false; sessions = [];
    title.textContent = "SESSIONS — " + db;
    say("", false); rows.replaceChildren(); summary.textContent = "Loading…"; endAll.disabled = true;
    overlay.hidden = false;
    load();
    clearInterval(timer);
    timer = setInterval(function () { if (!document.hidden) load(); }, 5000);
    overlay.querySelector("[data-dbs-refresh]").focus();
  }

  function close() {
    if (overlay.hidden) return;
    overlay.hidden = true; clearInterval(timer); if (armed) armed();
    if (changed) document.dispatchEvent(new CustomEvent("pgdas:changed", {detail: {server: server, what: "databases"}}));   // refresh the counts in the list
    if (opener && opener.isConnected) opener.focus();
  }

  twoStep(endAll, "Click again to end ALL", endEverySession, true);
  overlay.querySelector("[data-dbs-refresh]").addEventListener("click", load);
  overlay.addEventListener("click", function (event) { if (event.target === overlay || event.target.closest("[data-dbs-close]")) close(); });
  document.addEventListener("keydown", function (event) {
    var review = document.getElementById("changeModal");
    if (event.key === "Escape" && !overlay.hidden && (!review || review.hidden)) close();
  });
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-db-sessions]");
    if (trigger) open(trigger);
  });
})();
