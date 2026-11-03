// Table structure: New table, Rename and Drop a table; Add, Edit, Rename and Drop a column; change the order of the columns.
// Every dialog builds its SQL on the SQL tab as you type. Save changes nothing yet: it opens the review popup (static/review.js)
// with exactly what would happen, and the change is made only after that is confirmed (a typed word for the risky ones).
// Changing the order rebuilds the table (explorer/table_rebuild.py), so it asks for the table name.
// Values are put into the page as text or as properly quoted SQL, never as HTML.
(function () {
  if (!window.pgdasReview) return;
  var toastEl = document.getElementById("pgToast"), toastTimer = null;
  // The built-in column types of PostgreSQL (Table 8.1 "Data Types" in https://www.postgresql.org/docs/current/datatype.html): [what to type, what it is].
  // Sizes and precisions can be added by hand, e.g. varchar(50), numeric(12,2), timestamp(3). Add [] for an array, e.g. text[].
  var TYPES = [
    ["bigint", "signed eight-byte integer (int8)"], ["bigserial", "autoincrementing eight-byte integer (serial8)"],
    ["bit(1)", "fixed-length bit string"], ["bit varying", "variable-length bit string (varbit)"], ["boolean", "true / false (bool)"],
    ["box", "rectangular box on a plane"], ["bytea", "binary data (\"byte array\")"], ["character(1)", "fixed-length character string (char)"],
    ["character varying(255)", "variable-length character string (varchar)"], ["cidr", "IPv4 or IPv6 network address"], ["circle", "circle on a plane"],
    ["date", "calendar date (year, month, day)"], ["double precision", "8-byte floating-point number (float8)"], ["inet", "IPv4 or IPv6 host address"],
    ["integer", "signed four-byte integer (int, int4)"], ["interval", "time span"], ["json", "textual JSON data"], ["jsonb", "binary JSON data, decomposed"],
    ["line", "infinite line on a plane"], ["lseg", "line segment on a plane"], ["macaddr", "MAC address"], ["macaddr8", "MAC address (EUI-64 format)"],
    ["money", "currency amount"], ["numeric(12,2)", "exact number of selectable precision (decimal)"], ["path", "geometric path on a plane"],
    ["pg_lsn", "PostgreSQL log sequence number"], ["pg_snapshot", "user-level transaction ID snapshot"], ["point", "geometric point on a plane"],
    ["polygon", "closed geometric path on a plane"], ["real", "4-byte floating-point number (float4)"], ["smallint", "signed two-byte integer (int2)"],
    ["smallserial", "autoincrementing two-byte integer (serial2)"], ["serial", "autoincrementing four-byte integer (serial4)"],
    ["text", "variable-length character string"], ["time", "time of day, no time zone"], ["time with time zone", "time of day with time zone (timetz)"],
    ["timestamp", "date and time, no time zone"], ["timestamp with time zone", "date and time with time zone (timestamptz)"],
    ["tsquery", "text search query"], ["tsvector", "text search document"], ["uuid", "universally unique identifier"], ["xml", "XML data"],
  ];

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

  function row(label, input) { return el("div", {"class": "dlg-row"}, [el("label", {text: label}), input]); }
  function textInput(field, placeholder) {
    return el("input", {type: "text", "data-field": field, autocomplete: "off", spellcheck: "false", maxlength: "200", placeholder: placeholder || ""});
  }
  function typeInput(field) {
    var input = textInput(field, "type, e.g. integer or varchar(50)");
    input.setAttribute("list", "tdlgTypes");
    return input;
  }
  function checkbox(field, label, checked) {
    var box = el("input", {type: "checkbox", "data-field": field});
    box.checked = !!checked;
    return el("label", {"class": "dlg-check"}, [box, document.createTextNode(" " + label)]);
  }

  function columnSql(c) {
    return ident(c.name.trim()) + " " + c.type.trim() + (c.notnull ? " NOT NULL" : "") + (c.default.trim() ? " DEFAULT " + c.default.trim() : "");
  }

  // ---- the dialogs: what each asks for, what it checks, the SQL it makes, and what the page does afterwards ---------
  var KINDS = {
    "new-table": {
      title: "CREATE — TABLE", done: "Table created.",
      build: function (form, ctx) {
        form.appendChild(row("Schema", el("input", {type: "text", value: ctx.schema, readonly: "readonly"})));
        form.appendChild(row("Table", textInput("name")));
        form.appendChild(el("p", {"class": "dlg-grants-head"}, [el("strong", {text: "Columns"}),
          el("button", {type: "button", "class": "btn", "data-col-add": "", text: "＋ Add column"})]));
        form.appendChild(el("div", {"data-cols": ""}));
        addColumn(form, {name: "id", type: "bigserial", pk: true});
      },
      collect: function (v, form) { v.cols = columns(form); return v; },
      problem: function (v) {
        if (!v.name.trim()) return "'Table' cannot be empty.";
        var seen = {};
        for (var i = 0; i < v.cols.length; i++) {
          var c = v.cols[i], n = c.name.trim();
          if (!n) return "Column " + (i + 1) + " has no name.";
          if (!c.type.trim()) return "Column " + n + " has no type.";
          if (seen[n]) return "Column " + n + " is listed twice.";
          seen[n] = true;
        }
        return v.cols.length ? "" : "Add at least one column.";
      },
      sql: function (v, ctx) {
        var lines = v.cols.map(function (c) { return "    " + columnSql(c); });
        var keys = v.cols.filter(function (c) { return c.pk; }).map(function (c) { return ident(c.name.trim()); });
        if (keys.length) lines.push("    PRIMARY KEY (" + keys.join(", ") + ")");
        return "CREATE TABLE " + ident(ctx.schema) + "." + ident(v.name.trim()) + " (\n" + lines.join(",\n") + "\n)";
      },
      after: function (v, ctx) {
        return {node: folder(ctx), open: {type: "table", server: ctx.server, db: ctx.db, schema: ctx.schema, table: v.name.trim()}};
      },
    },
    "rename-table": {
      title: "RENAME — TABLE", done: "Table renamed.",
      build: function (form, ctx) {
        form.appendChild(row("Table", el("input", {type: "text", value: ctx.table, readonly: "readonly"})));
        form.appendChild(row("New name", textInput("name")));
        form.appendChild(el("p", {"class": "dlg-hint", text: "Views, functions and applications that use the old name will stop working until they are changed."}));
      },
      problem: function (v, ctx) { return !v.name.trim() ? "'New name' cannot be empty." : v.name.trim() === ctx.table ? "That is the current name." : ""; },
      sql: function (v, ctx) { return "ALTER TABLE " + qualified(ctx) + " RENAME TO " + ident(v.name.trim()); },
      after: function (v, ctx) {
        return {node: folder(ctx), open: {type: "table", server: ctx.server, db: ctx.db, schema: ctx.schema, table: v.name.trim()}};
      },
    },
    "add-column": {
      title: "ADD — COLUMN", done: "Column added.",
      build: function (form, ctx) {
        form.appendChild(row("Table", el("input", {type: "text", value: ctx.schema + "." + ctx.table, readonly: "readonly"})));
        form.appendChild(row("Column", textInput("name")));
        form.appendChild(row("Type", typeInput("type")));
        form.appendChild(row("Default", textInput("default", "SQL expression, e.g. 0 or 'new' or now() (optional)")));
        form.appendChild(checkbox("notnull", "Not null (needs a default when the table already has rows)"));
      },
      problem: function (v) { return !v.name.trim() ? "'Column' cannot be empty." : !v.type.trim() ? "'Type' cannot be empty." : ""; },
      sql: function (v, ctx) { return "ALTER TABLE " + qualified(ctx) + " ADD COLUMN " + columnSql(v); },
      after: function () { return {redraw: true}; },
    },
    "rename-column": {
      title: "RENAME — COLUMN", done: "Column renamed.",
      build: function (form, ctx) {
        form.appendChild(row("Column", el("input", {type: "text", value: ctx.column.name, readonly: "readonly"})));
        form.appendChild(row("New name", textInput("name")));
      },
      problem: function (v, ctx) { return !v.name.trim() ? "'New name' cannot be empty." : v.name.trim() === ctx.column.name ? "That is the current name." : ""; },
      sql: function (v, ctx) { return "ALTER TABLE " + qualified(ctx) + " RENAME COLUMN " + ident(ctx.column.name) + " TO " + ident(v.name.trim()); },
      after: function () { return {redraw: true}; },
    },
    "edit-column": {
      title: "EDIT — COLUMN", done: "Column changed.",
      build: function (form, ctx) {
        var c = ctx.column, plain = !c.identity && !c.generated;
        form.appendChild(row("Column", el("input", {type: "text", value: c.name, readonly: "readonly"})));
        var type = typeInput("type"); type.value = c.type;
        form.appendChild(row("Type", type));
        form.appendChild(checkbox("using", "Convert the existing values to the new type (USING " + c.name + "::type)", true));
        var def = textInput("default", plain ? "SQL expression; empty = no default" : "");
        def.value = c.default; def.disabled = !plain;
        form.appendChild(row("Default", def));
        form.appendChild(checkbox("notnull", "Not null", !c.nullable));
        if (!plain) form.appendChild(el("p", {"class": "dlg-hint", text: "The default of an identity or generated column cannot be edited here."}));
        else form.appendChild(el("p", {"class": "dlg-hint", text: "Changing the type rewrites the table and locks it while it runs."}));
      },
      actions: function (v, ctx) {
        var c = ctx.column, out = [], col = ident(c.name), plain = !c.identity && !c.generated;
        if (v.type.trim() && v.type.trim() !== c.type) out.push("ALTER COLUMN " + col + " TYPE " + v.type.trim() + (v.using ? " USING " + col + "::" + v.type.trim() : ""));
        if (plain && v.default.trim() !== c.default) out.push("ALTER COLUMN " + col + (v.default.trim() ? " SET DEFAULT " + v.default.trim() : " DROP DEFAULT"));
        if (v.notnull !== !c.nullable) out.push("ALTER COLUMN " + col + (v.notnull ? " SET NOT NULL" : " DROP NOT NULL"));
        return out;
      },
      problem: function (v, ctx) { return this.actions(v, ctx).length ? "" : "Nothing has been changed."; },
      sql: function (v, ctx) { return "ALTER TABLE " + qualified(ctx) + "\n    " + this.actions(v, ctx).join(",\n    "); },
      after: function () { return {redraw: true}; },
    },
    "create-index": {
      title: "CREATE — INDEX", done: "Index created.",
      build: function (form, ctx) {
        form.appendChild(row("Table", el("input", {type: "text", value: ctx.schema + "." + ctx.table, readonly: "readonly"})));
        form.appendChild(row("Name", textInput("name", "optional: PostgreSQL picks one when empty")));
        var method = el("select", {"data-field": "method"});
        ["btree", "hash", "gist", "gin", "brin", "spgist"].forEach(function (m) { method.appendChild(el("option", {value: m, text: m})); });
        form.appendChild(row("Method", method));
        form.appendChild(checkbox("unique", "Unique (no two rows may have the same values)"));
        form.appendChild(el("p", {"class": "dlg-grants-head"}, [el("strong", {text: "Columns (in index order)"})]));
        (ctx.columns || []).forEach(function (name) {
          var box = el("input", {type: "checkbox", "data-idxcol": name}), desc = el("input", {type: "checkbox", "data-idxdesc": name});
          form.appendChild(el("div", {"class": "dlg-check"}, [el("label", {}, [box, document.createTextNode(" " + name)]),
            el("label", {"class": "muted", style: "margin-left:14px"}, [desc, document.createTextNode(" descending")])]));
        });
        form.appendChild(row("Expression", textInput("expr", "optional, e.g. lower(email)")));
        form.appendChild(row("Only rows where", textInput("where", "optional partial index, e.g. active")));
      },
      collect: function (v, form) {
        v.items = Array.prototype.filter.call(form.querySelectorAll("[data-idxcol]"), function (b) { return b.checked; }).map(function (b) {
          var name = b.getAttribute("data-idxcol"), desc = form.querySelector('[data-idxdesc="' + name.replace(/"/g, '\\"') + '"]');
          return ident(name) + (desc && desc.checked ? " DESC" : "");
        });
        if (v.expr.trim()) v.items.push("(" + v.expr.trim() + ")");
        return v;
      },
      problem: function (v) { return v.items.length ? "" : "Choose at least one column or enter an expression."; },
      sql: function (v, ctx) {
        return "CREATE " + (v.unique ? "UNIQUE " : "") + "INDEX " + (v.name.trim() ? ident(v.name.trim()) + " " : "") + "ON " + qualified(ctx) + " USING " + v.method +
          " (" + v.items.join(", ") + ")" + (v.where.trim() ? " WHERE " + v.where.trim() : "");
      },
      after: function () { return {redraw: true}; },
    },
    "rename-index": {
      title: "RENAME — INDEX", done: "Index renamed.",
      build: function (form, ctx) {
        form.appendChild(row("Index", el("input", {type: "text", value: ctx.index, readonly: "readonly"})));
        form.appendChild(row("New name", textInput("name")));
      },
      problem: function (v, ctx) { return !v.name.trim() ? "'New name' cannot be empty." : v.name.trim() === ctx.index ? "That is the current name." : ""; },
      sql: function (v, ctx) { return "ALTER INDEX " + ident(ctx.schema) + "." + ident(ctx.index) + " RENAME TO " + ident(v.name.trim()); },
      after: function () { return {redraw: true}; },
    },
  };

  function folder(ctx) { return {type: "tables", server: ctx.server, db: ctx.db, schema: ctx.schema}; }

  // ---- the column rows of New table ------------------------------------------------------------------------------
  function addColumn(form, start) {
    start = start || {};
    var line = el("div", {"class": "tdlg-col", "data-col": ""});
    var name = textInput("c-name", "name"), type = typeInput("c-type"), def = textInput("c-default", "default");
    name.value = start.name || ""; type.value = start.type || "";
    var notnull = el("input", {type: "checkbox", "data-field": "c-notnull"}), pk = el("input", {type: "checkbox", "data-field": "c-pk"});
    pk.checked = !!start.pk;
    var remove = el("button", {type: "button", "class": "btn", "data-col-remove": "", title: "Remove this column", text: "✕"});
    [name, type, def].forEach(function (input) { line.appendChild(input); });
    line.appendChild(el("label", {title: "Not null"}, [notnull, document.createTextNode(" not null")]));
    line.appendChild(el("label", {title: "Primary key"}, [pk, document.createTextNode(" key")]));
    line.appendChild(remove);
    form.querySelector("[data-cols]").appendChild(line);
  }

  function columns(form) {
    return Array.prototype.map.call(form.querySelectorAll("[data-col]"), function (line) {
      var get = function (field) { return line.querySelector('[data-field="' + field + '"]'); };
      return {name: get("c-name").value, type: get("c-type").value, default: get("c-default").value, notnull: get("c-notnull").checked, pk: get("c-pk").checked};
    });
  }

  // ---- the dialog -------------------------------------------------------------------------------------------------
  var open = null;       // {kind, root, form, ctx}

  function values(form) {
    var v = {};
    form.querySelectorAll("[data-field]").forEach(function (f) {
      var key = f.getAttribute("data-field");
      if (key.indexOf("c-") === 0) return;
      v[key] = f.type === "checkbox" ? f.checked : f.value;
    });
    if (open && open.kind.collect) open.kind.collect(v, form);
    return v;
  }

  function refresh() {
    if (!open) return;
    var v = values(open.form), problem = open.kind.problem(v, open.ctx);
    var line = open.root.querySelector("[data-problem]");
    line.textContent = problem; line.hidden = !problem;
    open.root.querySelector("[data-dlg-save]").disabled = !!problem;
    open.root.querySelector("[data-sql]").textContent = problem ? "-- " + problem : open.kind.sql(v, open.ctx) + ";";
  }

  function showTab(name) {
    open.root.querySelectorAll(".dlg-tab").forEach(function (t) { t.classList.toggle("active", t.getAttribute("data-tab") === name); });
    open.root.querySelectorAll(".dlg-panel").forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
  }

  function close() {
    if (!open) return;
    open.root.remove();
    open = null;
  }

  function finished(ctx, effect, message) {
    toast(message);
    document.dispatchEvent(new CustomEvent("pgdas:changed", {detail: Object.assign({server: ctx.server}, effect)}));
  }

  function save() {
    if (!open) return;
    var current = open, v = values(current.form);
    if (current.kind.problem(v, current.ctx)) return;
    var button = current.root.querySelector("[data-dlg-save]"), line = current.root.querySelector("[data-problem]");
    button.disabled = true;
    window.pgdasReview.start({
      server: current.ctx.server, db: current.ctx.db, sql: current.kind.sql(v, current.ctx),
      onError: function (message) { line.textContent = message; line.hidden = false; },
      onSuccess: function (result) { close(); finished(current.ctx, current.kind.after(v, current.ctx), result.message || current.kind.done); },
    }).then(function () { if (open === current) button.disabled = !!current.kind.problem(values(current.form), current.ctx); });
  }

  function openDialog(kindName, ctx) {
    close();
    var kind = KINDS[kindName];
    var form = el("div", {"class": "dlg-panel", "data-panel": "general"});
    var sqlPanel = el("div", {"class": "dlg-panel", "data-panel": "sql", hidden: "hidden"}, [el("pre", {"class": "sql dlg-sql", "data-sql": ""})]);
    var datalist = el("datalist", {id: "tdlgTypes"});
    TYPES.forEach(function (t) { datalist.appendChild(el("option", {value: t[0], label: t[1]})); });
    var root = el("div", {"class": "cyber-modal", "data-tdlg": ""}, [el("div", {"class": "cyber-dialog dlg tdlg", role: "dialog", "aria-modal": "true"}, [
      el("div", {"class": "modal-top"}, [el("span", {text: kind.title}), el("button", {type: "button", "data-dlg-close": "", "aria-label": "Close", text: "✕"})]),
      el("div", {"class": "dlg-tabs", role: "tablist"}, [
        el("button", {type: "button", "class": "dlg-tab active", "data-tab": "general", text: "General"}),
        el("button", {type: "button", "class": "dlg-tab", "data-tab": "sql", text: "SQL"})]),
      form, sqlPanel, datalist,
      el("p", {"class": "dlg-problem", "data-problem": "", hidden: "hidden"}),
      el("div", {"class": "dlg-foot"}, [
        el("button", {type: "button", "class": "btn", "data-dlg-close": "", text: "Close"}),
        el("button", {type: "button", "class": "btn primary", "data-dlg-save": "", disabled: "disabled", text: "Save"})]),
    ])]);
    open = {kind: kind, root: root, form: form, ctx: ctx};
    kind.build(form, ctx);
    document.body.appendChild(root);
    refresh();
    var first = form.querySelector("input:not([readonly]):not([disabled])");
    if (first) first.focus();
  }

  // Dropping goes straight to the review popup, which asks for the word DROP / ALTER.
  function review(ctx, sql, effect, message) {
    window.pgdasReview.start({
      server: ctx.server, db: ctx.db, sql: sql,
      onError: function (text) { toast(text); },
      onSuccess: function (result) { finished(ctx, effect, result.message || message); },
    });
  }

  function structure(node) {
    var holder = node.closest("[data-structure]");
    return holder ? JSON.parse(holder.getAttribute("data-structure")) : null;
  }

  function columnOf(button) {
    var line = button.closest("tr[data-column]");
    return line ? {name: line.getAttribute("data-column"), type: line.getAttribute("data-type"), nullable: line.getAttribute("data-nullable") === "1",
                   default: line.getAttribute("data-default") || "", identity: line.getAttribute("data-identity") || "",
                   generated: line.getAttribute("data-generated") || ""} : null;
  }

  // ---- moving columns on the Columns tab; Apply rebuilds the table -----------------------------------------------
  function renumber(table) {
    var names = [];
    table.querySelectorAll("tr[data-column]").forEach(function (line, index) {
      line.querySelector("[data-number]").textContent = index + 1;
      names.push(line.getAttribute("data-column"));
    });
    return names;
  }

  function move(button, step) {
    var line = button.closest("tr[data-column]"), table = line.closest("table"), holder = table.closest("[data-columns-tab]");
    var other = step < 0 ? line.previousElementSibling : line.nextElementSibling;
    if (!other || !other.hasAttribute("data-column")) return;
    if (step < 0) line.parentNode.insertBefore(line, other); else line.parentNode.insertBefore(other, line);
    var names = renumber(table), original = JSON.parse(holder.getAttribute("data-columns-tab"));
    var changed = names.join("\u0000") !== original.join("\u0000");
    holder.querySelector("[data-order-bar]").hidden = !changed;
    table.querySelectorAll("tr[data-column]").forEach(function (l) { l.classList.toggle("moved", changed && original.indexOf(l.getAttribute("data-column")) !== names.indexOf(l.getAttribute("data-column"))); });
  }

  function applyOrder(button) {
    var holder = button.closest("[data-columns-tab]"), ctx = structure(button);
    var names = renumber(holder.querySelector("table"));
    review(ctx, "ALTER TABLE " + qualified(ctx) + " REORDER COLUMNS (" + names.map(ident).join(", ") + ")", {redraw: true}, "Columns reordered.");
  }

  function resetOrder(button) {
    var holder = button.closest("[data-columns-tab]"), body = holder.querySelector("tbody");
    JSON.parse(holder.getAttribute("data-columns-tab")).forEach(function (name) {
      var line = Array.prototype.find.call(body.querySelectorAll("tr[data-column]"), function (l) { return l.getAttribute("data-column") === name; });
      body.appendChild(line);
    });
    renumber(holder.querySelector("table"));
    holder.querySelector("[data-order-bar]").hidden = true;
    body.querySelectorAll("tr.moved").forEach(function (l) { l.classList.remove("moved"); });
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-table-dialog]");
    if (trigger) {
      var ctx = structure(trigger);
      if (!ctx) return;
      var what = trigger.getAttribute("data-table-dialog");
      ctx.column = columnOf(trigger);
      var indexRow = trigger.closest("tr[data-index]"), indexTab = trigger.closest("[data-index-tab]");
      ctx.index = indexRow ? indexRow.getAttribute("data-index") : "";
      ctx.columns = indexTab ? JSON.parse(indexTab.getAttribute("data-index-tab")) : [];
      if (what === "drop-index") review(ctx, "DROP INDEX " + ident(ctx.schema) + "." + ident(ctx.index), {redraw: true}, "Index dropped.");
      else if (what === "reindex-index") review(ctx, "REINDEX INDEX " + ident(ctx.schema) + "." + ident(ctx.index), {redraw: true}, "Index rebuilt.");
      else if (what === "reindex-table") review(ctx, "REINDEX TABLE " + qualified(ctx), {redraw: true}, "Indexes rebuilt.");
      else if (what === "drop-table") review(ctx, "DROP TABLE " + qualified(ctx), {node: folder(ctx), open: folder(ctx)}, "Table dropped.");
      else if (what === "drop-column") review(ctx, "ALTER TABLE " + qualified(ctx) + " DROP COLUMN " + ident(ctx.column.name), {redraw: true}, "Column dropped.");
      else openDialog(what, ctx);
      return;
    }
    var moveButton = event.target.closest("[data-col-move]");
    if (moveButton) { move(moveButton, parseInt(moveButton.getAttribute("data-col-move"), 10)); return; }
    var apply = event.target.closest("[data-order-apply]");
    if (apply) { applyOrder(apply); return; }
    var undo = event.target.closest("[data-order-undo]");
    if (undo) { resetOrder(undo); return; }
    if (!open) return;
    var tab = event.target.closest(".dlg-tab");
    if (tab && open.root.contains(tab)) { showTab(tab.getAttribute("data-tab")); return; }
    if (event.target.closest("[data-dlg-close]") && open.root.contains(event.target)) { close(); return; }
    if (event.target.closest("[data-dlg-save]") && open.root.contains(event.target)) { save(); return; }
    if (event.target.closest("[data-col-add]") && open.root.contains(event.target)) { addColumn(open.form); refresh(); return; }
    var removeColumn = event.target.closest("[data-col-remove]");
    if (removeColumn && open.root.contains(removeColumn)) { removeColumn.closest("[data-col]").remove(); refresh(); return; }
    if (event.target === open.root) close();
  });
  document.addEventListener("input", function (event) { if (open && open.root.contains(event.target)) refresh(); });
  document.addEventListener("change", function (event) { if (open && open.root.contains(event.target)) refresh(); });
  document.addEventListener("keydown", function (event) {
    var reviewBox = document.getElementById("changeModal");
    if (event.key === "Escape" && open && (!reviewBox || reviewBox.hidden)) close();
  });
})();
