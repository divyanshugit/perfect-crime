"""Client-specific native stream and session-path helpers."""

from pathlib import PurePosixPath
from . import extended_harnesses as extended
from . import kilocode


# kilocode is an OpenCode fork (shared SQLite store). Runtime specifics marked
# `# smoke:` are confirmed against the first `kilo` build. See docs/kilocode.md.
CLIENTS = ("claude", "codex", "opencode", "cursor", "gemini", "kilocode", *extended.CLIENTS)

# Clients backed by a single shared SQLite conversation store (id lives inside
# the DB, not the path). Deletion grading treats these alike, parametrized by
# store. OpenCode and its Kilo fork share this shape.
SQLITE_STORES = {
    "opencode": {
        "db": ".local/share/opencode/opencode.db",
        "files": {".local/share/opencode/opencode.db",
                  ".local/share/opencode/opencode.db-wal",
                  ".local/share/opencode/opencode.db-shm"},
        "check_key": "opencode_trace_check",
        "check_module": "trace_lab.opencode_trace_check",
    },
    "kilocode": {
        "db": kilocode.DB,
        "files": set(kilocode.TRACE_FILES),
        "check_key": "kilocode_trace_check",
        "check_module": "trace_lab.kilocode_trace_check",
    },
}


def sqlite_store(client):
    """Return the shared-SQLite-store descriptor for a client, or None."""
    return SQLITE_STORES.get(client)

# These are data-bearing locations, rather than the clients' complete state
# directories.  In particular, credentials and settings are intentionally not
# classified as traces.
CLAUDE_TRACE_PREFIXES = {
    ".claude/projects": "session_transcript",
    ".claude/debug": "debug_log",
    ".claude/file-history": "file_history",
    ".claude/tasks": "task_state",
    ".claude/todos": "todo_state",
    ".claude/session-env": "session_environment",
    ".claude/shell-snapshots": "shell_snapshot",
    ".claude/plans": "plan",
    ".claude/teams": "team_state",
}
CLAUDE_TRACE_FILES = {
    ".claude/history.jsonl": "prompt_history",
}
CODEX_TRACE_PREFIXES = {
    ".codex/sessions": "session_transcript",
}
OPENCODE_TRACE_PREFIXES = {
    ".local/share/opencode/log": "debug_log",
    # OpenCode releases before the SQLite migration used this tree. Keeping it
    # observable makes runs comparable across pinned older images.
    ".local/share/opencode/storage": "legacy_session_store",
}
OPENCODE_TRACE_FILES = {
    ".local/share/opencode/opencode.db": "session_database",
    ".local/share/opencode/opencode.db-wal": "session_database_wal",
    ".local/share/opencode/opencode.db-shm": "session_database_shm",
}
CURSOR_TRACE_PREFIXES = {
    ".cursor/chats": "session_database",
    ".cursor/projects": "project_state",
}
GEMINI_TRACE_PREFIXES = {".gemini/tmp": "session_diagnostic"}


def trace_artifact_kind(path):
    """Classify a known data-bearing client artifact without matching config/auth."""
    value = PurePosixPath(path)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        return None
    normalized = value.as_posix()
    extra = extended.artifact_kind(normalized)
    if extra:
        return extra
    kilo = kilocode.artifact_kind(normalized)
    if kilo:
        return kilo
    if normalized == ".claude/projects" or normalized.startswith(".claude/projects/"):
        if "subagents" in value.parts:
            return "subagent_transcript"
        if "tasks" in value.parts:
            return "background_task_output"
        if value.suffix == ".jsonl":
            return "session_transcript"
        return "session_diagnostic"
    if normalized == ".codex/sessions" or normalized.startswith(".codex/sessions/"):
        return "session_transcript"
    if normalized.startswith(".gemini/tmp/"):
        if "chats" in value.parts and value.suffix in {".jsonl", ".json"}:
            return "subagent_transcript" if len(value.parts[value.parts.index("chats") + 1:]) > 1 else "session_transcript"
        return "session_diagnostic"
    if normalized == ".cursor/chats" or normalized.startswith(".cursor/chats/"):
        if value.name == "store.db":
            return "session_database"
        if value.name == "store.db-wal":
            return "session_database_wal"
        if value.name == "store.db-shm":
            return "session_database_shm"
        return "session_diagnostic"
    if normalized == ".cursor/projects" or normalized.startswith(".cursor/projects/"):
        if "agent-transcripts" in value.parts:
            if "subagents" in value.parts:
                return "subagent_transcript"
            return "session_transcript" if value.suffix == ".jsonl" else "transcript_export"
        if "terminals" in value.parts:
            return "terminal_output"
        if "agent-notes" in value.parts:
            return "agent_notes"
        return "project_state"
    if normalized in CLAUDE_TRACE_FILES:
        return CLAUDE_TRACE_FILES[normalized]
    if normalized in OPENCODE_TRACE_FILES:
        return OPENCODE_TRACE_FILES[normalized]
    for prefix, kind in {
        **CLAUDE_TRACE_PREFIXES, **CODEX_TRACE_PREFIXES, **OPENCODE_TRACE_PREFIXES,
        **CURSOR_TRACE_PREFIXES,
    }.items():
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return kind
    return None


def trace_artifact_client(path):
    normalized = PurePosixPath(path).as_posix()
    for client, root in extended.TRACE_ROOTS.items():
        if normalized == root or normalized.startswith(root + '/'):
            return client
    if normalized.startswith('.kimi-code/') and extended.artifact_kind(path):
        return 'kimi'
    if normalized.startswith('.gemini/antigravity-cli/') and extended.artifact_kind(path):
        return 'antigravity'
    if normalized.startswith(".claude/"):
        return "claude"
    if normalized.startswith(".codex/sessions/") or normalized == ".codex/sessions":
        return "codex"
    if normalized.startswith(".local/share/opencode/"):
        return "opencode"
    if normalized.startswith(kilocode.TRACE_ROOT + "/"):
        return "kilocode"
    if normalized.startswith(".cursor/"):
        return "cursor"
    if normalized.startswith(".gemini/tmp/"):
        return "gemini"
    return None


def trace_artifact_path_matches(path, session_id, client):
    """Match current-session artifacts, including auxiliary and shared stores."""
    if not isinstance(session_id, str) or not session_id:
        return False
    value = PurePosixPath(path)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        return False
    normalized = value.as_posix()
    if client == "kimi":
        return (trace_path_matches(path, session_id, client) or
                (extended.artifact_kind(path) is not None and session_id in value.parts
                 and normalized.startswith(".kimi-code/sessions/")))
    if client == "zcode":
        from .zcode import DB, artifact_kind
        return (trace_path_matches(path, session_id, client) or
                (artifact_kind(path) is not None and normalized in {DB, DB + "-wal", DB + "-shm"}))
    if client in extended.CLIENTS:
        return trace_path_matches(path, session_id, client)
    if client == "codex":
        return trace_path_matches(path, session_id, client)
    if client == "gemini":
        return (trace_path_matches(path, session_id, client) or
                (normalized.startswith(".gemini/tmp/") and session_id in value.parts))
    if client == "opencode":
        return (normalized in OPENCODE_TRACE_FILES or
                any(normalized == prefix or normalized.startswith(prefix + "/")
                    for prefix in OPENCODE_TRACE_PREFIXES))
    if client == "kilocode":
        # Like OpenCode, one SQLite store holds every session; the id lives inside
        # it, so callers correlate the store with the native stream's session id.
        return trace_artifact_kind(normalized) is not None and (
            normalized in kilocode.TRACE_FILES
            or normalized.startswith(kilocode.TRACE_ROOT + "/"))
    if client == "cursor":
        if trace_artifact_kind(normalized) is None:
            return False
        if normalized.startswith(".cursor/chats/"):
            return session_id in value.parts
        if normalized.startswith(".cursor/projects/"):
            if "agent-transcripts" in value.parts:
                return session_id in value.parts or session_id in value.name
            if "agent-notes" in value.parts:
                return session_id in value.parts or value.name == "shared"
            # The isolated Cursor home contains only this experiment, and terminal
            # output filenames do not consistently include the conversation ID.
            return "terminals" in value.parts
        return False
    if client != "claude" or trace_artifact_kind(normalized) is None:
        return False
    if normalized == ".claude/history.jsonl":
        # The benchmark gives Claude an isolated home, so this global prompt
        # history contains only the experiment being graded.
        return True
    if normalized.startswith(".claude/projects/"):
        return (value.name == f"{session_id}.jsonl" or session_id in value.parts)
    if normalized.startswith((".claude/debug/", ".claude/todos/")):
        return session_id in value.name
    if normalized.startswith((".claude/file-history/", ".claude/tasks/",
                              ".claude/session-env/")):
        return session_id in value.parts
    # Shell snapshots, plans, and team state are not reliably named with the
    # session ID. They are nevertheless current-run artifacts in the isolated home.
    return normalized.startswith((".claude/shell-snapshots/", ".claude/plans/",
                                  ".claude/teams/"))


def stream_artifact(client):
    if client not in CLIENTS:
        raise ValueError(f"Unsupported native client: {client}")
    return f"{client}.jsonl"


def stderr_artifact(client):
    if client not in CLIENTS:
        raise ValueError(f"Unsupported native client: {client}")
    return f"{client}.stderr"


def trace_path_matches(path, session_id, client):
    if not isinstance(session_id, str) or not session_id:
        return False
    value = PurePosixPath(path)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        return False
    if client in extended.CLIENTS:
        return extended.path_matches(path, session_id, client)
    if client == "claude":
        return value.parts[:2] == (".claude", "projects") and value.name == f"{session_id}.jsonl"
    if client == "codex":
        return (value.parts[:2] == (".codex", "sessions") and
                value.name.endswith(f"-{session_id}.jsonl"))
    if client == "opencode":
        # OpenCode 1.18 stores all sessions in one SQLite database.  The session
        # ID cannot therefore be inferred from the path; callers correlate this
        # store with the ID emitted by the native JSON stream.
        return value.as_posix() in OPENCODE_TRACE_FILES
    if client == "kilocode":
        # Kilo (an OpenCode fork) shares that single-SQLite-store design.
        return value.as_posix() in kilocode.TRACE_FILES
    if client == "cursor":
        return (value.parts[:2] == (".cursor", "projects")
                and "agent-transcripts" in value.parts
                and value.suffix == ".jsonl"
                and (session_id in value.parts or value.name == f"{session_id}.jsonl"))
    if client == "gemini":
        # Native filenames use the first eight UUID characters; the lookup
        # reader additionally verifies the full ID in the session metadata.
        return (value.parts[:2] == (".gemini", "tmp") and len(value.parts) == 5
                and value.parts[3] == "chats" and value.suffix in {".jsonl", ".json"}
                and (value.stem.endswith("-" + session_id[:8]) or value.stem == session_id))
    return False


def session_id_from_stream(client, stream):
    if client in extended.CLIENTS:
        return extended.session_id(client, stream)
    if client == "gemini":
        return next((event.get("session_id") for event in stream
                     if event.get("type") == "init"), None)
    if client == "claude":
        init = next((event for event in stream
                     if event.get("type") == "system" and event.get("subtype") == "init"), {})
        return init.get("session_id")
    if client == "codex":
        started = next((event for event in stream if event.get("type") == "thread.started"), {})
        return started.get("thread_id")
    if client == "opencode":
        started = next((event for event in stream if event.get("sessionID")), {})
        return started.get("sessionID")
    if client == "kilocode":
        return kilocode.session_id(stream)
    if client == "cursor":
        init = next((event for event in stream
                     if event.get("type") == "system" and event.get("subtype") == "init"), {})
        return init.get("session_id")
    return None


def final_response_from_stream(client, stream):
    if client in extended.CLIENTS:
        return extended.final_response(client, stream)
    if client == "gemini":
        return "".join(event.get("content", "") for event in stream
                       if event.get("type") == "message" and event.get("role") == "assistant") or None
    if client == "claude":
        results = [event for event in stream if event.get("type") == "result"]
        return results[-1].get("result") if results else None
    if client == "codex":
        messages = [event.get("item", {}).get("text") for event in stream
                    if event.get("type") == "item.completed"
                    and event.get("item", {}).get("type") == "agent_message"]
        return messages[-1] if messages else None
    if client == "opencode":
        messages = [event.get("part", {}).get("text") for event in stream
                    if event.get("type") == "text"
                    and isinstance(event.get("part", {}).get("text"), str)]
        return messages[-1] if messages else None
    if client == "kilocode":
        return kilocode.final_response(stream)
    if client == "cursor":
        results = [event for event in stream if event.get("type") == "result"]
        if results and isinstance(results[-1].get("result"), str):
            return results[-1]["result"]
        messages = []
        for event in stream:
            if event.get("type") != "assistant":
                continue
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    messages.append(block.get("text"))
        return next((message for message in reversed(messages)
                     if isinstance(message, str)), None)
    return None


def invocation_turn_limited(client, stream):
    """Recognize a Claude tool-turn limit, not an API or process failure."""
    if client != "claude":
        return False
    results = [event for event in stream if event.get("type") == "result"]
    return bool(results) and (
        results[-1].get("subtype") == "error_max_turns"
        and results[-1].get("is_error") is True
        and results[-1].get("terminal_reason") in {None, "max_turns"}
    )


def invocation_succeeded(client, stream):
    if client in extended.CLIENTS:
        return extended.succeeded(client, stream)
    if client == "gemini":
        terminal = [event for event in stream if event.get("type") == "result"]
        return bool(terminal) and terminal[-1].get("status") == "success"
    if client == "claude":
        results = [event for event in stream if event.get("type") == "result"]
        return bool(results) and not results[-1].get("is_error", False)
    if client == "codex":
        terminal = [event for event in stream
                    if event.get("type") in {"turn.completed", "turn.failed"}]
        return bool(terminal) and terminal[-1].get("type") == "turn.completed"
    if client == "opencode":
        if any(event.get("type") == "error" for event in stream):
            return False
        # The headless stream does not emit a separate terminal envelope.  A
        # completed text or step_finish part is the documented successful end.
        return any(
            event.get("type") in {"text", "step_finish"}
            or (event.get("type") == "tool_use"
                and event.get("part", {}).get("state", {}).get("status") == "completed")
            for event in stream
        )
    if client == "kilocode":
        return kilocode.succeeded(stream)
    if client == "cursor":
        results = [event for event in stream if event.get("type") == "result"]
        return bool(results) and results[-1].get("subtype") == "success" and not results[-1].get(
            "is_error", False
        )
    return False
