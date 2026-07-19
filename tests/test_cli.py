from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest

from bugagent.cli import _ticket_from_args


class InvestigationCliTests(unittest.TestCase):
    def test_ticket_from_explicit_arguments(self) -> None:
        args = argparse.Namespace(
            ticket_file=None,
            ticket_id="LOCAL-1",
            title="Fresh records fail during normal close",
            body="A customer says the close action crashes on a new record.",
            repo_ref="fixture@abc",
            expected_error=None,
        )

        ticket = _ticket_from_args(args, argparse.ArgumentParser())

        self.assertEqual(ticket.id, "LOCAL-1")
        self.assertEqual(ticket.title, "Fresh records fail during normal close")
        self.assertEqual(ticket.repo_ref, "fixture@abc")

    def test_ticket_from_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ticket_file = Path(directory) / "ticket.json"
            ticket_file.write_text(
                json.dumps(
                    {
                        "id": "LOCAL-2",
                        "title": "Close action crashes",
                        "body": "It happens before activity is added.",
                        "repo_ref": "fixture@def",
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                ticket_file=ticket_file,
                ticket_id=None,
                title=None,
                body=None,
                repo_ref=None,
                expected_error=None,
            )

            ticket = _ticket_from_args(args, argparse.ArgumentParser())

        self.assertEqual(ticket.id, "LOCAL-2")
        self.assertEqual(ticket.repo_ref, "fixture@def")


if __name__ == "__main__":
    unittest.main()
