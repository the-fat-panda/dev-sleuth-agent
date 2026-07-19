from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from bugagent.api_repositories import GitHubRepositorySource, validate_submission_source
from bugagent.github import GitHubCheckoutError, GitHubConfig, GitHubConfigurationError, GitHubSource, checkout
from bugagent.jira import GitHubProjectSource
from bugagent.api_repositories import source_from_jira


class GitHubTests(unittest.TestCase):
    def test_configuration_requires_an_explicit_allow_list(self) -> None:
        self.assertIsNone(GitHubConfig.from_environment({}))
        with self.assertRaisesRegex(GitHubConfigurationError, "ALLOWED_REPOSITORIES"):
            GitHubConfig.from_environment({"BUGAGENT_GITHUB_TOKEN": "secret"})

    def test_source_is_limited_to_the_configured_repository(self) -> None:
        config = GitHubConfig.from_environment({"BUGAGENT_GITHUB_ALLOWED_REPOSITORIES": "the-fat-panda/e-commerce"})
        assert config is not None
        validate_submission_source(GitHubRepositorySource(repository="the-fat-panda/e-commerce", ref="backend-main"), config)
        with self.assertRaisesRegex(GitHubCheckoutError, "not in BUGAGENT"):
            validate_submission_source(GitHubRepositorySource(repository="other/example", ref="main"), config)

    def test_checkout_resolves_a_full_sha_without_exposing_a_token_in_command(self) -> None:
        config = GitHubConfig.from_environment(
            {
                "BUGAGENT_GITHUB_ALLOWED_REPOSITORIES": "the-fat-panda/e-commerce",
                "BUGAGENT_GITHUB_TOKEN": "private-token",
            }
        )
        assert config is not None
        sha = "a" * 40
        completed = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, f"{sha}\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with patch("bugagent.github.subprocess.run", side_effect=completed) as run:
            with checkout(config, GitHubSource("the-fat-panda/e-commerce", "backend-main")) as resolved:
                self.assertEqual(resolved.commit, sha)
                self.assertIsInstance(resolved.root, Path)

        clone_command = run.call_args_list[0].args[0]
        self.assertNotIn("private-token", " ".join(clone_command))
        clone_environment = run.call_args_list[0].kwargs["env"]
        self.assertNotIn("private-token", clone_environment["GIT_CONFIG_VALUE_0"])

    def test_jira_github_mapping_becomes_the_api_source(self) -> None:
        project_source = GitHubProjectSource(
            repo_ref="the-fat-panda/e-commerce@backend-main",
            source=GitHubSource("the-fat-panda/e-commerce", "backend-main"),
        )
        source = source_from_jira(project_source)
        self.assertEqual(source.repository, "the-fat-panda/e-commerce")
        self.assertEqual(source.ref, "backend-main")


if __name__ == "__main__":
    unittest.main()
