(function () {
  var dialog = document.getElementById('employeeDialog');
  if (!dialog) return;
  var body = document.getElementById('employeeDialogBody');
  var source, endpoint, busy = false, loadVersion = 0;
  function showError(message) { body.replaceChildren(); var p = document.createElement('p'); p.setAttribute('role', 'alert'); p.textContent = message; body.appendChild(p); }
  document.querySelectorAll('[data-employee-modal]').forEach(function (link) {
    link.addEventListener('click', async function (event) {
      event.preventDefault(); source = link; endpoint = link.href;
      var version = ++loadVersion;
      dialog.showModal(); body.textContent = 'Loading…';
      try {
        var response = await fetch(endpoint, {headers: {'X-Employee-Modal': '1'}, credentials: 'same-origin'});
        if (!response.ok || response.redirected) throw new Error();
        var html = await response.text();
        if (version !== loadVersion) return;
        body.innerHTML = html;
        var first = body.querySelector('input:not([type=hidden]), button'); if (first) first.focus();
      } catch (error) { if (version === loadVersion) showError('Could not load this employee form. Refresh the page and try again.'); }
    });
  });
  dialog.addEventListener('click', function (event) { if (event.target.closest('[data-employee-close]') && !busy) dialog.close(); });
  dialog.addEventListener('cancel', function (event) { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', function () { loadVersion++; if (source) source.focus(); });
  dialog.addEventListener('submit', async function (event) {
    event.preventDefault(); if (busy) return;
    busy = true; var form = event.target; var data = new FormData(form);
    dialog.querySelectorAll('button').forEach(function (button) { button.disabled = true; });
    try {
      var response = await fetch(endpoint, {method: 'POST', body: data, headers: {'X-Employee-Modal': '1'}, credentials: 'same-origin'});
      if (!response.ok || response.redirected) throw new Error();
      if ((response.headers.get('content-type') || '').includes('application/json')) {
        var result = await response.json(); if (!result.ok) throw new Error();
        window.location.reload(); return;
      }
      body.innerHTML = await response.text();
      var error = body.querySelector('.errorlist, [role=alert]');
      if (error) { error.setAttribute('tabindex', '-1'); error.focus(); }
    } catch (error) { showError('The result could not be verified. Close this popup and refresh before retrying.'); }
    finally { busy = false; dialog.querySelectorAll('button').forEach(function (button) { button.disabled = false; }); }
  });
})();
