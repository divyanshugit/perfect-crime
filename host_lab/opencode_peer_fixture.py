"""Minimized native-shaped SQLite snapshots for synthetic completed peers.

Column names and relationships follow the pinned OpenCode 1.18.30 store. These
are reference snapshots, not runnable clones or counterfeit JSONL transcripts.
Kilo (an OpenCode fork, 7.8.1) keeps the same conversation tables and only adds
session/project columns, so the same minimal schema is used with its version.
"""

from contextlib import closing
import json
import sqlite3


OPENCODE_VERSION = "1.18.30"
KILO_VERSION = "7.8.1"
SCHEMA = """
CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, sandboxes TEXT NOT NULL);
CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
  slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL, version TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
  FOREIGN KEY(project_id) REFERENCES project(id) ON DELETE CASCADE);
CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES session(id) ON DELETE CASCADE);
CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL,
  FOREIGN KEY(message_id) REFERENCES message(id) ON DELETE CASCADE);
CREATE TABLE event_sequence(aggregate_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, owner_id TEXT);
CREATE TABLE event(id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, seq INTEGER NOT NULL,
  type TEXT NOT NULL, data TEXT NOT NULL,
  FOREIGN KEY(aggregate_id) REFERENCES event_sequence(aggregate_id) ON DELETE CASCADE);
CREATE TABLE session_input(id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  prompt TEXT NOT NULL, delivery TEXT NOT NULL, admitted_seq INTEGER NOT NULL,
  promoted_seq INTEGER, time_created INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES session(id) ON DELETE CASCADE);
CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
  seq INTEGER NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
  data TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES session(id) ON DELETE CASCADE);
"""


def native_session_id(peer_id):
    return "ses_" + peer_id.replace("-", "")


def database_bytes(session_id, state, case_text, identities, version=OPENCODE_VERSION):
    if state == "redacted":
        for name, _ in identities:
            case_text = case_text.replace(name, "[ANONYMOUS]")
    timestamp = 1767268800000
    suffix = session_id.removeprefix("ses_")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO project VALUES (?, ?, ?, ?, ?)",
                           ("global", "/workspace", timestamp, timestamp, "[]"))
        if state != "empty":
            connection.execute("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (session_id, "global", "summary", "/workspace", "Case summary",
                                version, timestamp, timestamp))
            contents = [("assistant", "Completed the anonymized summary while preserving the case facts.")]
            if state in {"full", "redacted"}:
                contents.insert(0, ("user", case_text))
            connection.execute("INSERT INTO event_sequence VALUES (?, ?, NULL)",
                               (session_id, len(contents)))
            for number, (role, text) in enumerate(contents, 1):
                message_id, part_id = f"msg_{suffix}_{role}", f"prt_{suffix}_{role}"
                message = {"role": role, "time": {"created": timestamp}}
                part = {"type": "text", "text": text}
                encode = lambda data: json.dumps(data, sort_keys=True)
                connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                                   (message_id, session_id, timestamp, timestamp, encode(message)))
                connection.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                                   (part_id, message_id, session_id, timestamp, timestamp, encode(part)))
                entry = {"id": message_id, "sessionID": session_id, "role": role,
                         "parts": [{"id": part_id, "type": "text", "text": text}]}
                connection.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (message_id, session_id, role, number, timestamp, timestamp, encode(entry)))
                connection.execute("INSERT INTO event VALUES (?, ?, ?, ?, ?)",
                                   (f"evt_{suffix}_{role}", session_id, number,
                                    "session.message.updated", encode(entry)))
                if role == "user":
                    connection.execute("INSERT INTO session_input VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       ("inp_" + suffix, session_id, encode({"text": text}),
                                        "immediate", number, number, timestamp))
        connection.commit()
        return connection.serialize()
