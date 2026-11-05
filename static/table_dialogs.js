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
  TYPES = TYPES.map(function (t) { return [t[0].replace(/\([^)]*\)/g, ""), t[1]]; }).concat([
    ["varchar", "variable-length text"], ["char", "fixed-length text"], ["decimal", "exact decimal (numeric)"],
    ["float", "floating point with binary precision"], ["timestamptz", "timestamp with time zone"],
    ["timestamp without time zone", "timestamp without time zone"], ["time without time zone", "time without time zone"],
    ["int4range", "integer range"], ["int8range", "bigint range"], ["numrange", "numeric range"],
    ["daterange", "date range"], ["tsrange", "timestamp range"], ["tstzrange", "timestamp with time zone range"],
    ["int4multirange", "integer multirange"], ["int8multirange", "bigint multirange"], ["nummultirange", "numeric multirange"],
    ["datemultirange", "date multirange"], ["tsmultirange", "timestamp multirange"], ["tstzmultirange", "timestamp with time zone multirange"],
    ["integer[]", "array of integers"], ["text[]", "array of text"], ["numeric[]", "array of decimals"],
    ["uuid[]", "array of UUIDs"], ["jsonb[]", "array of JSON values"]
  ]);

  function typeSpec(value) {
    var match = /^\s*([a-z]+(?:\s+varying)?)(?:\s*\(\s*([+-]?\d+)\s*(?:,\s*([+-]?\d+)\s*)?\))?(\s+(?:with|without)\s+time\s+zone)?\s*((?:\[\s*\]\s*)*)$/i.exec(value);
    if (!match) return null;
    var base = match[1].toLowerCase().replace(/\s+/g, " "), mode, max;
    if (/^(varchar|char|character|character varying|bit|bit varying|varbit)$/.test(base)) { mode = "length"; max = /bit/.test(base) ? null : 10485760; }
    else if (/^(numeric|decimal)$/.test(base)) { mode = "numeric"; max = 1000; }
    else if (/^(timestamp|timestamptz|time|timetz|interval)$/.test(base)) { mode = "time"; max = 6; }
    else if (base === "float") { mode = "float"; max = 53; }
    else return null;
    if (match[3] !== undefined && mode !== "numeric") return null;
    if (match[4] && base !== "timestamp" && base !== "time") return null;
    return {base: base, zone: match[4] || "", array: (match[5] || "").replace(/\s/g, ""), precision: match[2], scale: match[3], mode: mode, max: max};
  }

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message; toastEl.className = "toast"; toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 6000);
  }

  function ident(name) { return '"' + name.replace(/"/g, '""') + '"'; }
  function qualified(ctx) { return ident(ctx.schema) + "." + ident(ctx.table); }
  function literal(text) { return "'" + text.replace(/'/g, "''") + "'"; }

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
  // A searchable type list: click or focus to see every type, type to narrow it (matches the name or the description, any order of
  // words), ▲ ▼ Enter or a click to pick. Anything typed is still accepted as it is (varchar(50), text[], a type of your own).
  var extraTypes = [];            // the database's own types, once they have arrived
  var comboCount = 0;
  function typeInput(field) {
    var id = "tdlgType" + (++comboCount);
    var input = textInput(field, "search or type, e.g. integer or varchar(50)");
    input.setAttribute("role", "combobox"); input.setAttribute("aria-expanded", "false"); input.setAttribute("aria-controls", id); input.setAttribute("aria-autocomplete", "list");
    var list = el("ul", {"class": "combo-list", id: id, role: "listbox", hidden: "hidden"});
    var wrap = el("div", {"class": "combo"}, [input, list]);
    var size = el("input", {type: "number", step: "1", "aria-label": "Length / Precision", placeholder: "Default"});
    var scale = el("input", {type: "number", step: "1", min: "-1000", max: "1000", "aria-label": "Scale", placeholder: "Default"});
    var sizeText = el("span", {text: "Length / Precision"});
    var sizeLabel = el("label", {}, [sizeText, size]);
    var scaleLabel = el("label", {}, [document.createTextNode("Scale"), scale]);
    var parameters = el("div", {"class": "type-parameters", hidden: "hidden"}, [sizeLabel, scaleLabel]);
    wrap.appendChild(parameters);
    var spec = null, specKey = "";
    function syncType() {
      spec = typeSpec(input.value);
      var key = spec ? spec.base + spec.zone + spec.array : "";
      if (key !== specKey) { size.value = ""; scale.value = ""; }
      specKey = key;
      parameters.hidden = !spec;
      if (!spec) return;
      scaleLabel.hidden = spec.mode !== "numeric";
      sizeText.textContent = spec.mode === "length" ? "Length" : "Precision";
      size.min = spec.mode === "time" ? "0" : "1";
      if (spec.max !== null) size.max = String(spec.max); else size.removeAttribute("max");
      size.title = spec.mode === "time" ? "Fractional-second precision: 0 to 6" : spec.mode === "numeric" ? "Total digits: 1 to 1000" : "Optional length or precision";
      scale.title = "Decimal digits; negative scale requires PostgreSQL 15 or later";
      if (spec.precision !== undefined) {
        size.value = spec.precision; scale.value = spec.scale === undefined ? "" : spec.scale;
        input.value = spec.base + spec.zone + spec.array;
      }
    }
    input._typeSql = function () {
      if (!spec) return input.value;
      return spec.base + (size.value !== "" ? "(" + size.value + (spec.mode === "numeric" && scale.value !== "" ? "," + scale.value : "") + ")" : "") + spec.zone + spec.array;
    };
    input._typeProblem = function () {
      if (!spec) return "";
      if (!size.validity.valid || (spec.mode === "numeric" && !scale.validity.valid)) return "Enter a whole-number length/precision and scale within the supported limits.";
      if (spec.mode === "numeric" && scale.value !== "" && size.value === "") return "Enter precision before scale.";
      return "";
    };
    input.addEventListener("input", syncType);
    var shown = [], active = -1, picking = false;

    function matches(query) {
      var words = query.toLowerCase().split(/\s+/).filter(Boolean);
      return TYPES.concat(extraTypes.map(function (t) { return [t.name, t.kind]; })).filter(function (t) {
        var hay = (t[0] + " " + t[1]).toLowerCase();
        return words.every(function (w) { return hay.indexOf(w) >= 0; });
      }).sort(function (a, b) {
        // name starts with the first word, then name contains every word, then only the description matches
        var rank = function (t) {
          var name = t[0].toLowerCase();
          return name.indexOf(words[0] || "") === 0 ? 0 : words.every(function (w) { return name.indexOf(w) >= 0; }) ? 1 : 2;
        };
        return rank(a) - rank(b);
      });
    }

    function render(query) {
      list.textContent = "";
      shown = matches(query || "");
      active = shown.length ? 0 : -1;
      shown.forEach(function (t, index) {
        var item = el("li", {role: "option", id: id + "-" + index, "class": "combo-item" + (index === active ? " active" : "")},
                      [el("span", {"class": "combo-name", text: t[0]}), el("span", {"class": "combo-note muted", text: t[1]})]);
        item.addEventListener("mousedown", function (event) { event.preventDefault(); });
        item.addEventListener("click", function (event) { event.stopPropagation(); pick(index); });
        list.appendChild(item);
      });
      if (!shown.length) list.appendChild(el("li", {"class": "combo-empty muted", text: "No listed type matches — what you typed is used as it is."}));
      input.setAttribute("aria-activedescendant", active >= 0 ? id + "-" + active : "");
    }

    function show(query) {
      render(query);
      var box = input.getBoundingClientRect();
      list.style.left = box.left + "px"; list.style.top = box.bottom + 2 + "px"; list.style.width = Math.max(box.width, 320) + "px";
      list.style.maxHeight = Math.max(120, window.innerHeight - box.bottom - 16) + "px";
      list.hidden = false; input.setAttribute("aria-expanded", "true");
    }

    function hide() { list.hidden = true; input.setAttribute("aria-expanded", "false"); }

    function pick(index) {
      input.value = shown[index][0];
      picking = true;
      input.dispatchEvent(new Event("input", {bubbles: true}));          // so the dialog rebuilds its SQL
      picking = false;
      hide();
    }

    function move(step) {
      if (!shown.length) return;
      list.children[active] && list.children[active].classList.remove("active");
      active = (active + step + shown.length) % shown.length;
      var item = list.children[active];
      item.classList.add("active"); item.scrollIntoView({block: "nearest"});
      input.setAttribute("aria-activedescendant", item.id);
    }

    input.addEventListener("focus", function () { show(""); input.select(); });         // the whole list first, whatever the box already holds
    input.addEventListener("click", function () { if (list.hidden) show(""); });
    input.addEventListener("input", function () { if (!picking) show(input.value); });
    input.addEventListener("blur", function () { hide(); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (list.hidden) show(input.value); else move(event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter" && !list.hidden && active >= 0 && shown.length) {
        event.preventDefault(); event.stopPropagation(); pick(active);
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault(); event.stopPropagation(); hide();                          // closes the list, not the dialog
      }
    });
    wrap.input = input;
    wrap.sync = syncType;
    syncType();
    return wrap;
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
      tabs: [["general", "General"], ["columns", "Columns"], ["constraints", "Constraints"], ["advanced", "Advanced"], ["security", "Security"], ["sql", "SQL"]],
      build: function (form, ctx, panels) {
        form.appendChild(row("Schema", el("input", {type: "text", value: ctx.schema, readonly: "readonly"})));
        form.appendChild(row("Table", textInput("name")));
        form.appendChild(row("Owner", el("select", {"data-field": "owner", "data-roles": "owner"})));
        form.appendChild(row("Comment", el("textarea", {"data-field": "comment", rows: "3", maxlength: "2000", placeholder: "optional: what this table is for"})));
        fillRoles();
        panels.advanced.appendChild(checkbox("unlogged", "Unlogged (faster to write, but emptied after a crash and not copied to standby servers)"));
        panels.advanced.appendChild(row("Fill factor", el("input", {type: "number", "data-field": "fillfactor", min: "10", max: "100", placeholder: "10 – 100, empty = default (100)"})));
        panels.advanced.appendChild(el("p", {"class": "dlg-hint", text: "A lower fill factor leaves room in each page for updates, which can make a table that is updated a lot faster. Leave it empty if unsure."}));
        panels.security.appendChild(el("p", {"class": "dlg-grants-head"}, [el("strong", {text: "Privileges"}),
          el("button", {type: "button", "class": "btn", "data-priv-add": "", text: "＋ Add"})]));
        panels.security.appendChild(el("p", {"class": "dlg-hint", text: "Who may do what with the new table. Leave it empty to keep PostgreSQL's defaults (only the owner). Each row becomes a GRANT run right after the table is created."}));
        panels.security.appendChild(el("div", {"data-privs": ""}));
        panels.columns.appendChild(el("p", {"class": "dlg-grants-head"}, [el("strong", {text: "Columns"}),
          el("button", {type: "button", "class": "btn", "data-col-add": "", text: "＋ Add column"})]));
        panels.columns.appendChild(el("div", {"data-cols": ""}));
        addColumn(panels.columns, {name: "id", type: "bigserial", pk: true});
        open.constraints = window.pgdasConstraints.editor({ctx: ctx});
        panels.constraints.appendChild(open.constraints.el);
      },
      collect: function (v) {
        v.cols = columns(open.panels.columns);
        open.constraints.setColumns(v.cols.map(function (c) { return c.name.trim(); }).filter(Boolean));     // the constraints choose among these
        v.constraints = open.constraints.read();
        var advanced = open.panels.advanced;
        v.unlogged = advanced.querySelector("[data-field=unlogged]").checked;
        v.fillfactor = advanced.querySelector("[data-field=fillfactor]").value.trim();
        v.grants = privRows(open.panels.security);
        return v;
      },
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
        if (!v.cols.length) return "Add at least one column.";
        if (v.fillfactor !== "" && !(/^\d+$/.test(v.fillfactor) && +v.fillfactor >= 10 && +v.fillfactor <= 100)) return "Advanced: the fill factor must be a whole number from 10 to 100.";
        if (v.grants.some(function (g) { return g.privs.length && !g.to; })) return "Security: choose a role for every row that has privileges.";
        var constraintProblem = window.pgdasConstraints.problem(v.constraints);
        if (constraintProblem) return constraintProblem;
        if (v.cols.some(function (c) { return c.pk; }) && v.constraints.some(function (c) { return c.type === "PRIMARY KEY"; })) {
          return "A key box is ticked on the Columns tab and there is also a Primary key constraint. Use one of them.";
        }
        return "";
      },
      sql: function (v, ctx) {
        var lines = v.cols.map(function (c) { return "    " + columnSql(c); });
        var keys = v.cols.filter(function (c) { return c.pk; }).map(function (c) { return ident(c.name.trim()); });
        if (keys.length) lines.push("    PRIMARY KEY (" + keys.join(", ") + ")");
        v.constraints.forEach(function (c) { lines.push("    " + window.pgdasConstraints.sql(c)); });
        var target = ident(ctx.schema) + "." + ident(v.name.trim());
        var statements = ["CREATE " + (v.unlogged ? "UNLOGGED " : "") + "TABLE " + target + " (\n" + lines.join(",\n") + "\n)" +
                          (v.fillfactor !== "" ? " WITH (fillfactor = " + parseInt(v.fillfactor, 10) + ")" : "")];
        if (v.owner) statements.push("ALTER TABLE " + target + " OWNER TO " + ident(v.owner));
        if (v.comment.trim()) statements.push("COMMENT ON TABLE " + target + " IS " + literal(v.comment.trim()));
        v.grants.filter(function (g) { return g.to && g.privs.length; }).forEach(function (g) {
          var privs = g.privs.indexOf("ALL") >= 0 ? "ALL PRIVILEGES" : g.privs.join(", ");
          statements.push("GRANT " + privs + " ON TABLE " + target + " TO " + (g.to === "PUBLIC" ? "PUBLIC" : ident(g.to)) + (g.option ? " WITH GRANT OPTION" : ""));
        });
        return statements.join(";\n");
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
        var type = typeInput("type"); type.input.value = c.type; type.sync();
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
    name.value = start.name || ""; type.input.value = start.type || "";
    type.sync();
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
      return {name: get("c-name").value, type: get("c-type")._typeSql(), default: get("c-default").value, notnull: get("c-notnull").checked, pk: get("c-pk").checked};
    });
  }

  // ---- the dialog -------------------------------------------------------------------------------------------------
  var open = null;       // {kind, root, form, ctx}

  function values(form) {
    var v = {};
    form.querySelectorAll("[data-field]").forEach(function (f) {
      var key = f.getAttribute("data-field");
      if (key.indexOf("c-") === 0) return;
      v[key] = f.type === "checkbox" ? f.checked : f._typeSql ? f._typeSql() : f.value;
    });
    if (open && open.kind.collect) open.kind.collect(v, form);
    return v;
  }

  function formProblem(current, v) {
    var problem = current.kind.problem(v, current.ctx);
    current.form.querySelectorAll('[role="combobox"]').forEach(function (input) { if (!problem) problem = input._typeProblem(); });
    return problem;
  }

  function refresh() {
    if (!open) return;
    var v = values(open.form), problem = formProblem(open, v);
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
    if (formProblem(current, v)) return;
    var button = current.root.querySelector("[data-dlg-save]"), line = current.root.querySelector("[data-problem]");
    button.disabled = true;
    window.pgdasReview.start({
      server: current.ctx.server, db: current.ctx.db, sql: current.kind.sql(v, current.ctx),
      onError: function (message) { line.textContent = message; line.hidden = false; },
      onSuccess: function (result) { close(); finished(current.ctx, current.kind.after(v, current.ctx), result.message || current.kind.done); },
    }).then(function () { if (open === current) button.disabled = !!formProblem(current, values(current.form)); });
  }

  // The types this database adds (enums, domains, composite and range types, extension types) join the suggestions once they arrive.
  var typeCache = {};
  var roleCache = {}, extraRoles = [];
  function addDatabaseTypes(ctx) {
    extraTypes = []; extraRoles = [];
    if (!ctx.types_url) return;
    if (typeCache[ctx.types_url]) { extraTypes = typeCache[ctx.types_url]; extraRoles = roleCache[ctx.types_url] || []; return; }
    fetch(ctx.types_url, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
      .then(function (r) { return r.ok ? r.json() : {types: [], roles: []}; })
      .then(function (data) {
        typeCache[ctx.types_url] = data.types || []; roleCache[ctx.types_url] = data.roles || [];
        extraTypes = typeCache[ctx.types_url]; extraRoles = roleCache[ctx.types_url];
        fillRoles();
      })
      .catch(function () { /* the built-in types are still offered */ });
  }

  // ---- roles: the owner of a new table and the rows of its Security tab ------------------------------------------------
  function fillRoles() {
    if (!open) return;
    open.root.querySelectorAll("select[data-roles]").forEach(function (select) {
      var kept = select.value, owner = select.getAttribute("data-roles") === "owner";
      select.textContent = "";
      select.appendChild(el("option", {value: "", text: owner ? "the login PGDAS uses" : "choose a role…"}));
      if (!owner) select.appendChild(el("option", {value: "PUBLIC", text: "PUBLIC (everyone)"}));
      extraRoles.forEach(function (name) { select.appendChild(el("option", {value: name, text: name})); });
      select.value = kept;
    });
  }

  var PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"];
  function addPrivRow(panel) {
    var boxes = el("div", {"class": "dlg-grant-boxes"});
    ["ALL"].concat(PRIVILEGES).forEach(function (name) {
      boxes.appendChild(el("label", {"class": "dlg-grant-line"}, [el("input", {type: "checkbox", "data-priv": name}), document.createTextNode(" " + name)]));
    });
    boxes.appendChild(el("label", {"class": "dlg-grant-line muted"}, [el("input", {type: "checkbox", "data-priv-option": ""}), document.createTextNode(" WITH GRANT OPTION")]));
    panel.querySelector("[data-privs]").appendChild(el("div", {"class": "dlg-grant", "data-priv-row": ""}, [
      el("select", {"data-roles": "grantee", "data-grantee": ""}), boxes,
      el("button", {type: "button", "class": "btn", "data-priv-remove": "", title: "Remove this row", text: "✕"})]));
    fillRoles();
  }
  function privRows(panel) {
    return Array.prototype.map.call(panel.querySelectorAll("[data-priv-row]"), function (row) {
      return {to: row.querySelector("[data-grantee]").value, option: row.querySelector("[data-priv-option]").checked,
              privs: Array.prototype.filter.call(row.querySelectorAll("[data-priv]"), function (b) { return b.checked; }).map(function (b) { return b.getAttribute("data-priv"); })};
    });
  }
  function syncPrivRow(row, changed) {            // ALL ticks or clears the others; ticking the others by hand updates ALL
    var all = row.querySelector('[data-priv="ALL"]');
    var others = PRIVILEGES.map(function (n) { return row.querySelector('[data-priv="' + n + '"]'); });
    if (changed === all) others.forEach(function (b) { b.checked = all.checked; });
    else all.checked = others.every(function (b) { return b.checked; });
  }

  function openDialog(kindName, ctx) {
    close();
    var kind = KINDS[kindName];
    var form = el("div", {"class": "dlg-panel", "data-panel": "general"});
    var sqlPanel = el("div", {"class": "dlg-panel", "data-panel": "sql", hidden: "hidden"}, [el("pre", {"class": "sql dlg-sql", "data-sql": ""})]);
    // Most dialogs are General + SQL; a dialog may name more tabs (kind.tabs) and gets an empty panel for each in `panels`.
    var tabList = kind.tabs || [["general", "General"], ["sql", "SQL"]], panels = {general: form}, middle = [form];
    tabList.forEach(function (t) {
      if (t[0] === "general" || t[0] === "sql") return;
      panels[t[0]] = el("div", {"class": "dlg-panel", "data-panel": t[0], hidden: "hidden"});
      middle.push(panels[t[0]]);
    });
    var root = el("div", {"class": "cyber-modal", "data-tdlg": ""}, [el("div", {"class": "cyber-dialog dlg tdlg", role: "dialog", "aria-modal": "true"}, [
      el("div", {"class": "modal-top"}, [el("span", {text: kind.title}), el("button", {type: "button", "data-dlg-close": "", "aria-label": "Close", text: "✕"})]),
      el("div", {"class": "dlg-tabs", role: "tablist"}, tabList.map(function (t, index) {
        return el("button", {type: "button", "class": "dlg-tab" + (index === 0 ? " active" : ""), "data-tab": t[0], text: t[1]});
      })),
    ].concat(middle, [sqlPanel,
      el("p", {"class": "dlg-problem", "data-problem": "", hidden: "hidden"}),
      el("div", {"class": "dlg-foot"}, [
        el("button", {type: "button", "class": "btn", "data-dlg-close": "", text: "Close"}),
        el("button", {type: "button", "class": "btn primary", "data-dlg-save": "", disabled: "disabled", text: "Save"})]),
    ]))]);
    open = {kind: kind, root: root, form: form, ctx: ctx, panels: panels};
    addDatabaseTypes(ctx);
    kind.build(form, ctx, panels);
    document.body.appendChild(root);
    refresh();
    var first = form.querySelector("input:not([readonly]):not([disabled]):not([role=combobox])");     // not the type list: it would open at once
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
    if (event.target.closest("[data-col-add]") && open.root.contains(event.target)) { addColumn(open.panels.columns || open.form); refresh(); return; }
    if (event.target.closest("[data-priv-add]") && open.root.contains(event.target)) { addPrivRow(open.panels.security); refresh(); return; }
    var removePriv = event.target.closest("[data-priv-remove]");
    if (removePriv && open.root.contains(removePriv)) { removePriv.closest("[data-priv-row]").remove(); refresh(); return; }
    var removeColumn = event.target.closest("[data-col-remove]");
    if (removeColumn && open.root.contains(removeColumn)) { removeColumn.closest("[data-col]").remove(); refresh(); return; }
    if (event.target === open.root) close();
  });
  document.addEventListener("input", function (event) { if (open && open.root.contains(event.target)) refresh(); });
  document.addEventListener("change", function (event) {
    if (!open || !open.root.contains(event.target)) return;
    var privRow = event.target.closest("[data-priv-row]");
    if (privRow && event.target.hasAttribute("data-priv")) syncPrivRow(privRow, event.target);
    refresh();
  });
  document.addEventListener("keydown", function (event) {
    var reviewBox = document.getElementById("changeModal");
    if (event.key === "Escape" && open && (!reviewBox || reviewBox.hidden)) close();
  });
})();
