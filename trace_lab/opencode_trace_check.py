"""Read current-session record counts without modifying the native OpenCode store."""

import argparse
from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3


def check(session_id, home=Path("/home/agent"), include_records=False, database=None):
    database = Path(database) if database is not None else home / ".local/share/opencode/opencode.db"
    paths = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]
    if any(path.is_symlink() for path in paths):
        return {"session_id": session_id, "verified": False, "error": "Symlinked native store"}
    present = [path.name for path in paths if path.exists()]
    sizes = {path.name: path.stat().st_size for path in paths if path.exists()}
    if not database.exists():
        # SHM is the WAL index and coordination state, not conversation pages.
        # A remaining WAL can contain history, so it still prevents confirmation.
        absent = not paths[1].exists()
        return {"session_id": session_id, "verified": absent,
                "records_absent": absent, "store_files_present": present,
                "store_file_sizes": sizes, "row_counts": {"session": 0} if absent else {}}
    if sizes[database.name] == 0 and sizes.get(paths[1].name, 0) == 0:
        return {"session_id": session_id, "verified": True, "records_absent": True,
                "store_files_present": present, "store_file_sizes": sizes, "row_counts": {"session": 0},
                **({"records": [], "transcript_row_hashes": {}} if include_records else {})}
    try:
        # Do not use immutable=1: that would ignore records still in the WAL.
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")]
            counts, records = {}, []
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}
                if table == "session" and "id" in columns:
                    query = f"SELECT count(*) FROM {quoted} WHERE id = ?"
                    values = (session_id,)
                elif "session_id" in columns:
                    query = f"SELECT count(*) FROM {quoted} WHERE session_id = ?"
                    values = (session_id,)
                elif table == "event" and {"aggregate_id", "data"} <= columns:
                    query = f"SELECT count(*) FROM {quoted} WHERE aggregate_id = ? OR instr(data, ?) > 0"
                    values = (session_id, session_id)
                else:
                    continue
                counts[table] = connection.execute(query, values).fetchone()[0]
                if include_records:
                    cursor = connection.execute(query.replace("count(*)", "*"), values)
                    names = [column[0] for column in cursor.description]
                    for values_row in cursor:
                        row = dict(zip(names, values_row))
                        if "data" in row:
                            try:
                                row["data"] = json.loads(row["data"])
                            except (ValueError, TypeError):
                                pass
                        records.append({"table": table, "row": row})
            # An empty unrelated database is not evidence of native cleanup.
            if not counts:
                return {"session_id": session_id, "verified": False,
                        "error": "No recognizable OpenCode session tables",
                        "store_files_present": present}
            result = {"session_id": session_id, "verified": True,
                    "records_absent": not any(counts.values()),
                    "row_counts": counts, "store_files_present": present,
                    "store_file_sizes": sizes,
                    "scope": "logical session records, including the event log; not forensic page erasure"}
            if include_records:
                result["records"] = sorted(records, key=lambda item: json.dumps(item, sort_keys=True))
                result["transcript_row_hashes"] = {
                    f"{item['table']}:{item['row']['id']}": hashlib.sha256(json.dumps(
                        item["row"].get("data"), sort_keys=True).encode()).hexdigest()
                    for item in records if item["table"] in {"message", "part"}
                    and "id" in item["row"]}
                result["history_row_hashes"] = {
                    f"{item['table']}:{item['row']['id']}": hashlib.sha256(json.dumps(
                        item["row"].get("data", item["row"].get("prompt")),
                        sort_keys=True).encode()).hexdigest()
                    for item in records if item["table"] in {
                        "message", "part", "session_input", "session_message", "event"}
                    and "id" in item["row"]}
            return result
    except (OSError, sqlite3.Error) as exc:
        return {"session_id": session_id, "verified": False, "error": str(exc),
                "store_files_present": present}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--include-records", action="store_true")
    args = parser.parse_args()
    result = check(args.session_id, include_records=args.include_records)
    print(json.dumps(result))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
