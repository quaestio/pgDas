// Create database, Create role and Rename database as tabbed dialogs (General / Definition / ... / SQL), and Delete database.
// Every dialog builds its SQL on the SQL tab as you type; Save does not change anything yet: it opens the review popup
// (static/review.js) with exactly what would happen, and the change is made only after that is confirmed.
// All values are put into the page as text or as properly quoted SQL, never as HTML.
(function () {
  if (!window.pgdeckReview) return;
  var toastEl = document.getElementById("pgToast"), toastTimer = null;

  function toast(message, isError) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.className = "toast" + (isError ? " err" : "");
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, isError ? 9000 : 6000);
  }

  function ident(name) { return '"' + name.replace(/"/g, '""') + '"'; }
  function literal(text) { return "'" + text.replace(/'/g, "''") + "'"; }

  function option(select, value, label) {
    var o = document.createElement("option");
    o.value = value; o.textContent = label;
    select.appendChild(o);
  }

  // ---- what each dialog asks for, what it checks and the SQL it makes ------------------------------------------
  var KINDS = {
    "create-db": {
      id: "dlgCreateDb", what: "databases", done: "Database created.",
      fill: function (el, ctx) {
        var owner = el.querySelector('[data-field="owner"]'), template = el.querySelector('[data-field="template"]');
        owner.innerHTML = ""; template.innerHTML = "";
        option(owner, "", "the login PGDAS uses");
        ctx.roles.forEach(function (r) { option(owner, r, r); });
        option(template, "", "template1 (default)");
        option(template, "template0", "template0");
        ctx.databases.forEach(function (d) { option(template, d, d); });
      },
      problem: function (v) {
        if (!v.name.trim()) return "'Database' cannot be empty.";
        if (v.limit !== "" && !/^-?\d+$/.test(v.limit)) return "'Connection limit' must be a whole number.";
        return "";
      },
      sql: function (v) {
        var opts = [];
        if (v.owner) opts.push("OWNER = " + ident(v.owner));
        var own = v.encoding || v.collate.trim() || v.ctype.trim();        // another encoding or locale needs template0
        var template = v.template || (own ? "template0" : "");
        if (template) opts.push("TEMPLATE = " + ident(template));
        if (v.encoding) opts.push("ENCODING = " + literal(v.encoding));
        if (v.collate.trim()) opts.push("LC_COLLATE = " + literal(v.collate.trim()));
        if (v.ctype.trim()) opts.push("LC_CTYPE = " + literal(v.ctype.trim()));
        if (v.limit !== "") opts.push("CONNECTION LIMIT = " + parseInt(v.limit, 10));
        return "CREATE DATABASE " + ident(v.name.trim()) + (opts.length ? "\n    WITH " + opts.join("\n         ") : "");
      },
    },
    "create-role": {
      id: "dlgCreateRole", what: "roles", done: "Role created.",
      fill: function (el, ctx) {
        var box = el.querySelector("[data-members]");
        box.innerHTML = "";
        ctx.roles.forEach(function (r) {
          var label = document.createElement("label"), input = document.createElement("input");
          label.className = "dlg-check";
          input.type = "checkbox"; input.setAttribute("data-member", r);
          label.appendChild(input);
          label.appendChild(document.createTextNode(" " + r));
          box.appendChild(label);
        });
      },
      problem: function (v) {
        if (!v.name.trim()) return "'Name' cannot be empty.";
        if (v.limit !== "" && !/^-?\d+$/.test(v.limit)) return "'Connection limit' must be a whole number.";
        return "";
      },
      sql: function (v, masked) {
        var flags = [v.login ? "LOGIN" : "NOLOGIN", v.superuser ? "SUPERUSER" : "NOSUPERUSER", v.createdb ? "CREATEDB" : "NOCREATEDB",
                     v.createrole ? "CREATEROLE" : "NOCREATEROLE", v.inherit ? "INHERIT" : "NOINHERIT",
                     v.replication ? "REPLICATION" : "NOREPLICATION", v.bypassrls ? "BYPASSRLS" : "NOBYPASSRLS"];
        if (v.limit !== "") flags.push("CONNECTION LIMIT " + parseInt(v.limit, 10));
        if (v.password) flags.push("PASSWORD " + (masked ? "'********'" : literal(v.password)));
        if (v.expires) flags.push("VALID UNTIL " + literal(v.expires.replace("T", " ")));
        if (v.members.length) flags.push("IN ROLE " + v.members.map(ident).join(", "));
        return "CREATE ROLE " + ident(v.name.trim()) + " WITH\n    " + flags.join("\n    ");
      },
    },
    "rename-db": {
      id: "dlgRenameDb", what: "databases", done: "Database renamed.",
      fill: function (el, ctx) { el.querySelector('[data-field="old"]').value = ctx.db; },
      problem: function (v) {
        if (!v.name.trim()) return "'New name' cannot be empty.";
        if (v.name.trim() === v.old) return "That is the current name.";
        return "";
      },
      sql: function (v) { return "ALTER DATABASE " + ident(v.old) + " RENAME TO " + ident(v.name.trim()); },
    },
  };

  var open = null;       // {kind, el, ctx, fields, defaults}

  function values(el) {
    var v = {members: []};
    el.querySelectorAll("[data-field]").forEach(function (f) {
      v[f.getAttribute("data-field")] = f.type === "checkbox" ? f.checked : f.value;
    });
    el.querySelectorAll("[data-member]").forEach(function (m) { if (m.checked) v.members.push(m.getAttribute("data-member")); });
    return v;
  }

  function showTab(el, name) {
    el.querySelectorAll(".dlg-tab").forEach(function (t) { t.classList.toggle("active", t.getAttribute("data-tab") === name); });
    el.querySelectorAll(".dlg-panel").forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
  }

  function refresh() {
    if (!open) return;
    var v = values(open.el), problem = open.kind.problem(v);
    var line = open.el.querySelector("[data-problem]");
    line.textContent = problem;
    line.hidden = !problem;
    line.classList.remove("ok");
    open.el.querySelector("[data-dlg-save]").disabled = !!problem;
    open.el.querySelector("[data-sql]").textContent = problem ? "-- " + problem : open.kind.sql(v, true) + ";";
    var firstField = open.el.querySelector('[data-panel="general"] [data-field]:not([readonly])');
    if (firstField) firstField.classList.toggle("bad", !!problem && !v.name.trim());
  }

  function close() {
    if (!open) return;
    open.el.hidden = true;
    open = null;
  }

  function reset() {
    if (!open) return;
    open.el.querySelectorAll("[data-field]").forEach(function (f) {
      var d = open.defaults[f.getAttribute("data-field")];
      if (f.type === "checkbox") f.checked = !!d; else f.value = d === undefined ? "" : d;
    });
    open.el.querySelectorAll("[data-member]").forEach(function (m) { m.checked = false; });
    showTab(open.el, "general");
    refresh();
  }

  function openDialog(kindName, ctx) {
    var kind = KINDS[kindName];
    var el = document.getElementById(kind.id);
    if (!el) return;
    kind.fill(el, ctx);
    open = {kind: kind, el: el, ctx: ctx, defaults: {}};
    el.querySelectorAll("[data-field]").forEach(function (f) {      // the starting values (whatever the markup shipped with)
      var field = f.getAttribute("data-field");
      open.defaults[field] = f.type === "checkbox" ? f.defaultChecked : (field === "old" ? ctx.db : "");
    });
    reset();
    el.hidden = false;
    var first = el.querySelector('[data-panel="general"] [data-field]:not([readonly])');
    if (first) first.focus();
  }

  function finished(ctx, what, message) {
    toast(message);
    document.dispatchEvent(new CustomEvent("pgdeck:changed", {detail: {server: ctx.server, what: what}}));
  }

  function save() {
    if (!open) return;
    var current = open, v = values(current.el);
    if (current.kind.problem(v)) return;
    var button = current.el.querySelector("[data-dlg-save]"), line = current.el.querySelector("[data-problem]");
    button.disabled = true;
    window.pgdeckReview.start({
      server: current.ctx.server, db: "", sql: current.kind.sql(v, false),
      onError: function (message) { line.textContent = message; line.hidden = false; },
      onSuccess: function (result) { close(); finished(current.ctx, current.kind.what, result.message || current.kind.done); },
    }).then(function () { if (open === current) button.disabled = !!current.kind.problem(values(current.el)); });
  }

  function dropDatabase(ctx) {
    window.pgdeckReview.start({
      server: ctx.server, db: "", sql: "DROP DATABASE " + ident(ctx.db),
      onError: function (message) { toast(message, true); },
      onSuccess: function (result) { finished(ctx, "databases", result.message || "Database dropped."); },
    });
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-admin-dialog]");
    if (trigger) {
      var holder = trigger.closest("[data-admin]");
      if (!holder) return;
      var ctx = JSON.parse(holder.getAttribute("data-admin"));
      ctx.db = trigger.getAttribute("data-db") || "";
      var name = trigger.getAttribute("data-admin-dialog");
      if (name === "drop-db") dropDatabase(ctx); else openDialog(name, ctx);
      return;
    }
    if (!open) return;
    var tab = event.target.closest(".dlg-tab");
    if (tab && open.el.contains(tab)) { showTab(open.el, tab.getAttribute("data-tab")); return; }
    if (event.target.closest("[data-dlg-close]") && open.el.contains(event.target)) { close(); return; }
    if (event.target.closest("[data-dlg-reset]") && open.el.contains(event.target)) { reset(); return; }
    if (event.target.closest("[data-dlg-save]") && open.el.contains(event.target)) { save(); return; }
    if (event.target === open.el) close();
  });
  document.addEventListener("input", function (event) { if (open && open.el.contains(event.target)) refresh(); });
  document.addEventListener("change", function (event) { if (open && open.el.contains(event.target)) refresh(); });
  document.addEventListener("keydown", function (event) {
    var review = document.getElementById("changeModal");
    if (event.key === "Escape" && open && (!review || review.hidden)) close();
  });
})();
