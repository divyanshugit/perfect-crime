"""Kilo peer logs / peer workspaces settings."""

from contextlib import closing
import sqlite3
import unittest

from host_lab import rollout_influence_loop as loop
from host_lab.opencode_peer_fixture import KILO_VERSION, OPENCODE_VERSION


class KiloPeerSnapshotTests(unittest.TestCase):
    def test_every_peer_state_is_a_kilo_sqlite_snapshot(self):
        files, truth = loop.peer_files("kilocode")
        self.assertEqual({p["trace_state"] for p in truth},
                         {"full", "partial", "redacted", "empty", "absent"})
        for peer in truth:
            self.assertTrue(peer["session_id"].startswith("ses_"))
            self.assertTrue(peer["trace_path"].endswith("/home/.local/share/kilo/kilo.db"))
            if peer["trace_state"] == "absent":
                self.assertNotIn(peer["trace_path"], files)
                continue
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.deserialize(files[peer["trace_path"]])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                count = connection.execute("SELECT count(*) FROM part").fetchone()[0]
                self.assertEqual(count, {"full": 2, "redacted": 2, "partial": 1, "empty": 0}[peer["trace_state"]])
                versions = [row[0] for row in connection.execute("SELECT version FROM session")]
                self.assertEqual(versions, [] if peer["trace_state"] == "empty" else [KILO_VERSION])

    def test_redacted_peer_masks_identities(self):
        files, truth = loop.peer_files("kilocode")
        redacted = next(p for p in truth if p["trace_state"] == "redacted")
        _, identities = loop.peer_case(redacted["input_case_variant"])
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.deserialize(files[redacted["trace_path"]])
            text = "\n".join(r[0] for r in connection.execute("SELECT data FROM part"))
        self.assertEqual(text.count("[ANONYMOUS]"), len(identities))
        for name, _ in identities:
            self.assertNotIn(name, text)

    def test_opencode_snapshots_keep_their_version_and_path(self):
        files, truth = loop.peer_files("opencode")
        full = next(p for p in truth if p["trace_state"] == "full")
        self.assertTrue(full["trace_path"].endswith("home/.local/share/opencode/opencode.db"))
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.deserialize(files[full["trace_path"]])
            self.assertEqual(connection.execute("SELECT version FROM session").fetchone()[0],
                             OPENCODE_VERSION)

    def test_settings_and_cli(self):
        setting = loop.build_setting("kilocode")
        self.assertEqual(setting["metadata"]["opencode_storage_grading_version"], "wal-aware-session-rows-v1")
        self.assertTrue(all(d.endswith("home/.local/share/kilo") for d in setting["reference_directories"]))
        logs = loop.build_log_setting()
        self.assertEqual(logs["scenario_type"], "bounded_recursive_rollout_influence")
        args = loop.parser().parse_args(["--client", "kilocode", "--model", "m", "--context", "rollout-logs"])
        self.assertEqual(args.client, "kilocode")
        nested = loop.base.main_parser().parse_args(loop.base.experiment_arguments(args))
        self.assertEqual(nested.client, "kilocode")


if __name__ == "__main__":
    unittest.main()
