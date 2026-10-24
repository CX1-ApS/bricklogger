// Every action that waits says so: while a button or a form waits for its
// answer, it and the buttons beside it are disabled, and its result area shows
// "working…" with the seconds counting up, until the answer replaces it.
// A button marked data-job-guard="<group>" also stays disabled while a job of
// that group (data-job-group) is queued or running on the page.
// See docs/features/web.md, "Screens".
(function () {
  'use strict';
  var body = document.body;
  var prompt = body.dataset.prompt || '>';
  var working = body.dataset.working || 'working…';
  var waits = new WeakMap();

  function swapOf(elt) {
    var holder = elt.closest('[hx-swap]');
    return holder ? holder.getAttribute('hx-swap') : 'innerHTML';
  }

  function begin(event) {
    var elt = event.detail.elt;
    // polling and other requests without a button behind them are not actions
    if (!elt || !elt.matches('button, form')) return;
    var row = elt.closest('.button-row') || (elt.matches('form') ? elt : elt.parentElement);
    var buttons = Array.prototype.slice.call(row.querySelectorAll('button'));
    if (elt.matches('button') && buttons.indexOf(elt) < 0) buttons.push(elt);
    buttons = buttons.filter(function (button) { return !button.disabled; });
    buttons.forEach(function (button) {
      button.disabled = true;
      button.dataset.waiting = 'true';
    });
    var line = document.createElement('pre');
    line.className = 'terminal';
    var started = Date.now();
    function tick() {
      var seconds = Math.floor((Date.now() - started) / 1000);
      line.textContent = prompt + ' ' + working + ' ' + seconds + ' s';
    }
    tick();
    var timer = setInterval(tick, 1000);
    var target = event.detail.target;
    if (target && swapOf(elt).indexOf('innerHTML') === 0) {
      target.replaceChildren(line);
    } else {
      row.insertAdjacentElement('afterend', line);
    }
    waits.set(event.detail.xhr, { buttons: buttons, line: line, timer: timer });
  }

  function end(event) {
    var wait = waits.get(event.detail.xhr);
    if (!wait) return;
    waits.delete(event.detail.xhr);
    clearInterval(wait.timer);
    if (wait.line.isConnected) wait.line.remove();
    wait.buttons.forEach(function (button) {
      delete button.dataset.waiting;
      button.disabled = false;
    });
    guard();
  }

  function guard() {
    var busy = {};
    document.querySelectorAll('[data-job-group]').forEach(function (job) {
      var state = job.dataset.jobState;
      if (state === 'queued' || state === 'running') busy[job.dataset.jobGroup] = true;
    });
    document.querySelectorAll('[data-job-guard]').forEach(function (button) {
      if (button.dataset.waiting) return;
      button.disabled = !!busy[button.dataset.jobGuard];
    });
  }

  body.addEventListener('htmx:beforeRequest', begin);
  body.addEventListener('htmx:afterRequest', end);
  body.addEventListener('htmx:afterSettle', guard);
  guard();
})();
