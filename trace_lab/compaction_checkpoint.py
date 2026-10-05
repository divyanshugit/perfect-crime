"""Controller-only pause for an independent, read-only checkpoint capture."""

from pathlib import Path
import json
import sys
import time

GATE = Path('/tmp/trace-lab-checkpoint-captured')
CONTEXT = Path('/workspace/.runtime/session.json')


def context_metadata(capture, record_ids):
    """Locators only: no summary text, payload, or instructions."""
    fields = {'kimi': 'summary in a type=context.apply_compaction agentId=main record', 'zcode': 'part.data.text linked to a message with semantics.kind=compact_summary', 'muse': 'payload.event.replacement.text in context_compaction_installed',
              'grok': 'content[].text in a synthetic_reason=compaction_meta user record',
              'codex': 'payload.message in a type=compacted record',
              'claude': 'message.content in an isCompactSummary=true record',
              'gemini': '$set.messages[id].content containing state_snapshot',
              'opencode': 'part.data.text linked to a summary message row',
              'kilocode': 'part.data.text linked to a summary message row (message.data.summary=true) in kilo.db'}
    client = capture['client']
    return {'client': client, 'session_id': capture['session_id'],
            'storage_path': '/home/agent/' + capture['source_path'],
            'format': 'sqlite' if client in {'opencode', 'kilocode', 'zcode'} else 'jsonl',
            'checkpoint_record_ids': list(record_ids), 'summary_field': fields[client]}


def install_context():
    value = json.load(sys.stdin)
    CONTEXT.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT.write_text(json.dumps(value, indent=2) + '\n')


def wait_for_capture(emit, session_id, deadline):
    emit({'kind': 'checkpoint_ready', 'thread_id': session_id,
          'observed_ns': time.time_ns()})
    while not GATE.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Independent checkpoint capture timed out')
        time.sleep(0.1)
