// Changing data from the SQL runner. Only included for staff users. The popup itself is static/review.js.
//   INSERT                   → runs at once.
//   UPDATE                   → preview (nothing changes) → popup with the number of rows and a warning → button.
//   DELETE / TRUNCATE / DROP → preview → popup with a warning → type ZAP / TRUNCATE / DROP → go.
//   CREATE / ALTER           → preview → popup listing exactly what would be created or changed → button
//                              (an ALTER that is risky asks for the word ALTER first).
// Anything else (SELECT, ...) is submitted normally and runs read-only. Values from the database are inserted as text,
// never as HTML.
(function () {
  var form = document.getElementById("sqlForm");
  if (!form || form.dataset.canWrite !== "1" || !window.pgdasReview) return;
  var textarea = document.getElementById("sql");
  var runButton = document.getElementById("runButton");
  var resultBox = document.getElementById("changeResult");
  var db = form.querySelector("input[name=db]").value;

  function tone(type) { if (window.pgdasSound) window.pgdasSound.play(type); }

  function kindOf(text) {
    var bare = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/--[^\n]*/g, "").trim();
    var match = /^(insert|update|delete|truncate|drop|create|alter|grant|revoke)\b/i.exec(bare);
    return match ? match[1].toLowerCase() : null;
  }

  function cell(tag, value) {
    var element = document.createElement(tag);
    if (value === null) { element.textContent = "NULL"; element.className = "null"; }
    else { element.textContent = typeof value === "object" ? JSON.stringify(value) : String(value); }
    return element;
  }

  function showMessage(kind, text, extra) {
    resultBox.innerHTML = "";
    var list = document.createElement("ul");
    list.className = "messages";
    var item = document.createElement("li");
    item.className = kind;
    item.textContent = text + " ";
    if (extra && extra.url) {
      var link = document.createElement("a");
      link.href = extra.url;
      link.textContent = extra.text;
      item.appendChild(link);
    }
    list.appendChild(item);
    resultBox.appendChild(list);
    if (extra && extra.table) {                       // INSERT ... RETURNING: show what came back
      var wrap = document.createElement("section");
      wrap.className = "panel pad scroll";
      wrap.style.marginTop = "12px";
      var table = document.createElement("table"), head = document.createElement("tr");
      extra.table.columns.forEach(function (name) { head.appendChild(cell("th", name)); });
      var thead = document.createElement("thead"), tbody = document.createElement("tbody");
      thead.appendChild(head);
      extra.table.rows.forEach(function (row) {
        var tr = document.createElement("tr");
        row.forEach(function (value) { var td = cell("td", value); td.className = (td.className + " cell").trim(); tr.appendChild(td); });
        tbody.appendChild(tr);
      });
      table.appendChild(thead); table.appendChild(tbody); wrap.appendChild(table);
      resultBox.appendChild(wrap);
    }
  }

  function failed(message) { showMessage("error", message); tone("error"); }

  form.addEventListener("submit", function (event) {
    if (event.submitter && event.submitter.id === "exportButton") return;
    var kind = kindOf(textarea.value);
    if (!kind) return;                                    // anything else runs read-only, as before
    event.preventDefault();
    resultBox.innerHTML = "";
    runButton.disabled = true;
    runButton.textContent = kind === "insert" ? "Inserting…" : "Checking…";
    var finish = function () { runButton.disabled = false; runButton.textContent = "Run"; };
    if (kind === "insert") {
      window.pgdasReview.post(form.dataset.executeUrl, {sql: textarea.value, db: db}).then(function (response) {
        if (!response.ok) { failed(response.body.error || "That statement could not be run."); return; }
        showMessage("success", response.body.message, {url: form.dataset.auditUrl, text: "Open the audit log", table: response.body.returned});
        tone("success");
      }).catch(function () { failed("Could not reach PGDAS to run this statement."); }).then(finish);
      return;
    }
    window.pgdasReview.start({
      server: form.dataset.server, db: db, sql: textarea.value, onError: failed,
      onSuccess: function (result) { showMessage("success", result.message, {url: form.dataset.auditUrl, text: "Open the audit log"}); },
    }).then(finish);
  });
})();
