from __future__ import annotations

import unittest

from bugagent.fix_jobs import FixJobRegistry, FixJobState


class FixJobRegistryTests(unittest.TestCase):
    def test_job_retains_stage_progress_through_a_validated_plan(self) -> None:
        registry = FixJobRegistry()
        job = registry.create("run-1")
        registry.mark_running(job.job_id)
        registry.emit(job.job_id, "repository_checkout", "started", "Cloning reproduced source")
        registry.emit(job.job_id, "repository_checkout", "completed", "Pinned reproduced commit")
        registry.mark_done(job.job_id, plan_id="plan-1", plan_path="C:/plans/plan-1.json")

        finished = registry.get(job.job_id)
        assert finished is not None
        self.assertEqual(finished.status, FixJobState.DONE)
        self.assertEqual(finished.current_stage, "pr_plan")
        self.assertEqual(finished.current_state, "completed")
        self.assertEqual(finished.plan_id, "plan-1")
        self.assertEqual([event.stage for event in finished.events], ["queued", "job", "repository_checkout", "repository_checkout", "pr_plan"])


if __name__ == "__main__":
    unittest.main()
