// Constraints: the Constraints tab of New table, and Add / Rename / Drop constraint on an existing table's Constraints tab.
// A constraint is a Primary key, a Foreign key, a Unique constraint or a Check. The editor builds the SQL as you work, and nothing
// is changed until the review popup (static/review.js) is confirmed. Values are shown as text, never as HTML.
(function () {
  if (!window.pgdasReview) return;
  var toastEl = document.getElementById("pgToast"), toastTimer = null;

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message; toastEl.className = "toast"; toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 6000);
  }

  function ident(name) { return '"' + name.replace(/"/g, '""') + '"'; }
  function qualified(ctx) { return ident(ctx.schema) + "." + ident(ctx.table); }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = attrs[key]; else if (key === "class") node.className = attrs[key]; else node.setAttribute(key, attrs[key]);
    });
    (children || []).forEach(function (child) { node.appendChild(child); });
    return node;
  }

  function select(options, attrs) {
    var s = el("select", attrs);
    options.forEach(function (o) { s.appendChild(el("option", {value: o[0], text: o[1]})); });
    return s;
  }

  var TYPES = [["PRIMARY KEY", "Primary key"], ["FOREIGN KEY", "Foreign key"], ["UNIQUE", "Unique"], ["CHECK", "Check"]];
  var ACTIONS = [["", "default (NO ACTION)"], ["NO ACTION", "NO ACTION"], ["RESTRICT", "RESTRICT"], ["CASCADE", "CASCADE (delete / update along)"],
                 ["SET NULL", "SET NULL"], ["SET DEFAULT", "SET DEFAULT"]];

  // The tables of the database (with their columns), for choosing what a foreign key points at.
  var tableCache = {};
  function loadTables(ctx) {
    if (!ctx.tables_url) return Promise.resolve([]);
    if (tableCache[ctx.tables_url]) return Promise.resolve(tableCache[ctx.tables_url]);
    return fetch(ctx.tables_url, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
      .then(function (r) { return r.ok ? r.json() : {tables: []}; })
      .then(function (data) { tableCache[ctx.tables_url] = data.tables || []; return tableCache[ctx.tables_url]; })
      .catch(function () { return []; });
  }

  // ---- the editor: a list of constraints --------------------------------------------------------------------------
  // options: {ctx, single (one constraint, no remove button), notValid (offer NOT VALID for foreign keys and checks)}
  function editor(options) {
    var root = el("div", {"class": "cons"}), list = el("div", {"data-cons-list": ""});
    var add = el("button", {type: "button", "class": "btn", text: "＋ Add constraint"});
    var names = [], tables = [];
    if (!options.single) root.appendChild(el("p", {"class": "dlg-grants-head"}, [el("strong", {text: "Constraints"}), add]));
    root.appendChild(list);
    if (!options.single) root.appendChild(el("p", {"class": "dlg-hint", text: "A primary key makes the column(s) unique and not null. A foreign key keeps the values pointing at rows that exist in another table. A check refuses values that fail a condition. A unique constraint refuses duplicates."}));

    function columnSelect(chosen) {
      var s = select([["", "choose a column…"]].concat(names.map(function (n) { return [n, n]; })), {"data-cons-col": ""});
      s.value = names.indexOf(chosen) >= 0 ? chosen : "";
      return s;
    }

    function refSelect(chosen) {
      var s = select([["", "choose a column…"]], {"data-cons-refcol": ""});
      var table = tables[parseInt(chosen.table, 10)];
      (table ? table.columns : []).forEach(function (c) { s.appendChild(el("option", {value: c.name, text: c.name + "  (" + c.type + ")"})); });
      s.value = chosen.column || "";
      return s;
    }

    function addPair(row, start) {
      var type = row.querySelector("[data-cons-type]").value, pair = el("div", {"class": "cons-pair", "data-cons-pair": ""});
      pair.appendChild(columnSelect(start && start.column));
      if (type === "FOREIGN KEY") {
        pair.appendChild(el("span", {"class": "muted", text: "→"}));
        pair.appendChild(refSelect({table: row.querySelector("[data-cons-ref]").value, column: start && start.ref}));
      }
      pair.appendChild(el("button", {type: "button", "class": "btn", "data-cons-pair-remove": "", title: "Remove this column", text: "✕"}));
      row.querySelector("[data-cons-pairs]").appendChild(pair);
    }

    function show(row) {
      var type = row.querySelector("[data-cons-type]").value;
      row.querySelector("[data-cons-fk]").hidden = type !== "FOREIGN KEY";
      row.querySelector("[data-cons-columns]").hidden = type === "CHECK";
      row.querySelector("[data-cons-check]").hidden = type !== "CHECK";
      var nv = row.querySelector("[data-cons-notvalid]");
      if (nv) nv.parentNode.hidden = !(type === "FOREIGN KEY" || type === "CHECK");
    }

    function rebuildPairs(row, keep) {
      var old = keep ? readPairs(row) : [];
      row.querySelector("[data-cons-pairs]").textContent = "";
      if (!old.length) old = [{}];
      old.forEach(function (p) { addPair(row, p); });
    }

    function readPairs(row) {
      return Array.prototype.map.call(row.querySelectorAll("[data-cons-pair]"), function (pair) {
        var ref = pair.querySelector("[data-cons-refcol]");
        return {column: pair.querySelector("[data-cons-col]").value, ref: ref ? ref.value : ""};
      });
    }

    function addRow(start) {
      start = start || {};
      var row = el("div", {"class": "cons-row", "data-cons": ""});
      var type = select(TYPES, {"data-cons-type": ""}), name = el("input", {type: "text", "data-cons-name": "", autocomplete: "off", spellcheck: "false", placeholder: "name (optional)", maxlength: "63"});
      type.value = start.type || "PRIMARY KEY";
      var head = el("div", {"class": "cons-head"}, [type, name]);
      if (!options.single) head.appendChild(el("button", {type: "button", "class": "btn", "data-cons-remove": "", title: "Remove this constraint", text: "✕"}));
      row.appendChild(head);
      row.appendChild(el("div", {"data-cons-columns": ""}, [el("div", {"data-cons-pairs": ""}),
        el("button", {type: "button", "class": "btn", "data-cons-pair-add": "", text: "＋ column"})]));
      var ref = el("select", {"data-cons-ref": ""});
      var fk = el("div", {"data-cons-fk": "", hidden: "hidden"}, [
        el("label", {}, [document.createTextNode("References "), ref]),
        el("label", {}, [document.createTextNode("ON DELETE "), select(ACTIONS, {"data-cons-ondelete": ""})]),
        el("label", {}, [document.createTextNode("ON UPDATE "), select(ACTIONS, {"data-cons-onupdate": ""})])]);
      row.appendChild(fk);
      row.appendChild(el("div", {"data-cons-check": "", hidden: "hidden"}, [
        el("input", {type: "text", "data-cons-expr": "", autocomplete: "off", spellcheck: "false", placeholder: "condition, e.g. age >= 0"})]));
      if (options.notValid) row.appendChild(el("label", {"class": "dlg-check", hidden: "hidden"}, [
        el("input", {type: "checkbox", "data-cons-notvalid": ""}), document.createTextNode(" NOT VALID: do not check the rows already in the table (faster; new rows are still checked)")]));
      list.appendChild(row);
      fillReferences(row);
      rebuildPairs(row, false);
      show(row);
      return row;
    }

    function fillReferences(row) {
      var ref = row.querySelector("[data-cons-ref]"), chosen = ref.value;
      ref.textContent = "";
      ref.appendChild(el("option", {value: "", text: tables.length ? "choose a table…" : "loading tables…"}));
      tables.forEach(function (t, index) { ref.appendChild(el("option", {value: String(index), text: t.schema + "." + t.name})); });
      ref.value = chosen;
    }

    function read() {
      return Array.prototype.map.call(list.querySelectorAll("[data-cons]"), function (row) {
        var table = tables[parseInt(row.querySelector("[data-cons-ref]").value, 10)];
        var nv = row.querySelector("[data-cons-notvalid]");
        return {type: row.querySelector("[data-cons-type]").value, name: row.querySelector("[data-cons-name]").value, pairs: readPairs(row),
                refSchema: table ? table.schema : "", refTable: table ? table.name : "",
                onDelete: row.querySelector("[data-cons-ondelete]").value, onUpdate: row.querySelector("[data-cons-onupdate]").value,
                expr: row.querySelector("[data-cons-expr]").value, notValid: !!(nv && nv.checked)};
      });
    }

    root.addEventListener("click", function (event) {
      var row = event.target.closest("[data-cons]");
      if (event.target === add) { addRow(); root.dispatchEvent(new Event("input", {bubbles: true})); return; }
      if (!row) return;
      if (event.target.closest("[data-cons-remove]")) { row.remove(); root.dispatchEvent(new Event("input", {bubbles: true})); }
      else if (event.target.closest("[data-cons-pair-add]")) { addPair(row); root.dispatchEvent(new Event("input", {bubbles: true})); }
      else if (event.target.closest("[data-cons-pair-remove]")) {
        var pair = event.target.closest("[data-cons-pair]");
        if (row.querySelectorAll("[data-cons-pair]").length > 1) pair.remove(); else pair.querySelectorAll("select").forEach(function (s) { s.value = ""; });
        root.dispatchEvent(new Event("input", {bubbles: true}));
      }
    });
    root.addEventListener("change", function (event) {
      var row = event.target.closest("[data-cons]");
      if (!row) return;
      if (event.target.hasAttribute("data-cons-type")) { rebuildPairs(row, false); show(row); }
      else if (event.target.hasAttribute("data-cons-ref")) rebuildPairs(row, true);
    });

    loadTables(options.ctx).then(function (found) {
      tables = found;
      list.querySelectorAll("[data-cons]").forEach(function (row) { fillReferences(row); });
    });

    return {
      el: root,
      add: addRow,
      read: read,
      // The columns the constraint may use (the table's, or the ones typed on New table's Columns tab).
      setColumns: function (list2) {
        var changed = list2.join("\u0000") !== names.join("\u0000");
        names = list2;
        if (!changed) return;
        list.querySelectorAll("[data-cons-col]").forEach(function (s) {
          var chosen = s.value;
          s.textContent = "";
          s.appendChild(el("option", {value: "", text: "choose a column…"}));
          names.forEach(function (n) { s.appendChild(el("option", {value: n, text: n})); });
          s.value = names.indexOf(chosen) >= 0 ? chosen : "";
        });
      },
    };
  }

  // ---- checks and SQL -----------------------------------------------------------------------------------------------
  function problem(list) {
    var primaries = 0;
    for (var i = 0; i < list.length; i++) {
      var c = list[i], label = "Constraint " + (i + 1) + ": ";
      if (c.type === "CHECK") { if (!c.expr.trim()) return label + "enter the condition the check must satisfy."; continue; }
      if (c.type === "PRIMARY KEY") primaries++;
      var seen = {};
      for (var j = 0; j < c.pairs.length; j++) {
        if (!c.pairs[j].column) return label + "choose a column in every row.";
        if (seen[c.pairs[j].column]) return label + "a column is chosen twice.";
        seen[c.pairs[j].column] = true;
        if (c.type === "FOREIGN KEY" && !c.pairs[j].ref) return label + "choose the column each one points at.";
      }
      if (c.type === "FOREIGN KEY" && !c.refTable) return label + "choose the table it points at.";
    }
    return primaries > 1 ? "A table has only one primary key." : "";
  }

  function sql(c) {
    var name = c.name.trim() ? "CONSTRAINT " + ident(c.name.trim()) + " " : "";
    var cols = c.pairs.map(function (p) { return ident(p.column); }).join(", ");
    if (c.type === "CHECK") return name + "CHECK (" + c.expr.trim() + ")";
    if (c.type === "FOREIGN KEY") {
      return name + "FOREIGN KEY (" + cols + ") REFERENCES " + ident(c.refSchema) + "." + ident(c.refTable) + " (" +
        c.pairs.map(function (p) { return ident(p.ref); }).join(", ") + ")" + (c.onDelete ? " ON DELETE " + c.onDelete : "") + (c.onUpdate ? " ON UPDATE " + c.onUpdate : "");
    }
    return name + c.type + " (" + cols + ")";
  }

  window.pgdasConstraints = {editor: editor, problem: problem, sql: sql, ident: ident};

  // ---- the dialogs on an existing table ---------------------------------------------------------------------------
  var open = null;

  function finished(ctx, message) {
    toast(message);
    document.dispatchEvent(new CustomEvent("pgdas:changed", {detail: {server: ctx.server, redraw: true}}));
  }

  function close() { if (open) { open.root.remove(); open = null; } }

  function dialog(title, body, saveText) {
    var save = el("button", {type: "button", "class": "btn primary", "data-cdlg-save": "", disabled: "disabled", text: saveText || "Save"});
    var line = el("p", {"class": "dlg-problem", "data-cdlg-problem": "", hidden: "hidden"});
    var preview = el("pre", {"class": "sql dlg-sql", "data-cdlg-sql": ""});
    var root = el("div", {"class": "cyber-modal", "data-cdlg": ""}, [el("div", {"class": "cyber-dialog dlg tdlg", role: "dialog", "aria-modal": "true"}, [
      el("div", {"class": "modal-top"}, [el("span", {text: title}), el("button", {type: "button", "data-cdlg-close": "", "aria-label": "Close", text: "✕"})]),
      body, el("p", {"class": "dlg-hint", text: "The SQL that will be run:"}), preview, line,
      el("div", {"class": "dlg-foot"}, [el("button", {type: "button", "class": "btn", "data-cdlg-close": "", text: "Close"}), save])])]);
    document.body.appendChild(root);
    return {root: root, save: save, line: line, preview: preview};
  }

  function openAdd(ctx) {
    close();
    var box = editor({ctx: ctx, single: true, notValid: true});
    var parts = el("div", {"class": "dlg-panel"}, [el("div", {"class": "dlg-row"}, [el("label", {text: "Table"}),
      el("input", {type: "text", value: ctx.schema + "." + ctx.table, readonly: "readonly"})]), box.el]);
    var d = dialog("ADD — CONSTRAINT", parts, "Review");
    box.setColumns(ctx.columns || []);
    box.add();
    function statement() {
      var list = box.read(), c = list[0], text = problem(list);
      return {text: text, sql: text ? "" : "ALTER TABLE " + qualified(ctx) + " ADD " + sql(c) + (c.notValid && (c.type === "FOREIGN KEY" || c.type === "CHECK") ? " NOT VALID" : "")};
    }
    function refresh() {
      var s = statement();
      d.line.textContent = s.text; d.line.hidden = !s.text; d.save.disabled = !!s.text;
      d.preview.textContent = s.text ? "-- " + s.text : s.sql + ";";
    }
    open = {root: d.root, refresh: refresh, save: function () {
      var s = statement();
      if (s.text) return;
      d.save.disabled = true;
      window.pgdasReview.start({
        server: ctx.server, db: ctx.db, sql: s.sql,
        onError: function (message) { d.line.textContent = message; d.line.hidden = false; },
        onSuccess: function (result) { close(); finished(ctx, result.message || "Constraint added."); },
      }).then(function () { if (open) refresh(); });
    }};
    refresh();
  }

  function openRename(ctx) {
    close();
    var input = el("input", {type: "text", maxlength: "63", autocomplete: "off", spellcheck: "false"});
    var parts = el("div", {"class": "dlg-panel"}, [
      el("div", {"class": "dlg-row"}, [el("label", {text: "Constraint"}), el("input", {type: "text", value: ctx.constraint, readonly: "readonly"})]),
      el("div", {"class": "dlg-row"}, [el("label", {text: "New name"}), input])]);
    var d = dialog("RENAME — CONSTRAINT", parts, "Review");
    function statement() {
      var name = input.value.trim();
      var text = !name ? "'New name' cannot be empty." : name === ctx.constraint ? "That is the current name." : "";
      return {text: text, sql: text ? "" : "ALTER TABLE " + qualified(ctx) + " RENAME CONSTRAINT " + ident(ctx.constraint) + " TO " + ident(name)};
    }
    function refresh() {
      var s = statement();
      d.line.textContent = s.text; d.line.hidden = !s.text || !input.value; d.save.disabled = !!s.text;
      d.preview.textContent = s.text ? "-- " + s.text : s.sql + ";";
    }
    open = {root: d.root, refresh: refresh, save: function () {
      var s = statement();
      if (s.text) return;
      d.save.disabled = true;
      window.pgdasReview.start({
        server: ctx.server, db: ctx.db, sql: s.sql,
        onError: function (message) { d.line.textContent = message; d.line.hidden = false; },
        onSuccess: function (result) { close(); finished(ctx, result.message || "Constraint renamed."); },
      }).then(function () { if (open) refresh(); });
    }};
    refresh();
    input.focus();
  }

  function structure(node) {
    var holder = node.closest("[data-structure]");
    return holder ? JSON.parse(holder.getAttribute("data-structure")) : null;
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-constraint-action]");
    if (trigger) {
      var ctx = structure(trigger), what = trigger.getAttribute("data-constraint-action");
      if (!ctx) return;
      var tab = trigger.closest("[data-cons-tab]"), line = trigger.closest("tr[data-constraint]");
      ctx.columns = tab ? JSON.parse(tab.getAttribute("data-cons-tab")) : [];
      ctx.constraint = line ? line.getAttribute("data-constraint") : "";
      if (what === "add") openAdd(ctx);
      else if (what === "rename") openRename(ctx);
      else if (what === "drop") {
        window.pgdasReview.start({
          server: ctx.server, db: ctx.db, sql: "ALTER TABLE " + qualified(ctx) + " DROP CONSTRAINT " + ident(ctx.constraint),
          onError: function (text) { toast(text); },
          onSuccess: function (result) { finished(ctx, result.message || "Constraint dropped."); },
        });
      }
      return;
    }
    if (!open) return;
    if (event.target.closest("[data-cdlg-close]") || event.target === open.root) { close(); return; }
    if (event.target.closest("[data-cdlg-save]")) open.save();
  });
  document.addEventListener("input", function (event) { if (open && open.root.contains(event.target)) open.refresh(); });
  document.addEventListener("change", function (event) { if (open && open.root.contains(event.target)) setTimeout(function () { if (open) open.refresh(); }, 0); });
  document.addEventListener("keydown", function (event) {
    var reviewBox = document.getElementById("changeModal");
    if (event.key === "Escape" && open && (!reviewBox || reviewBox.hidden)) close();
  });
})();
