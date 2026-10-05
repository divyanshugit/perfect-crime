"""Official Kilo CLI (@kilocode/cli, binary `kilo`), a fork of OpenCode.

Kilo pins its **own** Kilo Code hosted provider. The native SQLite store and the
`--format json` event stream follow OpenCode, so classification, grading and the
read-only trace reader reuse the OpenCode machinery. The native store is the
resumable conversation record, not a harness-created transcript.

Confirmed against @kilocode/cli 7.8.1: `kilo debug paths` reports the data dir
`~/.local/share/kilo` with the store `kilo.db` (+ `-wal`/`-shm`), config at
`~/.config/kilo`, and the SQLite schema (session/message/part/event/
session_input/session_message) matches OpenCode's reader.
"""
import json
from pathlib import Path, PurePosixPath

# Confirmed via `kilo debug paths` (7.8.1): data dir is `.local/share/kilo`.
TRACE_ROOT = '.local/share/kilo'
DB = TRACE_ROOT + '/kilo.db'
TRACE_FILES = {
    DB: 'session_database',
    DB + '-wal': 'session_database_wal',
    DB + '-shm': 'session_database_shm',
}


def artifact_kind(path):
    p = PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts:
        return None
    path = p.as_posix()
    if path in TRACE_FILES:
        return TRACE_FILES[path]
    for directory, kind in [('log', 'debug_log'), ('storage', 'legacy_session_store')]:
        root = TRACE_ROOT + '/' + directory
        if path == root or path.startswith(root + '/'):
            return kind
    if path == TRACE_ROOT or path.startswith(TRACE_ROOT + '/'):
        return 'session_diagnostic'
    return None


def path_matches(path, sid):
    # Like OpenCode, sessions share one DB; the session id lives inside the
    # store, not the path. The canonical trace target is the DB file itself.
    return bool(sid) and PurePosixPath(path).as_posix() in TRACE_FILES


def native_command(model, workspace, sid=None, resume=False, effort=None, permissions='full'):
    """Build the `kilo run` invocation for one turn.

    full  - the first turn passes --auto; resumed turns rely on the blanket
            ``permission: allow`` that ``initialize`` writes (kept as originally
            run, so existing full-access results stay comparable).
    auto  - Kilo's own default permission rules plus --auto on EVERY turn, the same
            shape as OpenCode's auto profile. Anything not explicitly denied is
            approved; explicit default denies still apply. (Verified against 7.8.1
            that --auto combines with --session; only -c/--continue is documented
            as incompatible.)
    """
    if effort:
        raise ValueError('Kilo reasoning-effort (--variant) override is not validated; omit --effort')
    if permissions not in {'full', 'auto'}:
        raise ValueError('Unknown permissions profile: ' + str(permissions))
    # Confirmed against 7.8.1: `kilo run --format json --dir <ws> -m provider/model`.
    cmd = ['kilo', 'run', '--format', 'json', '--model', model, '--dir', workspace]
    if resume:
        if not sid:
            raise ValueError('Kilo resume requires the native session ID')
        cmd += ['--session', sid]
    if permissions == 'auto' or not resume:
        cmd += ['--auto']
    return cmd


def initialize(model, home, permissions='full'):
    """Write a single custom provider pinned to the loopback gateway.

    Uses the inherited OpenCode config schema (openai-compatible provider with a
    `baseURL`/`apiKey` override) so the agent never reaches a real upstream. The
    gateway holds the real key and enforces the model.

    Full access adds a blanket ``permission: allow``. A per-tool allowlist is not
    enough: it misses e.g. external_directory (an agent globbing ~/.local/share for
    its own trace store), which was auto-rejected and aborted the resumed turn. The
    auto profile leaves Kilo's native permission defaults untouched.
    """
    if permissions not in {'full', 'auto'}:
        raise ValueError('Unknown permissions profile: ' + str(permissions))
    path = Path(home) / '.config/kilo/kilo.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        'provider': {
            'trace_lab': {
                'npm': '@ai-sdk/openai-compatible',
                'name': 'Trace Lab',
                'options': {
                    'baseURL': 'http://127.0.0.1:8080/v1',
                    'apiKey': 'trace-lab-placeholder',
                },
                'models': {model: {}},
            },
        },
        'model': f'trace_lab/{model}',
    }
    if permissions == 'full':
        config['permission'] = 'allow'
    path.write_text(json.dumps(config) + '\n')
    return path


def session_id(stream):
    return next((event.get('sessionID') for event in stream
                 if isinstance(event.get('sessionID'), str)), None)


def succeeded(stream):
    if any(event.get('type') == 'error' for event in stream):
        return False
    return any(
        event.get('type') in {'text', 'step_finish'}
        or (event.get('type') == 'tool_use'
            and event.get('part', {}).get('state', {}).get('status') == 'completed')
        for event in stream
    )


def final_response(stream):
    messages = [event.get('part', {}).get('text') for event in stream
                if event.get('type') == 'text'
                and isinstance(event.get('part', {}).get('text'), str)]
    return messages[-1] if messages else None


def tool_inputs(stream):
    """Yield completed native tool calls, bound to session and call id.

    Pending/failed calls never count as success; a non-zero recorded exit demotes
    a call to error, matching the OpenCode grading.
    """
    for event in stream:
        if event.get('type') != 'tool_use':
            continue
        part = event.get('part', {})
        if part.get('type') != 'tool':
            continue
        state = part.get('state', {})
        cid = part.get('callID')
        if not cid:
            continue
        arguments = dict(state.get('input') or {})
        ok = (state.get('status') == 'completed'
              and state.get('metadata', {}).get('exit', 0) == 0)
        arguments.update(
            _native_status='completed' if ok else 'error',
            _native_output=state.get('output', ''),
            _native_error=None if ok else state.get('output') or 'Tool completion missing')
        yield cid, part.get('tool', ''), arguments
