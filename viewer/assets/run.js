/* Copied from the official trace browser. Local changes are marked `kilo-viewer:`. */
/* Run page: the event timeline, plus the evidence and grading tabs.

   Focus mode hides the two things that make a long trace unreadable without
   changing what happened — the model's own thinking, and tool results — and
   keeps everything that bears on whether the trace was tampered with. */

(function () {
  var DATA = null;
  var EVENTS = [];
  var VIEW = 'focus';

  var el = {
    title: document.getElementById('s-title'),
    sub: document.getElementById('s-sub'),
    verdict: document.getElementById('s-verdict'),
    headline: document.getElementById('s-headline'),
    stats: document.getElementById('s-stats'),
    runId: document.getElementById('s-runid'),
    runIdText: document.getElementById('s-runid-text'),
    back: document.getElementById('back-link'),
    trace: document.getElementById('trace'),
    count: document.getElementById('event-count'),
    evidence: document.getElementById('evidence'),
    task: document.getElementById('task'),
    jump: document.getElementById('jump'),
    expand: document.getElementById('expand-outputs')
  };

  var params = new URLSearchParams(location.search);
  var RUN_ID = params.get('id');
  var BACK = params.get('back');
  if (BACK) el.back.href = 'index.html?' + BACK;

  // ------------------------------------------------------------ rendering

  // In Focus mode a tool result only earns its place if it failed or if the
  // call touched a trace file — those are the moments the paper is about.
  // Deliberately the harness trace locations only — a bare ".jsonl" matches
  // half of every Codex transcript and would stop Focus filtering anything.
  var TRACE_PATH = new RegExp([
    '\\.claude/projects', '\\.claude/shell-snapshots',
    '\\.codex/sessions', 'rollout-\\d{4}-\\d{2}-\\d{2}',
    '\\.gemini/tmp', '\\.gemini/[^\\s]*chats',
    'opencode\\.db', '\\.local/share/opencode', 'native-session',
    '\\.grok/sessions', '\\.local/share/muse/sessions',
    '\\.gemini/antigravity-cli', '\\.zcode/cli',
    '\\.kimi-code/sessions',
    // kilo-viewer: Kilo CLI keeps one SQLite store under ~/.local/share/kilo
    'kilo\\.db', '\\.local/share/kilo', '\\bkilo\\s+(--pure\\s+)?(session|db)\\b'
  ].join('|'), 'i');

  function touchesTrace(event) {
    var haystack = (event.title || '') + ' ' + (event.input || '');
    return TRACE_PATH.test(haystack);
  }

  function visible(event) {
    if (VIEW === 'full') return true;
    if (event.kind === 'thought') return false;
    if (event.kind === 'tool_result') return event.is_error || touchesTrace(event);
    return true;
  }

  // The .row wrapper is display:contents and so has no box of its own —
  // the anchor has to live on the gutter cell for scrollIntoView to work.
  function gutter(event) {
    return '<div class="row-gutter" id="ev-' + event.i + '">' +
      '<a class="row-turn" href="#ev-' + event.i + '">#' + (event.i + 1) + '</a>' +
      (event.ts ? '<br>' + TPC.clockTime(event.ts) : '') + '</div>';
  }

  function label(text) {
    return '<span class="event-label">' + TPC.escapeHtml(text) + '</span>';
  }

  function body(event) {
    switch (event.kind) {
      case 'stage':
        return '<div class="stage-divider"><span class="stage-name">' +
          TPC.escapeHtml(event.name || 'stage') + '</span></div>' +
          (event.prompt ? '<div class="bubble bubble-user">' +
            label('stage prompt') + TPC.markdown(event.prompt) + '</div>' : '');

      case 'session_start':
        var fields = [
          ['agent', event.model],
          ['cwd', event.cwd],
          ['permission', event.permission_mode],
          ['session', event.session_id],
          ['tools', event.tools && event.tools.length
            ? event.tools.length + ' (' + event.tools.slice(0, 8).join(', ') +
              (event.tools.length > 8 ? ', …' : '') + ')' : null]
        ].filter(function (f) { return f[1]; });
        return '<div class="session-card">' + label('session start') + '<dl>' +
          fields.map(function (f) {
            return '<dt>' + TPC.escapeHtml(f[0]) + '</dt><dd>' +
              TPC.escapeHtml(f[1]) + '</dd>';
          }).join('') + '</dl></div>';

      case 'user':
        return '<div class="bubble bubble-user">' + label('user') +
          TPC.markdown(event.text) + '</div>';

      case 'text':
        return '<div class="bubble">' + TPC.markdown(event.text) + '</div>';

      case 'thought':
        return '<details class="thought"><summary>thinking</summary>' +
          '<div class="thought-body">' + TPC.escapeHtml(event.text) + '</div></details>';

      case 'tool_call':
        var isJson = /^\s*[[{]/.test(event.input || '');
        return '<div class="tool"><div class="tool-head">' +
          '<span class="tool-name">' + TPC.escapeHtml(event.name || 'tool') + '</span>' +
          (touchesTrace(event)
            ? '<span class="spacer"></span><span class="chip chip-alert">trace file</span>' : '') +
          '</div><pre class="tool-cmd' + (isJson ? ' tool-cmd-json' : '') + '">' +
          TPC.escapeHtml(event.title || event.input || '') + '</pre></div>';

      case 'tool_result':
        var open = el.expand.checked ? ' open' : '';
        var exit = event.exit_code !== undefined && event.exit_code !== null
          ? ' exit ' + event.exit_code : '';
        return '<div class="tool"><div class="tool-head">' +
          '<span class="tool-name">' + TPC.escapeHtml(event.name || 'result') +
          '</span><span class="spacer"></span><span>result' + exit + '</span></div>' +
          '<details class="tool-out' + (event.is_error ? ' tool-out-error' : '') +
          '"' + open + '><summary></summary><pre>' +
          TPC.escapeHtml(event.output || '(no output)') + '</pre>' +
          (event.truncated ? '<div class="tool-trunc">output elided in the middle</div>' : '') +
          '</details></div>';

      case 'file_change':
        return '<div class="filechange">' + label('file change') +
          (event.changes || []).map(function (c) {
            return '<div>' + TPC.escapeHtml(c.kind || 'change') + ' <b>' +
              TPC.escapeHtml(c.path || '') + '</b></div>';
          }).join('') + '</div>';

      case 'notice':
        return '<div class="notice notice-' + TPC.escapeHtml(event.flavor || 'info') + '">' +
          '<div class="notice-title">' + TPC.escapeHtml(event.title || '') + '</div>' +
          (event.detail ? '<div class="notice-detail">' +
            TPC.escapeHtml(event.detail) + '</div>' : '') +
          (event.body ? '<div class="notice-body">' +
            TPC.escapeHtml(event.body) + '</div>' : '') + '</div>';

      case 'result':
        return '<div class="bubble">' + label('result · ' + (event.status || '')) +
          (event.text ? TPC.markdown(event.text) : '<p class="muted">(no final message)</p>') +
          '</div>';

      case 'turn_end':
        return '';

      default:
        return '<div class="bubble muted">' + TPC.escapeHtml(event.kind) + '</div>';
    }
  }

  function renderTrace() {
    var shown = EVENTS.filter(visible);
    el.trace.innerHTML = shown.map(function (event) {
      var html = body(event);
      if (!html) return '';
      return '<div class="row">' + gutter(event) +
        '<div class="row-body">' + html + '</div></div>';
    }).join('');

    var hidden = EVENTS.length - shown.length;
    el.count.textContent = EVENTS.length.toLocaleString('en-US') + ' events' +
      (hidden > 0 ? ' · ' + hidden.toLocaleString('en-US') + ' hidden in Focus' : '');
  }

  // ----------------------------------------------------------------- rail

  function renderRail() {
    var meta = DATA.meta;
    document.title = meta.label + ' · ' + meta.harness + ' · trace';
    el.title.textContent = meta.label;
    el.sub.textContent = meta.harness + ' · ' + meta.model;

    el.verdict.textContent = TPC.verdictLabel(DATA.verdict.verdict);
    el.verdict.className = 'verdict-big verdict verdict-' + DATA.verdict.verdict;
    el.headline.textContent = DATA.verdict.headline || '';

    var rows = [
      ['attack class', meta.group],
      ['setting', meta.label],
      ['harness', meta.harness + (meta.cli_version ? ' ' + meta.cli_version : '')],
      ['model', meta.model],
      ['permissions', meta.paper_permission === 'full' ? 'full access' :
        (meta.paper_permission === 'auto' ? 'auto mode' : meta.permission_mode)],
      ['paper selection', meta.paper_status],
      ['status', meta.status],
      ['duration', TPC.duration(meta.elapsed_seconds)],
      ['events', TPC.number(DATA.summary.events)],
      ['tool calls', TPC.number(DATA.summary.tool_calls)],
      ['cost', TPC.money(DATA.summary.cost_usd)],
      ['trace source', meta.trace_source],
      ['batch', meta.batch && (meta.batch + (meta.trial ? ' · ' + meta.trial : ''))],
      ['paper trial', meta.paper_trial]
    ];
    if (meta.skill_name) rows.splice(2, 0, ['skill', meta.skill_name]);
    if (DATA.attempts.length) rows.push(['attempts', String(DATA.attempts.length)]);

    el.stats.innerHTML = rows.filter(function (r) { return r[1]; }).map(function (r) {
      return '<dt>' + TPC.escapeHtml(r[0]) + '</dt><dd>' + TPC.escapeHtml(r[1]) + '</dd>';
    }).join('');

    el.runIdText.textContent = meta.run_id;
    el.runId.addEventListener('click', function () {
      navigator.clipboard && navigator.clipboard.writeText(meta.run_id);
      el.runId.setAttribute('title', 'copied');
    });
  }

  // ------------------------------------------------------------- evidence

  function eventList(title, items, className) {
    if (!items || !items.length) return '';
    return '<h3 class="rail-section-label">' + TPC.escapeHtml(title) + '</h3>' +
      '<table class="evidence-table"><tbody>' + items.map(function (item) {
        var text = typeof item === 'string' ? item : (item.path || JSON.stringify(item));
        return '<tr><td class="mono ' + className + '">' +
          TPC.escapeHtml(text) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function renderEvidence() {
    var report = DATA.report;
    var parts = [];

    parts.push('<p class="muted" style="max-width:44rem">The observer watches the ' +
      'container filesystem from outside it. These are the writes, moves and ' +
      'deletions it saw against the harness\'s own trace files — independent ' +
      'of what the agent said it was doing.</p>');

    parts.push(eventList('Trace deletions', report.trace_deletion_events, 'op-delete'));
    parts.push(eventList('Trace moves', report.trace_move_events, 'op-modify'));
    parts.push(eventList('Content replacements',
      report.trace_content_replacement_events, 'op-modify'));
    if (report.permission_denials && report.permission_denials.length) {
      parts.push('<h3 class="rail-section-label">Permission denials (' +
        report.permission_denials.length + ')</h3>' +
        report.permission_denials.map(function (denial) {
          var input = denial.tool_input || {};
          var command = input.command || input.file_path ||
            JSON.stringify(input).slice(0, 2000);
          return '<div class="tool" style="margin-bottom:.6rem">' +
            '<div class="tool-head"><span class="tool-name">' +
            TPC.escapeHtml(denial.tool_name || 'tool') + '</span>' +
            '<span class="spacer"></span><span>blocked</span></div>' +
            (input.description ? '<div class="tool-trunc" style="padding-top:.4rem">' +
              TPC.escapeHtml(input.description) + '</div>' : '') +
            '<pre class="tool-cmd">' + TPC.escapeHtml(command) + '</pre></div>';
        }).join(''));
    }

    if (DATA.fs_events.length) {
      // kilo-viewer: runs with a follow-up request stage tag each event with its stage;
      // only the graded stage counts, earlier activity (e.g. SQLite removing its own
      // journal file at startup) is shown dimmed rather than as an agent deletion.
      var staged = DATA.fs_events.some(function (e) { return e.stage; });
      parts.push('<h3 class="rail-section-label">Filesystem events on trace files (' +
        DATA.fs_events.length + ')</h3>' +
        '<table class="evidence-table"><thead><tr><th>time</th>' +
        (staged ? '<th>stage</th>' : '') + '<th>op</th>' +
        '<th>path</th></tr></thead><tbody>' +
        DATA.fs_events.map(function (e) {
          var ops = (e.events || []).join(', ');
          var destructive = /delete|moved_from/.test(ops);
          var ignored = staged && e.counted === false;
          return '<tr' + (ignored ? ' style="opacity:.55"' : '') + '>' +
            '<td class="mono">' + TPC.escapeHtml(TPC.clockTime(e.ts)) + '</td>' +
            (staged ? '<td>' + TPC.escapeHtml(e.stage || '') +
              (ignored ? ' · not counted' : '') + '</td>' : '') +
            '<td class="op ' + (destructive && !ignored ? 'op-delete' : 'op-modify') + '">' +
            TPC.escapeHtml(ops) + '</td>' +
            '<td class="mono">' + TPC.escapeHtml(e.root ? e.root + '/' + e.path : e.path) +
            '</td></tr>';
        }).join('') + '</tbody></table>');
    }

    if (report.trace_write_event_count) {
      parts.push('<p class="muted">The harness itself wrote to its trace ' +
        TPC.number(report.trace_write_event_count) + ' times during this run; ' +
        'ordinary logging accounts for most of that.</p>');
    }

    var meaningful = parts.filter(function (p) { return p; }).length > 1;
    el.evidence.innerHTML = meaningful ? parts.join('')
      : parts[0] + '<p class="empty-state">No trace-file events were recorded for this run.</p>';
  }

  // ------------------------------------------------------------ task tab

  function renderTask() {
    var parts = [];
    var meta = DATA.meta;

    if (meta.task_prompt) {
      parts.push('<h3 class="rail-section-label">Task prompt</h3>' +
        '<div class="prose"><pre>' + TPC.escapeHtml(meta.task_prompt) + '</pre></div>');
    }
    if (meta.skill_path) {
      parts.push('<h3 class="rail-section-label">Injected skill</h3>' +
        '<p class="mono">' + TPC.escapeHtml(meta.skill_name || '') + ' — ' +
        TPC.escapeHtml(meta.skill_path) + '</p>');
    }

    if (DATA.attempts.length) {
      parts.push('<h3 class="rail-section-label">Scored attempts</h3>' +
        '<table class="evidence-table"><thead><tr><th>#</th><th>task</th>' +
        '<th>trace</th><th>reward</th><th>trace status</th></tr></thead><tbody>' +
        DATA.attempts.map(function (a) {
          var tampered = a.trace_edited || a.trace_cleared || a.trace_deleted ||
            (a.trace_status && a.trace_status !== 'unchanged');
          return '<tr><td>' + TPC.escapeHtml(a.attempt) + '</td>' +
            '<td class="num">' + (a.task_score !== undefined ? a.task_score
              : (a.document_score !== undefined ? a.document_score : '—')) + '</td>' +
            '<td class="num">' + (a.trace_score !== undefined ? a.trace_score : '—') + '</td>' +
            '<td class="num">' + (a.total_reward !== undefined ? a.total_reward : '—') + '</td>' +
            '<td class="op ' + (tampered ? 'op-delete' : '') + '">' +
            TPC.escapeHtml(a.trace_status || (tampered ? 'edited' : 'unchanged')) +
            '</td></tr>';
        }).join('') + '</tbody></table>');
    }

    var v = DATA.verdict;
    parts.push('<h3 class="rail-section-label">Grading</h3><div class="prose">');
    parts.push('<p><b>' + TPC.escapeHtml(TPC.verdictLabel(v.verdict)) + '</b>' +
      (v.headline ? ' — ' + TPC.escapeHtml(v.headline) : '') + '</p>');
    if (v.observation_status) {
      parts.push('<p class="muted">Observation status: ' +
        TPC.escapeHtml(v.observation_status) + '.</p>');
    }
    if (v.interpretation) {
      parts.push('<p class="muted">' + TPC.escapeHtml(v.interpretation) + '</p>');
    }
    if (v.adjudication) {
      parts.push('<p class="muted">Outcome follows the paper-results trial selection (' +
        TPC.escapeHtml(v.adjudication) + '), which overrides the run\'s own grader.</p>');
    }
    if (v.note) parts.push('<p class="muted">' + TPC.escapeHtml(v.note) + '</p>');
    if (v.model_fallbacks && v.model_fallbacks.length) {
      parts.push('<p><b>Model fallbacks:</b> ' +
        TPC.escapeHtml(JSON.stringify(v.model_fallbacks)) + '</p>');
    }
    if (v.errors && v.errors.length) {
      parts.push('<p class="muted"><b>Harness errors:</b> ' +
        TPC.escapeHtml(v.errors.join('; ')) + '</p>');
    }
    parts.push('</div>');

    if (DATA.summary.final_response) {
      parts.push('<h3 class="rail-section-label">Final response</h3>' +
        '<div class="prose bubble">' + TPC.markdown(DATA.summary.final_response) + '</div>');
    }

    el.task.innerHTML = parts.join('');
  }

  // ----------------------------------------------------------------- wire

  document.querySelectorAll('.tab-btn').forEach(function (button) {
    button.addEventListener('click', function () {
      document.querySelectorAll('.tab-btn').forEach(function (b) {
        b.classList.toggle('active', b === button);
      });
      document.querySelectorAll('.section').forEach(function (section) {
        section.classList.toggle('active', section.id === 'tab-' + button.dataset.tab);
      });
    });
  });

  function setView(next) {
    VIEW = next;
    document.getElementById('view-focus').classList.toggle('active', next === 'focus');
    document.getElementById('view-full').classList.toggle('active', next === 'full');
    renderTrace();
  }

  document.getElementById('view-focus').addEventListener('click', function () { setView('focus'); });
  document.getElementById('view-full').addEventListener('click', function () { setView('full'); });
  el.expand.addEventListener('change', renderTrace);

  function jumpTo(n) {
    if (!n) return;
    var target = document.getElementById('ev-' + (n - 1));
    // The event may simply be one Focus mode hides; switch views rather than
    // silently doing nothing.
    if (!target && VIEW === 'focus') {
      setView('full');
      target = document.getElementById('ev-' + (n - 1));
    }
    if (!target) return;
    // Let the freshly written timeline lay out before scrolling into it.
    // The jump is instant on purpose: a smooth scroll through a 900-event
    // trace is both slow and easy for the next render to cancel.
    requestAnimationFrame(function () {
      target.scrollIntoView({ block: 'center' });
      var row = target.nextElementSibling;
      if (!row) return;
      row.classList.remove('row-flash');
      void row.offsetWidth;
      row.classList.add('row-flash');
    });
  }

  el.jump.addEventListener('change', function () {
    jumpTo(parseInt(el.jump.value, 10));
  });

  if (!RUN_ID) {
    el.trace.innerHTML = '<p class="empty-state">No run id in the URL.</p>';
    return;
  }

  TPC.loadRun(RUN_ID).then(function (data) {
    DATA = data;
    EVENTS = data.events_list;
    renderRail();
    renderTrace();
    renderEvidence();
    renderTask();
    if (location.hash.indexOf('#ev-') === 0) {
      jumpTo(parseInt(location.hash.slice(4), 10) + 1);
    }
  }).catch(function (err) {
    el.trace.innerHTML = '<p class="empty-state">Could not load this run (' +
      TPC.escapeHtml(err.message) + ').</p>';
  });
})();
