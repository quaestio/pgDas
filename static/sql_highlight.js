// SQL runner: colours the reserved words, strings, numbers and comments of the SQL box. The box itself stays an ordinary textarea (typing, selecting,
// undo and the cursor are the browser's own); its text is made transparent and a coloured copy of the same text is drawn behind it.
// The text is put in the page as text nodes, never as HTML.
(function () {
  "use strict";
  var RESERVED = {};
  ("all analyse analyze and any array as asc asymmetric both case cast check collate column constraint create current_catalog current_date current_role current_time current_timestamp " +
   "current_user default deferrable desc distinct do else end except false fetch for foreign from grant group having in initially intersect into lateral leading limit localtime " +
   "localtimestamp not null offset on only or order placing primary references returning select session_user some symmetric system_user table then to trailing true union unique user " +
   "using variadic when where window with").split(" ").forEach(function (w) { RESERVED[w] = true; });
  // Words that are not reserved in PostgreSQL but read as keywords in a statement.
  var COMMON = {};
  ("insert update delete truncate alter drop join left right inner outer full cross natural values set by view index sequence schema database role grant revoke begin commit rollback " +
   "explain analyze vacuum like ilike between is exists over partition filter rows range unbounded preceding following ascending descending nulls first last cascade restrict if " +
   "recursive materialized temporary temp unlogged add rename type owner replace function returns language trigger copy").split(" ").forEach(function (w) { COMMON[w] = true; });
  window.pgdasSqlWords = {reserved: RESERVED, common: COMMON};

  var box = document.getElementById("sql");
  if (!box || !window.getComputedStyle) { return; }

  // comment | string | quoted identifier | number | word
  var TOKEN = /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][\w$]*)/g;

  var wrap = document.createElement("div"), back = document.createElement("pre");
  wrap.className = "sql-editor"; back.className = "sql-hl"; back.setAttribute("aria-hidden", "true");
  box.parentNode.insertBefore(wrap, box);
  wrap.appendChild(back); wrap.appendChild(box);
  box.classList.add("sql-over");

  var COPY = ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing", "tabSize", "paddingTop", "paddingLeft", "paddingBottom", "paddingRight",
              "borderTopWidth", "borderLeftWidth", "borderBottomWidth", "borderRightWidth", "textIndent", "wordSpacing"];
  function span(cls, text) { var s = document.createElement("span"); s.className = cls; s.textContent = text; return s; }

  function paint() {
    var text = box.value, last = 0, match, out = document.createDocumentFragment();
    TOKEN.lastIndex = 0;
    while ((match = TOKEN.exec(text)) !== null) {
      if (match.index > last) { out.appendChild(document.createTextNode(text.slice(last, match.index))); }
      var token = match[0], lower = token.toLowerCase();
      if (match[1]) { out.appendChild(span("sqlk-comment", token)); }
      else if (match[2]) { out.appendChild(span("sqlk-string", token)); }
      else if (match[3]) { out.appendChild(span("sqlk-ident", token)); }
      else if (match[4]) { out.appendChild(span("sqlk-number", token)); }
      else if (RESERVED[lower]) { out.appendChild(span("sqlk-reserved", token)); }
      else if (COMMON[lower]) { out.appendChild(span("sqlk-keyword", token)); }
      else { out.appendChild(document.createTextNode(token)); }
      last = TOKEN.lastIndex;
    }
    if (last < text.length) { out.appendChild(document.createTextNode(text.slice(last))); }
    out.appendChild(document.createTextNode("\n"));                  // a last empty line is as tall here as in the box
    back.textContent = "";
    back.appendChild(out);
    back.scrollTop = box.scrollTop; back.scrollLeft = box.scrollLeft;
  }

  function measure() {                                                // the same text metrics as the box, and room for its scroll bar
    var style = window.getComputedStyle(box);
    COPY.forEach(function (name) { back.style[name] = style[name]; });
    var bar = box.offsetWidth - box.clientWidth - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth);
    back.style.paddingRight = (parseFloat(style.paddingRight) + Math.max(0, bar)) + "px";
  }

  box.addEventListener("input", paint);
  box.addEventListener("scroll", function () { back.scrollTop = box.scrollTop; back.scrollLeft = box.scrollLeft; });
  if (window.ResizeObserver) { new ResizeObserver(function () { measure(); paint(); }).observe(box); }
  window.addEventListener("resize", function () { measure(); paint(); });
  measure(); paint();
  // Text put in the box by a script (the history list, the builder, a click on a table) does not fire "input": those scripts dispatch it themselves,
  // and a form reset or the browser restoring the box is caught here.
  window.addEventListener("pageshow", function () { measure(); paint(); });
  window.pgdasSqlPaint = function () { measure(); paint(); };
})();
