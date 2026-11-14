// Reads a PostgreSQL connection string and returns the fields the Add server popup has. It runs in the browser; nothing is sent anywhere.
// Accepts  postgresql://user:password@host:5432/db?sslmode=require  (also postgres:// and jdbc:postgresql://)  and the keyword form
// host=... port=... user=... password='...' sslmode=...   Returns {values: {host, port, username, password, sslmode}, ignored: [...]} or {error}.
(function (root) {
  "use strict";
  var SSL = ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"];

  function decode(text) {
    try { return decodeURIComponent(text); } catch (e) { return text; }
  }

  function settle(pairs, ignored) {
    var values = {}, wanted = {host: "host", hostaddr: "host", port: "port", user: "username", password: "password", sslmode: "sslmode"};
    Object.keys(pairs).forEach(function (key) {
      var value = pairs[key], name = wanted[key.toLowerCase()];
      if (!name) { if (key.toLowerCase() === "dbname") { ignored.push("the database name (PGDAS picks a database the login can open)"); } else { ignored.push(key); } return; }
      if (name === "host" && key.toLowerCase() === "hostaddr" && values.host) { return; }
      if (name === "host" && value.indexOf(",") >= 0) { ignored.push("the other hosts in the list"); value = value.split(",")[0]; }
      if (name === "port" && value.indexOf(",") >= 0) { value = value.split(",")[0]; }
      if (name === "sslmode" && SSL.indexOf(value) < 0) { ignored.push("sslmode=" + value + " (not a mode PGDAS knows)"); return; }
      if (name === "port" && !/^\d{1,5}$/.test(value)) { ignored.push("port " + value + " (not a number)"); return; }
      values[name] = value;
    });
    if (!values.host && !values.username) { return {error: "No host or user found in that connection string."}; }
    return {values: values, ignored: ignored};
  }

  function parseUrl(text) {
    var rest = text.replace(/^jdbc:/i, "").replace(/^postgres(?:ql)?:\/\//i, "");
    // The user info ends at the last "@" before any "?": so a raw "#" or "@" inside a password does not cut it short.
    var q = rest.indexOf("?"), head = q >= 0 ? rest.slice(0, q) : rest;
    var at = head.lastIndexOf("@"), query = "", main;
    var afterUser = rest.slice(at + 1);
    var hash = afterUser.search(/[?#]/);
    if (at < 0 && q >= 0) { main = head; query = rest.slice(q + 1); }
    else {
      main = hash < 0 ? afterUser : afterUser.slice(0, hash);
      var tail = hash < 0 ? "" : afterUser.slice(hash);
      if (tail.charAt(0) === "?") { query = tail.slice(1); var h2 = query.indexOf("#"); if (h2 >= 0) { query = query.slice(0, h2); } }
      main = rest.slice(0, at + 1) + main;
    }
    var hashInMain = at < 0 ? main.indexOf("#") : -1;
    if (hashInMain >= 0) { main = main.slice(0, hashInMain); }
    at = main.lastIndexOf("@");
    var slash = main.indexOf("/", at + 1);
    var authority = slash < 0 ? main : main.slice(0, slash), path = slash < 0 ? "" : main.slice(slash + 1);    var pairs = {}, ignored = [];
    var userinfo = at >= 0 ? authority.slice(0, authority.lastIndexOf("@")) : "";
    var hostport = at >= 0 ? authority.slice(authority.lastIndexOf("@") + 1) : authority;
    if (userinfo) {
      var colon = userinfo.indexOf(":");
      pairs.user = decode(colon < 0 ? userinfo : userinfo.slice(0, colon));
      if (colon >= 0) { pairs.password = decode(userinfo.slice(colon + 1)); }
    }
    if (hostport) {
      var port = "", host = hostport;
      var v6 = /^\[([^\]]+)\](?::(\d*))?$/.exec(hostport);
      if (v6) { host = v6[1]; port = v6[2] || ""; }
      else if (hostport.indexOf(",") >= 0) { ignored.push("the other hosts in the list"); var first = hostport.split(",")[0]; var c1 = first.lastIndexOf(":"); host = c1 < 0 ? first : first.slice(0, c1); port = c1 < 0 ? "" : first.slice(c1 + 1); }
      else { var c = hostport.lastIndexOf(":"); if (c >= 0) { host = hostport.slice(0, c); port = hostport.slice(c + 1); } }
      if (host) { pairs.host = decode(host); }
      if (port) { pairs.port = port; }
    }
    if (path) { ignored.push("the database name " + decode(path) + " (PGDAS picks a database the login can open)"); }
    query.split("&").forEach(function (part) {
      if (!part) { return; }
      var eq = part.indexOf("="), key = decode(eq < 0 ? part : part.slice(0, eq)), value = eq < 0 ? "" : decode(part.slice(eq + 1).replace(/\+/g, " "));
      pairs[key] = value;
    });
    return settle(pairs, ignored);
  }

  function parseKeywords(text) {
    var pairs = {}, found = 0, re = /([A-Za-z_]+)\s*=\s*('(?:[^'\\]|\\.)*'|\S*)/g, match;
    while ((match = re.exec(text)) !== null) {
      var value = match[2];
      if (value.charAt(0) === "'") { value = value.slice(1, -1); }
      pairs[match[1]] = value.replace(/\\(.)/g, "$1");
      found += 1;
    }
    return found ? settle(pairs, []) : {error: "That does not look like a PostgreSQL connection string."};
  }

  function parse(text) {
    text = String(text || "").trim();
    if (!text) { return {error: "Paste a connection string first."}; }
    if (/^(jdbc:)?postgres(ql)?:\/\//i.test(text)) { return parseUrl(text); }
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) { return {error: "Only PostgreSQL connection strings are supported (postgresql://…)."}; }
    return parseKeywords(text);
  }

  root.pgdasParseConnection = parse;
  if (typeof module !== "undefined" && module.exports) { module.exports = parse; }
})(typeof window !== "undefined" ? window : (typeof global !== "undefined" ? global : {}));