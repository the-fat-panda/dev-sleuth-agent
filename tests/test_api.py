from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from bugagent.api import (
    APIConfig,
    APIConfigurationError,
    InvestigationProgressReporter,
    JobRegistry,
    ProgressInvestigationClient,
    ProgressSandbox,
    create_app,
)
from bugagent.artifacts import ArtifactStore
from bugagent.demo import build_demo_bundle
from bugagent.domain import RunBundle, Ticket


class BlockingBundleWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, ticket: Ticket, repo_root: Path, repo_commit: str) -> RunBundle:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("Test job was not released.")
        bundle = replace(build_demo_bundle(), ticket=ticket, repo_commit=repo_commit)
        ArtifactStore(self.root).write(bundle)
        return bundle


class ApiTests(unittest.TestCase):
    def test_background_job_moves_from_running_to_done_and_exposes_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            writer = BlockingBundleWriter(root / "runs")
            app = create_app(_config(root / "runs"), startup_validator=lambda _: None, investigation_runner=writer)

            with TestClient(app) as client:
                response = client.post("/investigations", json=_request(repository))
                self.assertEqual(response.status_code, 202)
                accepted = response.json()
                self.assertEqual(accepted["status"], "queued")
                self.assertEqual(accepted["source"], "manual")
                self.assertEqual(accepted["ticket"]["id"], "API-1")
                self.assertTrue(writer.started.wait(timeout=1))

                active = client.get("/investigations")
                self.assertEqual(active.status_code, 200)
                self.assertEqual(active.json()["jobs"][0]["job_id"], accepted["job_id"])
                self.assertEqual(active.json()["jobs"][0]["ticket"]["title"], "Close action crashes")

                running = client.get(accepted["status_url"])
                self.assertEqual(running.status_code, 200)
                self.assertEqual(running.json()["status"], "running")

                writer.release.set()
                completed = _wait_for_done(client, accepted["status_url"])

                self.assertEqual(completed["status"], "done")
                self.assertEqual(completed["verdict"]["status"], "REPRODUCED")
                self.assertEqual(completed["verdict"]["score"], 100)
                run_id = completed["run_id"]

                listed = client.get("/runs")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual([item["run_id"] for item in listed.json()["runs"]], [run_id])

                stored = client.get(f"/runs/{run_id}")
                self.assertEqual(stored.status_code, 200)
                self.assertEqual(stored.json()["ticket"]["id"], "API-1")

                cleared = client.delete("/runs")
                self.assertEqual(cleared.status_code, 200)
                self.assertEqual(cleared.json(), {"deleted_run_count": 1})
                self.assertEqual(client.get("/runs").json(), {"runs": []})
                self.assertEqual(client.get(f"/runs/{run_id}").status_code, 404)

                stream = client.get(accepted["events_url"])
                self.assertEqual(stream.status_code, 200)
                self.assertIn("event: progress", stream.text)
                self.assertIn('"stage":"job","state":"queued"', stream.text)
                self.assertIn('"stage":"job","state":"running"', stream.text)
                self.assertIn('"stage":"verdict","state":"completed"', stream.text)

                home = client.get("/")
                self.assertEqual(home.status_code, 200)
                self.assertIn("Investigation workspace", home.text)

                jira_status = client.get("/integrations/jira/status")
                self.assertEqual(jira_status.status_code, 200)
                self.assertEqual(jira_status.json(), {"configured": False})

                disabled_webhook = client.post("/integrations/jira/webhook", content=b"{}")
                self.assertEqual(disabled_webhook.status_code, 503)

    def test_worker_failure_is_available_from_the_job_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()

            def failing_runner(ticket: Ticket, repo_root: Path, repo_commit: str) -> RunBundle:
                raise RuntimeError("simulated model outage")

            app = create_app(_config(root / "runs"), startup_validator=lambda _: None, investigation_runner=failing_runner)
            with TestClient(app) as client:
                accepted = client.post("/investigations", json=_request(repository)).json()
                failed = _wait_for_state(client, accepted["status_url"], "failed")

            self.assertIn("simulated model outage", failed["error"])

    def test_rejects_nonexistent_local_repository_before_creating_a_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(_config(Path(directory) / "runs"), startup_validator=lambda _: None, investigation_runner=_unused_runner)
            with TestClient(app) as client:
                response = client.post("/investigations", json=_request(Path(directory) / "missing"))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "repository.path must name an existing local directory.")

    def test_config_requires_an_image_and_valid_positive_values(self) -> None:
        with self.assertRaisesRegex(APIConfigurationError, "BUGAGENT_SANDBOX_IMAGE"):
            APIConfig.from_environment({})
        with self.assertRaisesRegex(APIConfigurationError, "BUGAGENT_MAX_ATTEMPTS"):
            APIConfig.from_environment(
                {
                    "BUGAGENT_SANDBOX_IMAGE": "sha256:" + "a" * 64,
                    "BUGAGENT_MAX_ATTEMPTS": "0",
                }
            )

    def test_progress_adapters_emit_hypothesis_sandbox_replay_and_verdict_events(self) -> None:
        registry = JobRegistry()
        job = registry.create()
        progress = InvestigationProgressReporter(registry, job.job_id)
        ticket = Ticket("API-2", "Close action crashes", "It crashes before activity is added.", "fixture@abc")
        client = ProgressInvestigationClient(_CandidateClient(), progress)
        sandbox = ProgressSandbox(_Sandbox(), progress)

        client.propose(ticket, object(), ())
        sandbox.run(Path("."), Path("tests/bugagent_generated/test_close.py"))
        sandbox.run(Path("."), Path("tests/bugagent_generated/test_close.py"))
        sandbox.run(Path("."), Path("tests/bugagent_generated/test_close.py"))

        _, events = registry.wait_for_events(job.job_id, 0, timeout_seconds=0)
        stages = [(event.stage, event.state) for event in events]

        self.assertIn(("form_hypothesis", "started"), stages)
        self.assertIn(("form_hypothesis", "completed"), stages)
        self.assertIn(("candidate_sandbox", "started"), stages)
        self.assertIn(("replay_1", "completed"), stages)
        self.assertIn(("replay_2", "completed"), stages)
        self.assertIn(("verdict", "started"), stages)

    def test_job_registry_preserves_jira_origin_for_activity_view(self) -> None:
        registry = JobRegistry()
        ticket = Ticket("SCRUM-5", "Checkout total is wrong", "A customer report", "demo@main")
        job = registry.create(
            ticket,
            source="jira",
            issue_key="SCRUM-5",
            issue_url="https://example.atlassian.net/browse/SCRUM-5",
        )

        observed = registry.list_recent(25)

        self.assertEqual(observed, (job,))
        self.assertEqual(observed[0].source, "jira")
        self.assertEqual(observed[0].issue_key, "SCRUM-5")


def _config(runs_root: Path) -> APIConfig:
    return APIConfig(
        model="test-model",
        sandbox_image="sha256:" + "a" * 64,
        max_attempts=3,
        sandbox_timeout_seconds=30,
        runs_root=runs_root,
    )


def _request(repository: Path) -> dict[str, object]:
    return {
        "ticket": {
            "id": "API-1",
            "title": "Close action crashes",
            "body": "The normal close action crashes before activity is added.",
            "repo_ref": "fixture@abc",
        },
        "repository": {"kind": "local_path", "path": str(repository), "commit": "fixture"},
    }


def _wait_for_done(client: TestClient, url: str) -> dict[str, object]:
    return _wait_for_state(client, url, "done")


def _wait_for_state(client: TestClient, url: str, expected: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(url)
        payload = response.json()
        if payload["status"] == expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"Job did not reach {expected} state before deadline.")


def _unused_runner(ticket: Ticket, repo_root: Path, repo_commit: str) -> RunBundle:
    raise AssertionError("Nonexistent repository should be rejected before a worker is submitted.")


class _CandidateClient:
    def propose(self, ticket: Ticket, repository: object, prior_feedback: tuple[str, ...]) -> object:
        return object()


class _Sandbox:
    def run(self, repo_root: Path, candidate_path: Path) -> object:
        return object()


if __name__ == "__main__":
    unittest.main()
