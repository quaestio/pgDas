// Create database, Create role and Rename database as tabbed dialogs (General / Definition / ... / SQL), and Delete database.
// Every dialog builds its SQL on the SQL tab as you type; Save does not change anything yet: it opens the review popup
// (static/review.js) with exactly what would happen, and the change is made only after that is confirmed.
// All values are put into the page as text or as properly quoted SQL, never as HTML.
(function () {
  if (!window.pgdasReview) return;
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

  // ---- the Security tab of Create database: rows of grantee + privileges (+ WITH GRANT OPTION), one GRANT each -----------
  var PRIVILEGES = ["CREATE", "TEMPORARY", "CONNECT"];

  function addGrantRow(el, grantees) {
    var row = document.createElement("div"), select = document.createElement("select");
    row.className = "dlg-grant"; row.setAttribute("data-grant-row", "");
    select.setAttribute("data-grantee", "");
    option(select, "", "choose a role…");
    option(select, "PUBLIC", "PUBLIC (everyone)");
    grantees.forEach(function (r) { option(select, r, r); });
    row.appendChild(select);
    var boxes = document.createElement("div"); boxes.className = "dlg-grant-boxes";
    ["ALL"].concat(PRIVILEGES).forEach(function (priv) {
      var line = document.createElement("div"), label = document.createElement("label"), box = document.createElement("input");
      var optLabel = document.createElement("label"), opt = document.createElement("input");
      line.className = "dlg-grant-line";
      box.type = "checkbox"; box.setAttribute("data-priv", priv);
      label.appendChild(box); label.appendChild(document.createTextNode(" " + priv));
      opt.type = "checkbox"; opt.setAttribute("data-opt", priv); opt.disabled = true;
      optLabel.className = "muted"; optLabel.appendChild(opt); optLabel.appendChild(document.createTextNode(" WITH GRANT OPTION"));
      line.appendChild(label); line.appendChild(optLabel);
      boxes.appendChild(line);
    });
    row.appendChild(boxes);
    var remove = document.createElement("button");
    remove.type = "button"; remove.className = "btn"; remove.setAttribute("data-grant-remove", ""); remove.title = "Remove this row"; remove.textContent = "✕";
    row.appendChild(remove);
    el.querySelector("[data-grants]").appendChild(row);
  }

  function syncGrantRow(row, changed) {          // ALL ticks or clears the others; an option box works only with its privilege
    var priv = function (name) { return row.querySelector('[data-priv="' + name + '"]'); };
    var opt = function (name) { return row.querySelector('[data-opt="' + name + '"]'); };
    if (changed && changed.getAttribute("data-priv") === "ALL") PRIVILEGES.forEach(function (n) { priv(n).checked = changed.checked; });
    else if (changed && changed.getAttribute("data-opt") === "ALL") PRIVILEGES.forEach(function (n) { opt(n).checked = changed.checked; });
    else if (changed && changed.hasAttribute("data-priv")) priv("ALL").checked = PRIVILEGES.every(function (n) { return priv(n).checked; });
    ["ALL"].concat(PRIVILEGES).forEach(function (n) { opt(n).disabled = !priv(n).checked; if (!priv(n).checked) opt(n).checked = false; });
  }

  function grantRows(el) {
    return Array.prototype.map.call(el.querySelectorAll("[data-grant-row]"), function (row) {
      var on = function (attr, name) { return row.querySelector("[" + attr + '="' + name + '"]').checked; };
      return {to: row.querySelector("[data-grantee]").value, all: on("data-priv", "ALL"), allOption: on("data-opt", "ALL"),
              privs: PRIVILEGES.filter(function (n) { return on("data-priv", n); }),
              options: PRIVILEGES.filter(function (n) { return on("data-opt", n); })};
    });
  }

  function grantSql(db, g) {                     // plain privileges in one GRANT, the WITH GRANT OPTION ones in another
    var to = g.to === "PUBLIC" ? "PUBLIC" : ident(g.to), lines = [];
    if (g.all) {
      lines.push("GRANT ALL PRIVILEGES ON DATABASE " + ident(db) + " TO " + to + (g.allOption ? " WITH GRANT OPTION" : ""));
      return lines;
    }
    var plain = g.privs.filter(function (n) { return g.options.indexOf(n) < 0; });
    if (plain.length) lines.push("GRANT " + plain.join(", ") + " ON DATABASE " + ident(db) + " TO " + to);
    if (g.options.length) lines.push("GRANT " + g.options.join(", ") + " ON DATABASE " + ident(db) + " TO " + to + " WITH GRANT OPTION");
    return lines;
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
        el.querySelector("[data-grants]").innerHTML = "";
      },
      problem: function (v) {
        if (!v.name.trim()) return "'Database' cannot be empty.";
        if (v.limit !== "" && !/^-?\d+$/.test(v.limit)) return "'Connection limit' must be a whole number.";
        if (v.grants.some(function (g) { return !g.to && g.privs.length; })) return "Security: choose a role for every row that has privileges.";
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
        var statements = ["CREATE DATABASE " + ident(v.name.trim()) + (opts.length ? "\n    WITH " + opts.join("\n         ") : "")];
        v.grants.filter(function (g) { return g.to && g.privs.length; }).forEach(function (g) {
          statements = statements.concat(grantSql(v.name.trim(), g));
        });
        return statements.join(";\n");
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
    "drop-db": {
      id: "dlgDropDb", what: "databases", done: "Database dropped.",
      fill: function (el, ctx) {
        var n = ctx.sessions || 0;
        el.querySelector("[data-sessions-note]").textContent = n
          ? n + " other session" + (n === 1 ? " is" : "s are") + " connected to this database right now."
          : "No other sessions are connected right now.";
        el.querySelector("[data-field=force]").checked = false;
      },
      problem: function () { return ""; },
      sql: function (v) { return "DROP DATABASE " + ident(v.old) + (v.force ? " WITH (FORCE)" : ""); },
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
    var v = {members: [], grants: grantRows(el)};
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
    open.el.querySelectorAll("[data-grant-row]").forEach(function (r) { r.remove(); });
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
    document.dispatchEvent(new CustomEvent("pgdas:changed", {detail: {server: ctx.server, what: what}}));
  }

  function save() {
    if (!open) return;
    var current = open, v = values(current.el);
    if (current.kind.problem(v)) return;
    var button = current.el.querySelector("[data-dlg-save]"), line = current.el.querySelector("[data-problem]");
    button.disabled = true;
    window.pgdasReview.start({
      server: current.ctx.server, db: "", sql: current.kind.sql(v, false),
      onError: function (message) { line.textContent = message; line.hidden = false; },
      onSuccess: function (result) { close(); finished(current.ctx, current.kind.what, result.message || current.kind.done); },
    }).then(function () { if (open === current) button.disabled = !!current.kind.problem(values(current.el)); });
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-admin-dialog]");
    if (trigger) {
      var holder = trigger.closest("[data-admin]");
      if (!holder) return;
      var ctx = JSON.parse(holder.getAttribute("data-admin"));
      ctx.db = trigger.getAttribute("data-db") || "";
      ctx.sessions = parseInt(trigger.getAttribute("data-sessions") || "0", 10);
      openDialog(trigger.getAttribute("data-admin-dialog"), ctx);
      return;
    }
    if (!open) return;
    var tab = event.target.closest(".dlg-tab");
    if (tab && open.el.contains(tab)) { showTab(open.el, tab.getAttribute("data-tab")); return; }
    if (event.target.closest("[data-dlg-close]") && open.el.contains(event.target)) { close(); return; }
    if (event.target.closest("[data-dlg-reset]") && open.el.contains(event.target)) { reset(); return; }
    if (event.target.closest("[data-dlg-save]") && open.el.contains(event.target)) { save(); return; }
    if (event.target.closest("[data-grant-add]") && open.el.contains(event.target)) { addGrantRow(open.el, open.ctx.roles || []); refresh(); return; }
    var removeRow = event.target.closest("[data-grant-remove]");
    if (removeRow && open.el.contains(removeRow)) { removeRow.closest("[data-grant-row]").remove(); refresh(); return; }
    if (event.target === open.el) close();
  });
  document.addEventListener("input", function (event) { if (open && open.el.contains(event.target)) refresh(); });
  document.addEventListener("change", function (event) {
    if (!open || !open.el.contains(event.target)) return;
    var row = event.target.closest("[data-grant-row]");
    if (row) syncGrantRow(row, event.target);
    refresh();
  });
  document.addEventListener("keydown", function (event) {
    var review = document.getElementById("changeModal");
    if (event.key === "Escape" && open && (!review || review.hidden)) close();
  });
})();
