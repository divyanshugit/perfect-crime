"""Record the native child's PID and actual wait status, without changing its tools.

This supervisor never deletes transcripts or signals the child. Its framed output
is captured on the host; the filesystem observer remains in a separate container.
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time


def main():
    command = sys.argv[1:]
    native = bool(command) and command[0] in {"claude", "codex", "opencode", "cursor-agent", "gemini", "muse", "grok", "agy", "zcode", "kimi", "kilo"}
    two_turn_driver = command[:3] == ["python3", "-m", "trace_lab.claude_two_turn"]
    muse_driver = command[:3] == ['python3', '-m', 'trace_lab.muse_driver']
    kimi_driver = command[:3] == ['python3', '-m', 'trace_lab.kimi_driver']
    if not (native or two_turn_driver or muse_driver or kimi_driver):
        raise SystemExit("Expected a supported native agent executable")
    lock = threading.Lock()

    def emit(kind, **fields):
        with lock:
            print(json.dumps({"kind": kind, "observed_ns": time.time_ns(), **fields}), flush=True)

    # The bounded prompt is supplied by the host, with EOF, just as for normal -p.
    prompt = sys.stdin.buffer.read(1024 * 1024)
    if command[0] in {'grok', 'agy', 'zcode', 'kimi', 'kilo'} or command[:2] == ['muse', 'exec']:
        # `kilo run <message>` and `muse exec <prompt>` take the prompt as a
        # positional; grok/agy/zcode/kimi take it after -p. (-p is --password in
        # `kilo attach`, so it must not be added for kilo.)
        command = [*command, *(['-p'] if command[0] in {'grok', 'agy', 'zcode', 'kimi'} else []), prompt.decode('utf-8')]
        prompt = b''
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    emit("started", pid=child.pid, supervisor_pid=os.getpid())

    def forward(channel, source):
        while data := source.read1(65536):
            emit(channel, data_b64=base64.b64encode(data).decode())

    threads = [threading.Thread(target=forward, args=(channel, source), daemon=True)
               for channel, source in (("stdout", child.stdout), ("stderr", child.stderr))]
    for thread in threads:
        thread.start()
    try:
        child.stdin.write(prompt)
        child.stdin.close()
    except BrokenPipeError:
        pass
    code = child.wait()
    # Descendants may retain output pipes briefly after the native process exits.
    for thread in threads:
        thread.join(timeout=3)
    emit("exited", pid=child.pid, returncode=code,
         signal=-code if code < 0 else None,
         output_drained=not any(thread.is_alive() for thread in threads))


if __name__ == "__main__":
    main()
