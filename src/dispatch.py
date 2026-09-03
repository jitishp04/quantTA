"""Trigger a GitHub Actions workflow from a Discord command.

This is what makes `!scan` and `!study NVDA` work: the sync job is already
running inside GitHub Actions on its own cron, and Actions issues every job a
`GITHUB_TOKEN` for the duration of the run. Given `actions: write` in that
job's `permissions:` block, the same token can call the REST API to start a
DIFFERENT workflow (`scan.yml`, `study.yml`) via `workflow_dispatch`. No extra
secret, no personal access token -- the token already in every run is enough.

This only works inside Actions. A local `python sync.py` has no GITHUB_TOKEN
and `available` is False, so a local run reports the commands as unavailable
rather than raising.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
TIMEOUT = 15


class GitHubDispatcher:
    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        ref: str | None = None,
    ):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        # GITHUB_REPOSITORY ("owner/repo") and GITHUB_REF_NAME are set
        # automatically for every step of every Actions run -- nothing to
        # configure. They are simply absent locally.
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.ref = ref or os.environ.get("GITHUB_REF_NAME", "main")
        self.session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.token and self.repo)

    def dispatch(self, workflow_file: str, inputs: dict[str, str] | None = None) -> None:
        """POST a workflow_dispatch event. Raises with a specific hint on 404."""
        if not self.available:
            raise RuntimeError(
                "GITHUB_TOKEN/GITHUB_REPOSITORY not set -- dispatch only "
                "works when running inside a GitHub Actions job"
            )

        url = f"{API_ROOT}/repos/{self.repo}/actions/workflows/{workflow_file}/dispatches"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # workflow_dispatch inputs must all be strings over the REST API;
        # GitHub coerces them to the declared type (boolean, choice, ...).
        payload = {"ref": self.ref, "inputs": {k: str(v) for k, v in (inputs or {}).items()}}

        response = self.session.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        if response.status_code == 404:
            raise RuntimeError(
                f"{workflow_file} dispatch returned 404 -- the job's "
                "GITHUB_TOKEN needs 'actions: write' in its permissions: "
                "block, and the target workflow needs a workflow_dispatch "
                "trigger"
            )
        response.raise_for_status()
