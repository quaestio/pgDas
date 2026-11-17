// SQL runner helpers: the tables and columns of the chosen database on the right (click one to put it in the SQL box), hints while you type,
// and a query builder that writes a SELECT for you. Nothing here runs anything: it only puts text in the box, and Run works as before.
// Names from the database are put in the page as text, never as HTML.
(function () {
  "use strict";
  var box = document.getElementById("sql"), panel = document.getElementById("sqlHelper"), strip = document.getElementById("sqlSuggest");
  if (!box || !panel) { return; }
  var tables = [];
  var KEYWORDS = ["SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "LIMIT", "OFFSET", "JOIN", "LEFT JOIN", "INNER JOIN", "ON", "AND", "OR", "NOT", "NULL", "IS NULL", "IS NOT NULL",
    "IN", "LIKE", "ILIKE", "BETWEEN", "DISTINCT", "COUNT(*)", "SUM(", "AVG(", "MIN(", "MAX(", "AS", "HAVING", "UNION", "INSERT INTO", "VALUES", "UPDATE", "SET", "DELETE FROM",
    "CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CASE", "WHEN", "THEN", "ELSE", "END", "EXISTS", "ASC", "DESC", "WITH", "RETURNING", "NOW()", "COALESCE(", "CAST(", "LOWER(", "UPPER(", "LENGTH("];
  var RESERVED = (window.pgdasSqlWords && window.pgdasSqlWords.reserved) || {};              // the same list the box colours (sql_highlight.js)
  var NUMERIC = /^(smallint|integer|bigint|numeric|decimal|real|double precision|smallserial|serial|bigserial|money|oid)\b/i;

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function ident(name) { return /^[a-z_][a-z0-9_$]*$/.test(name) && !RESERVED[name] ? name : '"' + name.replace(/"/g, '""') + '"'; }
  function qualified(t) { return t.schema === "public" ? ident(t.name) : ident(t.schema) + "." + ident(t.name); }
  function literal(text) { return "'" + text.replace(/'/g, "''") + "'"; }
  function find(key) { return tables.filter(function (t) { return t.schema + "." + t.name === key; })[0]; }

  // ---- putting text in the box ------------------------------------------------------------------------------------------------------------
  function changed() { box.dispatchEvent(new Event("input", {bubbles: true})); }
  // The statement the cursor is in: [start, end) between the neighbouring semicolons, and where its select list is (SELECT [DISTINCT] <list> FROM).
  function statementAt(position) {
    var value = box.value, start = value.lastIndexOf(";", position - 1) + 1, end = value.indexOf(";", position);
    if (end < 0) { end = value.length; }
    var part = value.slice(start, end), m = /^(\s*SELECT\s+(?:DISTINCT\s+)?)([\s\S]*?)(\s+FROM\b)/i.exec(part);
    return {start: start, end: end, list: m ? [start + m[1].length, start + m[1].length + m[2].length, m[2].trim()] : null};
  }
  function put(text, isColumn) {
    var start = box.selectionStart, end = box.selectionEnd, here = statementAt(start);
    // In the select list of SELECT * FROM t, the first column replaces the star; the next ones get a comma before them.
    if (isColumn && here.list && start >= here.list[0] && start <= here.list[1] && here.list[2] === "*") {
      var star = here.list[0] + box.value.slice(here.list[0], here.list[1]).indexOf("*");
      box.focus();
      box.setRangeText(text, star, star + 1, "end");
      changed();
      return;
    }
    var before = box.value.slice(here.start, start).replace(/^\s+/, ""), glue = "";
    if (before && !/[\s(.,]$/.test(before)) { glue = " "; }
    if (isColumn && before.trim()) {
      var last = /([A-Za-z_]+|\S)\s*$/.exec(before);
      var token = last ? last[1] : "";
      var opens = /^(select|distinct|where|and|or|on|by|set|having|when|then|else|not|using|returning|case)$/i.test(token) || /^[,(=<>+\-*\/|!~]$/.test(token);
      glue = opens ? (/\s$/.test(before) || /[(]$/.test(before) ? "" : " ") : (/[,]\s*$/.test(before) ? "" : ", ");
    }
    box.focus();
    box.setRangeText(glue + text, start, end, "end");
    changed();
  }
  function putStatement(sql) {                       // a whole statement replaces what was in the box
    box.value = sql;
    box.focus(); box.setSelectionRange(box.value.length, box.value.length);
    changed();
  }
  // Clicking a table writes SELECT * FROM <table> LIMIT 10; in place of what was in the box, with the cursor on the star so the next column
  // clicked takes its place. After FROM, JOIN, INTO or UPDATE it puts just the name where the cursor is, so a statement being written is not lost.
  function putTable(t) {
    var name = qualified(t), before = box.value.slice(0, box.selectionStart);
    if (/\b(from|join|into|update|table)\s*$/i.test(before)) { put(name, false); return; }
    box.value = "SELECT * FROM " + name + " LIMIT 10;";
    box.focus(); box.setSelectionRange(7, 8);                // the star, selected: a column replaces it
    changed();
  }

  // ---- the panel: Browse and Query builder ------------------------------------------------------------------------------------------------
  var tabs = el("div", {"class": "sqlh-tabs", role: "tablist"}), browse = el("div", {"class": "sqlh-pane"}), builder = el("div", {"class": "sqlh-pane", hidden: "hidden"});
  var tabBrowse = el("button", {type: "button", "class": "sqlh-tab active"}, "Tables and columns"), tabBuild = el("button", {type: "button", "class": "sqlh-tab"}, "Query builder");
  tabs.appendChild(tabBrowse); tabs.appendChild(tabBuild);
  function show(which) {
    browse.hidden = which !== "browse"; builder.hidden = which !== "build";
    tabBrowse.classList.toggle("active", which === "browse"); tabBuild.classList.toggle("active", which === "build");
  }
  tabBrowse.addEventListener("click", function () { show("browse"); });
  tabBuild.addEventListener("click", function () { show("build"); });

  var filter = el("input", {type: "text", "class": "sqlh-filter", placeholder: "Find a table or column…", autocomplete: "off", spellcheck: "false", "aria-label": "Find a table or column"});
  var list = el("ul", {"class": "sqlh-list"});
  browse.appendChild(filter);
  browse.appendChild(el("p", {"class": "muted sqlh-help"}, "Click a table to get SELECT * FROM it LIMIT 10; then click columns to choose them. ▸ shows a table's columns."));
  browse.appendChild(list);

  function renderList() {
    var needle = filter.value.trim().toLowerCase(), shown = 0;
    list.textContent = "";
    tables.forEach(function (t) {
      var tableHit = !needle || (t.schema + "." + t.name).toLowerCase().indexOf(needle) >= 0;
      var columns = t.columns.filter(function (c) { return !needle || tableHit || c.name.toLowerCase().indexOf(needle) >= 0; });
      if (!tableHit && !columns.length) { return; }
      if (shown >= 300 && !needle) { return; }
      shown += 1;
      var item = el("li"), head = el("div", {"class": "sqlh-table"});
      var toggle = el("button", {type: "button", "class": "sqlh-toggle", "aria-label": "Show the columns of " + t.name, "aria-expanded": "false"}, "▸");
      var name = el("button", {type: "button", "class": "sqlh-name", title: "SELECT * FROM " + qualified(t) + " LIMIT 10;"}, (t.schema === "public" ? "" : t.schema + ".") + t.name);
      var kind = t.kind && t.kind !== "table" ? el("span", {"class": "muted sqlh-kind"}, t.kind) : null;
      var cols = el("ul", {"class": "sqlh-cols", hidden: "hidden"});
      columns.forEach(function (c) {
        var line = el("li"), button = el("button", {type: "button", "class": "sqlh-col", title: "Put " + ident(c.name) + " in the SQL box"}, c.name);
        button.appendChild(el("span", {"class": "muted"}, " " + c.type));
        button.addEventListener("click", function () { put(ident(c.name), true); });
        line.appendChild(button); cols.appendChild(line);
      });
      var open = function (yes) { cols.hidden = !yes; toggle.textContent = yes ? "▾" : "▸"; toggle.setAttribute("aria-expanded", yes ? "true" : "false"); };
      toggle.addEventListener("click", function () { open(cols.hidden); });
      name.addEventListener("click", function () { putTable(t); });
      head.appendChild(toggle); head.appendChild(name); if (kind) { head.appendChild(kind); }
      item.appendChild(head); item.appendChild(cols); list.appendChild(item);
      if (needle && !tableHit) { open(true); }
    });
    if (!list.firstChild) { list.appendChild(el("li", {"class": "muted"}, tables.length ? "Nothing matches." : "No tables in this database.")); }
  }
  filter.addEventListener("input", renderList);

  // ---- the query builder ------------------------------------------------------------------------------------------------------------------
  var OPERATORS = ["=", "<>", "<", "<=", ">", ">=", "LIKE", "ILIKE", "IN", "IS NULL", "IS NOT NULL"];
  var bTable = el("select", {"aria-label": "Table"}), bColumns = el("div", {"class": "sqlh-checks"}), bFilters = el("div"), bJoin = el("select", {"aria-label": "Combine conditions with"});
  var bOrder = el("select", {"aria-label": "Sort by"}), bDir = el("select", {"aria-label": "Direction"}), bLimit = el("input", {type: "text", value: "100", inputmode: "numeric", "aria-label": "Row limit", "class": "sqlh-limit"});
  var bPreview = el("pre", {"class": "sqlh-preview"}), bUse = el("button", {type: "button", "class": "btn primary"}, "Put it in the SQL box (replaces it)");
  ["AND", "OR"].forEach(function (w) { bJoin.appendChild(el("option", {value: w}, w)); });
  ["ASC", "DESC"].forEach(function (w) { bDir.appendChild(el("option", {value: w}, w)); });
  function row(label, node) { var r = el("div", {"class": "sqlh-row"}); r.appendChild(el("label", {}, label)); r.appendChild(node); return r; }
  builder.appendChild(row("Table", bTable));
  builder.appendChild(el("p", {"class": "sqlh-sub"}, "Columns (none ticked = all of them)"));
  builder.appendChild(bColumns);
  builder.appendChild(el("p", {"class": "sqlh-sub"}, "Only rows where"));
  builder.appendChild(bFilters);
  var addFilter = el("button", {type: "button", "class": "btn"}, "+ Condition");
  builder.appendChild(el("div", {"class": "sqlh-actions"}, ""));
  builder.lastChild.appendChild(addFilter); builder.lastChild.appendChild(document.createTextNode(" combined with ")); builder.lastChild.appendChild(bJoin);
  var orderLine = row("Sort by", bOrder); orderLine.appendChild(bDir); builder.appendChild(orderLine);
  builder.appendChild(row("Limit", bLimit));
  builder.appendChild(bPreview);
  builder.appendChild(bUse);

  function current() { return find(bTable.value); }
  function fillTables() {
    var kept = bTable.value;
    bTable.textContent = "";
    tables.forEach(function (t) { bTable.appendChild(el("option", {value: t.schema + "." + t.name}, (t.schema === "public" ? "" : t.schema + ".") + t.name)); });
    if (kept && find(kept)) { bTable.value = kept; }
    fillColumns();
  }
  function fillColumns() {
    var t = current();
    bColumns.textContent = ""; bFilters.textContent = "";
    bOrder.textContent = ""; bOrder.appendChild(el("option", {value: ""}, "(no sorting)"));
    if (!t) { build(); return; }
    t.columns.forEach(function (c) {
      var box1 = el("input", {type: "checkbox", value: c.name}), label = el("label");
      label.appendChild(box1); label.appendChild(document.createTextNode(" " + c.name));
      label.appendChild(el("span", {"class": "muted"}, " " + c.type)); bColumns.appendChild(label);
      bOrder.appendChild(el("option", {value: c.name}, c.name));
    });
    addCondition();                                  // one empty condition to start with; it counts only once it has a value
  }
  function addCondition() {
    var t = current();
    if (!t) { return; }
    var line = el("div", {"class": "sqlh-cond", "data-cond": ""}), col = el("select", {"aria-label": "Column"}), op = el("select", {"aria-label": "Condition"});
    var value = el("input", {type: "text", placeholder: "value", autocomplete: "off", "aria-label": "Value"}), remove = el("button", {type: "button", "class": "btn", title: "Remove this condition"}, "✕");
    t.columns.forEach(function (c) { col.appendChild(el("option", {value: c.name}, c.name)); });
    OPERATORS.forEach(function (o) { op.appendChild(el("option", {value: o}, o)); });
    var sync = function () { value.hidden = /^IS /.test(op.value); value.placeholder = op.value === "IN" ? "a, b, c" : "value"; build(); };
    op.addEventListener("change", sync); col.addEventListener("change", build); value.addEventListener("input", build);
    remove.addEventListener("click", function () { line.remove(); build(); });
    [col, op, value, remove].forEach(function (n) { line.appendChild(n); });
    bFilters.appendChild(line); build();
  }
  function valueSql(type, text) {
    var t = text.trim();
    if (NUMERIC.test(type) && /^-?\d+(\.\d+)?$/.test(t)) { return t; }
    if (/^boolean/i.test(type) && /^(true|false)$/i.test(t)) { return t.toLowerCase(); }
    return literal(text);
  }
  function build() {
    var t = current();
    if (!t) { bPreview.textContent = "-- no table to build from"; return ""; }
    var picked = Array.prototype.filter.call(bColumns.querySelectorAll("input"), function (b) { return b.checked; }).map(function (b) { return ident(b.value); });
    var sql = "SELECT " + (picked.length ? picked.join(", ") : "*") + "\nFROM " + qualified(t);
    var conditions = [];
    Array.prototype.forEach.call(bFilters.querySelectorAll("[data-cond]"), function (line) {
      var sel = line.querySelectorAll("select"), input = line.querySelector("input"), name = sel[0].value, op = sel[1].value;
      var type = (t.columns.filter(function (c) { return c.name === name; })[0] || {}).type || "";
      if (/^IS /.test(op)) { conditions.push(ident(name) + " " + op); }
      else if (input.value.trim() === "") { return; }
      else if (op === "IN") { conditions.push(ident(name) + " IN (" + input.value.split(",").map(function (p) { return valueSql(type, p.trim()); }).join(", ") + ")"); }
      else { conditions.push(ident(name) + " " + op + " " + valueSql(type, input.value)); }
    });
    if (conditions.length) { sql += "\nWHERE " + conditions.join("\n  " + bJoin.value + " "); }
    if (bOrder.value) { sql += "\nORDER BY " + ident(bOrder.value) + " " + bDir.value; }
    var limit = bLimit.value.trim();
    if (/^\d+$/.test(limit) && +limit > 0) { sql += "\nLIMIT " + limit; }
    sql += ";";
    bPreview.textContent = sql;
    return sql;
  }
  bTable.addEventListener("change", fillColumns);
  bColumns.addEventListener("change", build);
  [bJoin, bOrder, bDir].forEach(function (n) { n.addEventListener("change", build); });
  bLimit.addEventListener("input", build);
  addFilter.addEventListener("click", addCondition);
  bUse.addEventListener("click", function () { putStatement(build()); });

  // ---- hints while typing -----------------------------------------------------------------------------------------------------------------
  var suggestions = [];
  // The tables a statement names, by alias: FROM orders o JOIN customers AS c ... gives {o: orders, c: customers}; a table also answers to its own name.
  var NOT_ALIASES = {where: 1, join: 1, on: 1, left: 1, right: 1, inner: 1, outer: 1, full: 1, cross: 1, natural: 1, group: 1, order: 1, limit: 1, offset: 1, union: 1, using: 1, set: 1,
                     values: 1, returning: 1, having: 1, window: 1, fetch: 1, for: 1, lateral: 1, select: 1, from: 1};
  function lookupTable(ref) {
    var parts = ref.replace(/"/g, "").split(/\s*\.\s*/).map(function (p) { return p.toLowerCase(); }), name = parts[parts.length - 1], schema = parts.length > 1 ? parts[0] : "";
    return tables.filter(function (t) { return t.name.toLowerCase() === name && (!schema || t.schema.toLowerCase() === schema); })[0] || null;
  }
  function aliasMap(text) {
    var map = {}, re = /\b(?:from|join|update|into)\s+((?:"[^"]+"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:"[^"]+"|[A-Za-z_][\w$]*))?)(?:\s+(?:as\s+)?([A-Za-z_][\w$]*))?/gi, m;
    while ((m = re.exec(text)) !== null) {
      var table = lookupTable(m[1]);
      if (!table) { continue; }
      map[table.name.toLowerCase()] = table;
      if (m[2] && !NOT_ALIASES[m[2].toLowerCase()]) { map[m[2].toLowerCase()] = table; }
    }
    return map;
  }
  function hint() {
    if (!strip) { return; }
    var caret = box.selectionStart, before = box.value.slice(0, caret);
    var m = /(?:("[^"]+"|[A-Za-z_][\w$]*)\.)?([A-Za-z_][\w$]*)?$/.exec(before), qualifier = m && m[1] ? m[1].replace(/"/g, "").toLowerCase() : "", word = (m && m[2]) || "";
    var out = [];
    if (qualifier && aliasMap(box.value)[qualifier]) {                                    // an alias (or a table named in the statement): its columns
      aliasMap(box.value)[qualifier].columns.forEach(function (c) { out.push({text: ident(c.name), note: c.type}); });
    } else if (qualifier) {
      tables.forEach(function (t) {
        if (t.name.toLowerCase() === qualifier) { t.columns.forEach(function (c) { out.push({text: ident(c.name), note: c.type}); }); }
        else if (t.schema.toLowerCase() === qualifier) { out.push({text: ident(t.name), note: t.kind || "table"}); }
      });
    } else if (word.length >= 2) {
      var previous = /([A-Za-z_]+)\s+[A-Za-z_][\w$]*$/.exec(before), onlyTables = previous && /^(from|join|into|update|table)$/i.test(previous[1]);
      var seen = {};
      var add = function (text, note) { if (!seen[text]) { seen[text] = true; out.push({text: text, note: note}); } };
      tables.forEach(function (t) { add(qualified(t), t.kind || "table"); });
      if (!onlyTables) {
        tables.forEach(function (t) { t.columns.forEach(function (c) { add(ident(c.name), "column of " + t.name); }); });
        KEYWORDS.forEach(function (k) { add(k, "keyword"); });
      }
    }
    var lower = word.toLowerCase();
    suggestions = out.filter(function (s) { return s.text.replace(/"/g, "").toLowerCase().indexOf(lower) === 0 && s.text.toLowerCase() !== lower; }).slice(0, 10);
    strip.textContent = "";
    strip.hidden = !suggestions.length;
    if (!suggestions.length) { return; }
    strip.appendChild(el("span", {"class": "muted"}, "Hints (Ctrl+Space takes the first):"));
    suggestions.forEach(function (s) {
      var chip = el("button", {type: "button", "class": "sqlh-chip", title: s.note}, s.text);
      chip.addEventListener("mousedown", function (event) { event.preventDefault(); });          // keep the cursor in the box
      chip.addEventListener("click", function () { accept(s); });
      strip.appendChild(chip);
    });
  }
  function accept(s) {
    var caret = box.selectionStart, m = /([A-Za-z_][\w$]*)?$/.exec(box.value.slice(0, caret)), word = m && m[1] ? m[1] : "";
    box.setRangeText(s.text, caret - word.length, caret, "end");
    box.focus();
    changed();                                       // colours and hints follow the new text
  }
  box.addEventListener("input", hint);
  box.addEventListener("click", hint);
  box.addEventListener("keyup", function (event) { if (/^(Arrow|Home|End|Backspace)/.test(event.key)) { hint(); } });
  box.addEventListener("keydown", function (event) {
    if (event.ctrlKey && event.code === "Space" && suggestions.length) { event.preventDefault(); accept(suggestions[0]); }
    if (event.key === "Escape" && strip && !strip.hidden) { strip.hidden = true; }
  });

  // ---- load the database's tables ----------------------------------------------------------------------------------------------------------
  panel.textContent = "";
  panel.appendChild(tabs); panel.appendChild(browse); panel.appendChild(builder);
  fetch(panel.getAttribute("data-url"), {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
    .then(function (r) { return r.ok ? r.json() : {tables: []}; })
    .then(function (data) { tables = data.tables || []; renderList(); fillTables(); })
    .catch(function () { list.appendChild(el("li", {"class": "muted"}, "Could not read the tables just now.")); });
})();
