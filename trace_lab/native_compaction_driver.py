"""Drive built-in compaction without changing model prompts or session history."""

import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import pty
import queue
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

from .compaction_history import gemini_scope
from .compaction_skill_fixture import PAYLOAD
from .compaction_checkpoint import wait_for_capture


def emit(kind, **fields):
    print(json.dumps(dict(kind=kind, observed_ns=time.time_ns(), **fields)), flush=True)


class Driver:
    def __init__(self, spec):
        self.spec = spec
        self.deadline = time.monotonic() + spec['timeout']
        self.phase = 'initialize'
        self.child = None
        self.sid = None
        self.label = 'OpenCode'

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Native compaction time budget exhausted')
        return remaining

    def start_phase(self, name):
        self.phase = name
        emit('phase_started', phase=name)

    def finish_phase(self):
        emit('phase_completed', phase=self.phase, thread_id=self.sid)
        if self.phase == 'compaction' and self.spec.get('capture_checkpoint'):
            wait_for_capture(lambda record: print(json.dumps(record), flush=True),
                             self.sid, self.deadline)

    def started(self, command, **kwargs):
        self.child = subprocess.Popen(command, **kwargs)
        emit('native_started', pid=self.child.pid, native_argv=command)
        return self.child

    def exited(self, code, drained=True):
        emit('native_exited', pid=self.child.pid, exit_code=code, output_drained=drained)
        self.child = None

    def claude(self):
        self.sid = str(uuid.uuid4())
        emit('session_started', thread_id=self.sid)
        for phase, prompt in [('diagnosis', self.spec['task_prompt']),
                              ('prepare', self.spec['prepare_prompt']),
                              ('compaction', '/compact'),
                              ('continuation', self.spec['continue_prompt'])]:
            self.start_phase(phase)
            command = ['claude', '-p', '--output-format', 'stream-json', '--verbose',
                       '--model', self.spec['model'], '--dangerously-skip-permissions',
                       '--session-id' if phase == 'diagnosis' else '--resume', self.sid, prompt]
            child = self.started(command, stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
            incoming = queue.Queue()

            def read():
                try:
                    for line in child.stdout:
                        incoming.put(json.loads(line))
                except (ValueError, OSError) as exc:
                    incoming.put(exc)
                finally:
                    incoming.put(None)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            result = None
            boundary = False
            while True:
                try:
                    value = incoming.get(timeout=self.remaining())
                except queue.Empty:
                    raise RuntimeError('Claude phase timed out') from None
                if value is None:
                    break
                if isinstance(value, Exception):
                    raise value
                emit('native_event', phase=phase, value=value)
                boundary |= value.get('type') == 'system' and value.get('subtype') == 'compact_boundary'
                if value.get('type') == 'result':
                    result = value
            code = child.wait(timeout=self.remaining())
            reader.join(timeout=1)
            self.exited(code, not reader.is_alive())
            if code or not result or result.get('is_error'):
                raise RuntimeError('Claude phase failed: ' + str(result))
            if phase == 'compaction' and not boundary:
                raise RuntimeError('Claude /compact did not emit a native compact_boundary')
            self.finish_phase()

    def http(self, method, path, body=None):
        emit('native_http_request', phase=self.phase, method=method, path=path, body=body)
        connection = http.client.HTTPConnection('127.0.0.1', 4096,
                                                timeout=min(10, self.remaining()) if self.phase == 'initialize' else self.remaining())
        try:
            connection.request(method, path + '?directory=/workspace', None if body is None else json.dumps(body),
                               {'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if raw else None
            emit('native_http_response', phase=self.phase, path=path, status=response.status, value=value)
            if response.status >= 400:
                raise RuntimeError(self.label + ' native endpoint failed: ' + str(value))
            return value
        finally:
            connection.close()

    def opencode(self):
        return self.serve('opencode')

    def kilocode(self):
        # Kilo (an OpenCode fork) exposes the same POST /session/{id}/summarize.
        # Its newer POST /api/session/{id}/compact is not used: the legacy route
        # returns the finished boolean and matches OpenCode's persisted shape.
        return self.serve('kilo')

    def serve(self, binary):
        self.label = 'Kilo' if binary == 'kilo' else 'OpenCode'
        child = self.started([binary, '--pure', '--print-logs', '--log-level', 'DEBUG', 'serve', '--hostname', '127.0.0.1', '--port', '4096'],
                             stdout=sys.stderr, stderr=sys.stderr)
        while True:
            self.remaining()
            if child.poll() is not None:
                raise RuntimeError(self.label + ' server exited during startup')
            try:
                health = self.http('GET', '/global/health')
                if health.get('healthy') is not True:
                    raise RuntimeError(self.label + ' server is not healthy')
                break
            except (ConnectionRefusedError, ConnectionResetError, TimeoutError):
                time.sleep(0.1)
        session = self.http('POST', '/session', {})
        if self.spec.get('history_policy') == 'ensure-exposure':
            config = self.http('GET', '/config')
            if config.get('compaction', {}).get('tail_turns') != 0:
                raise RuntimeError(self.label + ' native full-history compaction was not configured')
        self.sid = session['id']
        emit('session_started', thread_id=self.sid)
        model = {'providerID': 'trace_lab', 'modelID': self.spec['model']}
        for phase, prompt in [('diagnosis', self.spec['task_prompt']),
                              ('prepare', self.spec['prepare_prompt']),
                              ('compaction', None), ('continuation', self.spec['continue_prompt'])]:
            self.start_phase(phase)
            if phase == 'compaction':
                result = self.http('POST', '/session/' + self.sid + '/summarize', dict(model, auto=False))
                if result is not True:
                    raise RuntimeError(self.label + ' native summarize did not complete')
            else:
                result = self.http('POST', '/session/' + self.sid + '/message',
                                   {'model': model, 'parts': [{'type': 'text', 'text': prompt}]})
                if not result or result.get('info', {}).get('error'):
                    raise RuntimeError(self.label + ' message failed: ' + str(result))
            self.http('GET', '/session/' + self.sid + '/message')
            self.finish_phase()
        child.terminate()
        code = child.wait(timeout=10)
        # SIGTERM is the native server's normal supervised shutdown.
        self.exited(code, True)

    def gemini(self):
        hook_file = Path('/tmp/trace-lab-gemini-compaction-hooks.jsonl')
        settings_path = Path.home() / '.gemini/settings.json'
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        for event in ('AfterAgent', 'PreCompress'):
            settings.setdefault('hooks', {})[event] = [{'hooks': [{
                'type': 'command', 'name': 'trace-lab-observe',
                'command': 'python3 -m trace_lab.gemini_compaction_hook', 'timeout': 10000}]}]
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings))
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 180, 0, 0))
        command = ['gemini', '--approval-mode', 'yolo', '--skip-trust',
                   '--model', self.spec['model'], '-i', self.spec['task_prompt']]
        self.start_phase('diagnosis')
        child = self.started(command, stdin=slave, stdout=slave, stderr=slave,
                             env=dict(os.environ, TERM='xterm-256color', NO_COLOR='1'))
        os.close(slave)
        ui = ''
        cursor = 0
        agents = 0

        def pump():
            nonlocal ui, cursor, agents
            self.remaining()
            if child.poll() is not None:
                raise RuntimeError('Gemini interactive process exited prematurely')
            if select.select([master], [], [], 0.1)[0]:
                raw = os.read(master, 65536)
                chunk = raw.decode(errors='replace')
                ui += chunk
                emit('native_ui', phase=self.phase, text=chunk)
            if hook_file.exists():
                raw_hooks = hook_file.read_text()
                lines = raw_hooks.splitlines()
                if raw_hooks and not raw_hooks.endswith('\n'):
                    lines = lines[:-1]
                for line in lines[cursor:]:
                    hook = json.loads(line)
                    emit('native_hook', phase=self.phase, value=hook)
                    if self.sid is None:
                        self.sid = hook['session_id']
                        emit('session_started', thread_id=self.sid)
                    elif hook['session_id'] != self.sid:
                        raise RuntimeError('Gemini hook session changed')
                    agents += hook.get('hook_event_name') == 'AfterAgent'
                cursor = len(lines)

        def wait_agent(count):
            while agents < count:
                pump()
            until = time.monotonic() + 0.5
            while time.monotonic() < until:
                pump()

        def submit(text):
            # Ink distinguishes a pasted block from a separate Enter key.
            os.write(master, text.encode())
            until = time.monotonic() + 0.2
            while time.monotonic() < until:
                pump()
            os.write(master, b'\r')
            if text == '/quit':
                return
            # Drain UI output after the Enter key without waiting for the model.
            until = time.monotonic() + 0.5
            while time.monotonic() < until:
                pump()

        def scope_probe(index):
            path = Path('/tmp') / f'trace-lab-active-history-{index}.json'
            submit('/chat share ' + str(path))
            while True:
                pump()
                try:
                    raw = path.read_bytes()
                    history = json.loads(raw)
                except (OSError, ValueError):
                    continue
                if not isinstance(history, list):
                    raise RuntimeError('Native Gemini history export is not an array')
                scope = gemini_scope(history, PAYLOAD)
                emit('native_history_export', phase=self.phase, thread_id=self.sid,
                     path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                     history=history, scope=scope, export_command='/chat share')
                return scope
        try:
            wait_agent(1)
            self.finish_phase()
            self.start_phase('prepare')
            submit(self.spec['prepare_prompt'])
            wait_agent(2)
            self.finish_phase()
            next_agent = 3
            if self.spec.get('history_policy') == 'ensure-exposure':
                scope = scope_probe(0)
                # Do not turn non-discovery into an explicit skill cue. Reviews
                # are only needed when loaded material is stranded in the tail.
                if scope['payload_in_history'] and not scope['payload_in_summarized_head']:
                    for index, prompt in enumerate(self.spec.get('review_prompts', []), 1):
                        self.start_phase('review-' + str(index))
                        submit(prompt)
                        wait_agent(next_agent)
                        next_agent += 1
                        self.finish_phase()
                        scope = scope_probe(index)
                        if scope['payload_in_summarized_head']:
                            break
                    if not scope['payload_in_summarized_head']:
                        raise RuntimeError('Loaded Gemini skill remains outside native compaction scope after task reviews')
            self.start_phase('compaction')
            start = len(ui)
            submit('/compress')
            while True:
                pump()
                clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', ui[start:])
                if re.search(r'Chat history compressed from\s+[\d,]+\s+to\s+[\d,]+\s+tokens\.', clean):
                    emit('native_compaction_completed', phase=self.phase, thread_id=self.sid)
                    break
                failures = ('Compression was not beneficial', 'Chat history compression did not reduce size',
                            'Could not compress chat history', 'Chat history compression failed',
                            'Nothing to compress.', 'Failed to compress chat history')
                if any(failure in clean for failure in failures):
                    raise RuntimeError('Gemini native runtime did not accept the compaction summary')
            self.finish_phase()
            self.start_phase('continuation')
            submit(self.spec['continue_prompt'])
            wait_agent(next_agent)
            self.finish_phase()
            submit('/quit')
            while True:
                self.remaining()
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        emit('native_ui', phase='shutdown', text=os.read(master, 65536).decode(errors='replace'))
                    except OSError:
                        break
                elif child.poll() is not None:
                    break
            code = child.wait(timeout=self.remaining())
            self.exited(code, True)
            if code:
                raise RuntimeError('Gemini interactive process failed')
        finally:
            os.close(master)

    def kimi(self):
        from .kimi_driver import Connection
        connection = Connection(lambda e: emit('native_event', phase=self.phase, value=e))
        self.child = connection.child
        emit('native_started', pid=self.child.pid, native_argv=['kimi', 'acp'])
        try:
            self.sid = connection.session('/workspace')
            emit('session_started', thread_id=self.sid)
            for phase, prompt in [('diagnosis', self.spec['task_prompt']),
                                  ('prepare', self.spec['prepare_prompt']),
                                  ('compaction', None), ('continuation', self.spec['continue_prompt'])]:
                self.start_phase(phase)
                if phase == 'compaction':
                    connection.compact(self.sid)
                    emit('native_compaction_completed', thread_id=self.sid)
                else:
                    connection.turn(self.sid, prompt)
                self.finish_phase()
        finally:
            connection.close()
            self.exited(connection.child.returncode, not connection.reader.is_alive())

    def zcode(self):
        return self.grok(client='zcode')

    def grok(self, client='grok'):
        from .extended_harnesses import native_command, session_id, succeeded
        for phase, prompt in [('diagnosis', self.spec['task_prompt']),
                              ('prepare', self.spec['prepare_prompt']),
                              ('compaction', '/compact'),
                              ('continuation', self.spec['continue_prompt'])]:
            self.start_phase(phase)
            command = native_command(client, self.spec['model'], '/workspace', self.sid,
                                     resume=self.sid is not None) + ['-p', prompt]
            child = self.started(command, stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
            incoming = queue.Queue()
            def read():
                try:
                    for line in child.stdout:
                        incoming.put(json.loads(line))
                except Exception as exc:
                    incoming.put(exc)
                finally:
                    incoming.put(None)
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            events = []
            while True:
                value = incoming.get(timeout=self.remaining())
                if value is None:
                    break
                if isinstance(value, Exception):
                    raise value
                events.append(value)
                emit('native_event', phase=phase, value=value)
            code = child.wait(timeout=self.remaining())
            reader.join(timeout=1)
            self.exited(code, not reader.is_alive())
            sid = session_id(client, events)
            if sid and self.sid and sid != self.sid:
                raise RuntimeError(f'{client} changed session identity during compaction')
            if sid and not self.sid:
                self.sid = sid
                emit('session_started', thread_id=self.sid)
            if code or (phase != 'compaction' and not succeeded(client, events)):
                raise RuntimeError(f'{client} phase failed')
            if phase == 'compaction':
                boundary = (any(e.get('type') == 'session.updated' and e.get('payload', {}).get('status') == 'completed' and e.get('payload', {}).get('boundaryId') for e in events) if client == 'zcode' else any(e.get('type') == 'auto_compact_completed' for e in events))
                if not boundary:
                    raise RuntimeError(f'{client} /compact did not emit a native compaction boundary')
                emit('native_compaction_completed', thread_id=self.sid)
            self.finish_phase()

    def muse(self):
        from .muse_driver import Connection
        connection = Connection(lambda e: emit('native_event', phase=self.phase, value=e), self.remaining())
        self.child = connection.child
        emit('native_started', pid=self.child.pid,
             native_argv=['muse', 'serve', '--disable-sandbox', '--trust-workspace'])
        try:
            self.sid = connection.session(self.spec['model'], '/workspace')
            emit('session_started', thread_id=self.sid)
            for phase, prompt in [('diagnosis', self.spec['task_prompt']),
                                  ('prepare', self.spec['prepare_prompt']),
                                  ('compaction', None),
                                  ('continuation', self.spec['continue_prompt'])]:
                self.start_phase(phase)
                if phase == 'compaction':
                    connection.compact(self.sid)
                    emit('native_compaction_completed', thread_id=self.sid)
                else:
                    connection.turn(self.sid, prompt)
                self.finish_phase()
        finally:
            connection.close()
            self.exited(connection.child.returncode, not connection.reader.is_alive())

    def run(self):
        try:
            getattr(self, self.spec['client'])()
        finally:
            if self.child is not None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait()
                self.exited(self.child.returncode, False)


def main():
    Driver(json.load(sys.stdin)).run()


if __name__ == '__main__':
    main()
