from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime

from bugagent.artifacts import ArtifactStore
from bugagent.demo import build_demo_bundle
from bugagent.web import RunStore


class RunStoreTests(unittest.TestCase):
    def test_lists_and_loads_valid_artifact_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = build_demo_bundle()
            ArtifactStore(Path(directory)).write(bundle)
            store = RunStore(Path(directory))
            summaries = store.list_runs()
            loaded = store.get_run(str(bundle.run_id))

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["status"], "REPRODUCED")
        assert loaded is not None
        self.assertEqual(loaded["ticket"]["id"], "DEMO-42")
        self.assertEqual(len(loaded["events"]), 4)
        datetime.fromisoformat(loaded["events"][-1]["occurred_at"])

    def test_rejects_path_traversal_as_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(RunStore(Path(directory)).get_run("../secret"))


if __name__ == "__main__":
    unittest.main()
