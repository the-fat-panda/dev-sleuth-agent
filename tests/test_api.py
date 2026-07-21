from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from bugagent.api import (
    APIConfig,
    APIConfigurationError,
    InvestigationProgressReporter,
    JobRegistry,
    ProgressInvestigationClient,
    ProgressSandbox,
    _continue_yolo_from_jira,
    create_app,
)
from bugagent.artifacts import ArtifactStore
from bugagent.demo import build_demo_bundle
from bugagent.domain import RunBundle, Ticket, VerdictStatus
from bugagent.fix import PublishedPullRequest, PullRequestPlan, write_pull_request_plan
from bugagent.fix_jobs import FixJobRegistry
from bugagent.github import GitHubConfig, GitHubSource
from bugagent.jira import GitHubProjectSource, JiraConfig
from bugagent.publish_jobs import PublicationJobRegistry


class CapturingExecutor:
    """Test double that proves a background handoff was queued without running it."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, function, *args):
        self.calls.append((function, args))


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

    def test_fix_preparation_requires_a_reproduced_bundle_before_any_model_or_git_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = replace(build_demo_bundle(), verdict=replace(build_demo_bundle().verdict, status=VerdictStatus.INCONCLUSIVE))
            ArtifactStore(root / "runs").write(bundle)
            app = create_app(_config(root / "runs"), startup_validator=lambda _: None, investigation_runner=_unused_runner)

            with TestClient(app) as client:
                response = client.post(
                    f"/runs/{bundle.run_id}/fixes",
                    json={
                        "repository": {"kind": "github", "repository": "owner/repo", "ref": "main"},
                        "base_branch": "main",
                    },
                )

        self.assertEqual(response.status_code, 409)
        self.assertIn("REPRODUCED", response.json()["detail"])

    def test_run_endpoints_surface_a_validated_local_fix_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = build_demo_bundle()
            ArtifactStore(root / "runs").write(bundle)
            plan_id = uuid4()
            prepared_plans = root / "prepared-prs"
            prepared_plans.mkdir()
            (prepared_plans / f"{plan_id}.json").write_text(
                json.dumps({"plan_id": str(plan_id), "run_id": str(bundle.run_id)}),
                encoding="utf-8",
            )
            app = create_app(_config(root / "runs"), startup_validator=lambda _: None, investigation_runner=_unused_runner)

            with TestClient(app) as client:
                listed = client.get("/runs")
                stored = client.get(f"/runs/{bundle.run_id}")
                fix_status = client.get(f"/runs/{bundle.run_id}/fix-status")

        expected_fix = {
            "status": "FIX_VALIDATED",
            "label": "FIX VALIDATED",
            "plan_id": str(plan_id),
            "published": False,
        }
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["runs"][0]["fix"], expected_fix)
        self.assertEqual(stored.status_code, 200)
        self.assertEqual(stored.json()["fix"], expected_fix)
        self.assertEqual(fix_status.status_code, 200)
        self.assertEqual(fix_status.json()["status"], "done")
        self.assertEqual(fix_status.json()["plan_id"], str(plan_id))

    def test_run_fix_status_surfaces_an_active_background_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = build_demo_bundle()
            ArtifactStore(root / "runs").write(bundle)
            app = create_app(_config(root / "runs"), startup_validator=lambda _: None, investigation_runner=_unused_runner)
            app.state.fix_jobs.create(str(bundle.run_id))

            with TestClient(app) as client:
                response = client.get(f"/runs/{bundle.run_id}/fix-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "queued")
        self.assertTrue(response.json()["status_url"].startswith("/fixes/"))

    def test_explicit_publication_creates_a_persisted_draft_pr_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = build_demo_bundle()
            ArtifactStore(root / "runs").write(bundle)
            plan = PullRequestPlan(
                plan_id=uuid4(),
                run_id=bundle.run_id,
                repository="owner/repo",
                base_branch="main",
                base_commit="a" * 40,
                head_branch="devsleuth/fix-demo",
                title="fix: demo",
                body="Validated local plan.",
                patch="diff --git a/service.py b/service.py\n",
                regression_path="tests/bugagent_generated/test_demo.py",
                regression_content="def test_demo():\n    assert True\n",
                created_at=datetime.now(timezone.utc),
            )
            write_pull_request_plan(plan, root / "prepared-prs" / f"{plan.plan_id}.json")
            publisher_calls: list[PullRequestPlan] = []

            def fake_publisher(candidate: PullRequestPlan, config: GitHubConfig) -> PublishedPullRequest:
                publisher_calls.append(candidate)
                self.assertTrue(config.publish_enabled)
                return PublishedPullRequest("owner/repo", 42, "https://github.example/pr/42", candidate.head_branch, "b" * 40)

            github = GitHubConfig(frozenset({"owner/repo"}), token="test-token", publish_enabled=True)
            app = create_app(
                _config(root / "runs", github=github),
                startup_validator=lambda _: None,
                investigation_runner=_unused_runner,
                pull_request_publisher=fake_publisher,
            )

            with TestClient(app) as client:
                rejected = client.post(f"/pull-request-plans/{plan.plan_id}/publish", json={"confirm": False})
                self.assertEqual(rejected.status_code, 422)
                accepted = client.post(f"/pull-request-plans/{plan.plan_id}/publish", json={"confirm": True})
                self.assertEqual(accepted.status_code, 202)
                completed = _wait_for_state(client, accepted.json()["status_url"], "done")
                stored_run = client.get(f"/runs/{bundle.run_id}")
                stored_plan = client.get(f"/pull-request-plans/{plan.plan_id}")

        self.assertEqual(publisher_calls, [plan])
        self.assertEqual(completed["publication"]["pull_request"]["url"], "https://github.example/pr/42")
        self.assertEqual(completed["publication"]["jira_comment"]["status"], "not_applicable")
        self.assertEqual(stored_run.json()["fix"]["status"], "DRAFT_PR_OPEN")
        self.assertEqual(stored_plan.json()["publication"]["pull_request"]["number"], 42)

    def test_yolo_mode_requires_publish_access_and_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disabled = create_app(_config(root / "runs"), startup_validator=lambda _: None, investigation_runner=_unused_runner)
            with TestClient(disabled) as client:
                self.assertEqual(client.get("/automation/yolo").json()["available"], False)
                self.assertEqual(client.put("/automation/yolo", json={"enabled": True, "confirm": True}).status_code, 422)

            github = GitHubConfig(frozenset({"owner/repo"}), token="test-token", publish_enabled=True)
            jira = JiraConfig(
                base_url="https://example.atlassian.net",
                email="demo@example.com",
                api_token="jira-token",
                webhook_secret="secret",
                project_sources={
                    "SCRUM": GitHubProjectSource("owner/repo@main", GitHubSource("owner/repo", "main")),
                },
            )
            enabled = create_app(
                _config(root / "runs", github=github, jira=jira),
                startup_validator=lambda _: None,
                investigation_runner=_unused_runner,
            )
            with TestClient(enabled) as client:
                self.assertEqual(client.get("/automation/yolo").json(), {"enabled": False, "available": True})
                self.assertEqual(client.put("/automation/yolo", json={"enabled": True, "confirm": False}).status_code, 422)
                self.assertEqual(client.put("/automation/yolo", json={"enabled": True, "confirm": True}).json()["enabled"], True)
                self.assertEqual(client.put("/automation/yolo", json={"enabled": False}).json()["enabled"], False)

    def test_yolo_handoff_queues_fix_validation_when_mode_was_enabled_at_jira_intake(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = build_demo_bundle()
            ArtifactStore(root / "runs").write(bundle)
            github = GitHubConfig(frozenset({"owner/repo"}), token="test-token", publish_enabled=True)
            source = GitHubProjectSource("owner/repo@main", GitHubSource("owner/repo", "main"))
            jobs = JobRegistry()
            investigation = jobs.create(bundle.ticket, source="jira", issue_key="SCRUM-9")
            fixes = FixJobRegistry()
            publications = PublicationJobRegistry()
            fix_executor = CapturingExecutor()
            publication_executor = CapturingExecutor()

            _continue_yolo_from_jira(
                jobs,
                investigation.job_id,
                bundle,
                "SCRUM-9",
                source,
                _config(root / "runs", github=github),
                fixes,
                publications,
                fix_executor,
                publication_executor,
                lambda _plan, _github: (_ for _ in ()).throw(AssertionError("publisher must not run before validation")),
                True,
            )

        self.assertEqual(len(fix_executor.calls), 1)
        self.assertEqual(len(publication_executor.calls), 0)
        _, events = jobs.wait_for_events(investigation.job_id, 0, timeout_seconds=0)
        self.assertIn(("yolo", "started"), [(event.stage, event.state) for event in events])
        self.assertIn(("yolo", "completed"), [(event.stage, event.state) for event in events])

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


def _config(
    runs_root: Path,
    *,
    github: GitHubConfig | None = None,
    jira: JiraConfig | None = None,
) -> APIConfig:
    return APIConfig(
        model="test-model",
        sandbox_image="sha256:" + "a" * 64,
        max_attempts=3,
        sandbox_timeout_seconds=30,
        runs_root=runs_root,
        github=github,
        jira=jira,
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
