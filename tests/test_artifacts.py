from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from bugagent.artifacts import ArtifactStore
from bugagent.demo import build_demo_bundle


class ArtifactStoreTests(unittest.TestCase):
    def test_writes_hash_manifest_and_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = ArtifactStore(Path(temporary_directory) / "runs").write(build_demo_bundle())
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(set(manifest["artifacts"]), {
                "candidates.json", "evidence.json", "ticket.json", "timeline.ndjson", "verdict.json"
            })
            ticket_hash = hashlib.sha256((path / "ticket.json").read_bytes()).hexdigest()
            self.assertEqual(manifest["artifacts"]["ticket.json"], ticket_hash)
            self.assertEqual(len((path / "timeline.ndjson").read_text(encoding="utf-8").splitlines()), 4)

    def test_refuses_to_overwrite_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ArtifactStore(Path(temporary_directory))
            bundle = build_demo_bundle()
            store.write(bundle)
            with self.assertRaises(FileExistsError):
                store.write(bundle)


if __name__ == "__main__":
    unittest.main()
