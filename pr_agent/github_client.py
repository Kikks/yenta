"""Thin wrapper around PyGithub.

We isolate every GitHub call behind this class so:
  - the agent nodes are easy to unit-test by mocking one object
  - rate-limit / pagination concerns live in one place
  - swapping to a different client (httpx + REST, GraphQL) later is local

Why PyGithub: handles pagination + auth + retries out of the box and is
the boring, well-known choice. For this size project, the extra
abstraction overhead of GraphQL isn't worth the speed gain.
"""
from __future__ import annotations

import base64
import logging
from collections import Counter
from typing import Iterable, Optional

from github import Auth, Github
from github.GithubException import GithubException, UnknownObjectException
from github.PullRequest import PullRequest
from github.Repository import Repository

from .state import FileChange, PRMeta

log = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._gh = Github(auth=Auth.Token(token), per_page=100)

    # ---------- low-level handles ----------

    def repo(self, owner: str, name: str) -> Repository:
        return self._gh.get_repo(f"{owner}/{name}")

    def pull(self, owner: str, name: str, number: int) -> PullRequest:
        return self.repo(owner, name).get_pull(number)

    @property
    def viewer_login(self) -> str:
        return self._gh.get_user().login

    # ---------- composite reads used by the fetch node ----------

    def load_pr_meta(self, owner: str, name: str, number: int) -> tuple[PRMeta, PullRequest]:
        pr = self.pull(owner, name, number)
        meta = PRMeta(
            owner=owner,
            repo=name,
            number=number,
            title=pr.title,
            body=pr.body,
            author=pr.user.login if pr.user else "unknown",
            base_ref=pr.base.ref,
            head_ref=pr.head.ref,
            is_fork=bool(pr.head.repo and pr.head.repo.full_name != f"{owner}/{name}"),
            additions=pr.additions,
            deletions=pr.deletions,
            changed_files=pr.changed_files,
            url=pr.html_url,
        )
        return meta, pr

    def load_changed_files(self, pr: PullRequest) -> list[FileChange]:
        """Paginated fetch of changed files. PyGithub paginates lazily so
        this works for large PRs without loading everything in memory."""
        out: list[FileChange] = []
        for f in pr.get_files():
            out.append(
                FileChange(
                    path=f.filename,
                    status=f.status,
                    additions=f.additions,
                    deletions=f.deletions,
                    patch=f.patch,  # may be None for binary or huge files
                )
            )
        return out

    def load_codeowners(self, owner: str, name: str, ref: str) -> Optional[str]:
        """CODEOWNERS lives in one of three canonical locations. Try each."""
        repo = self.repo(owner, name)
        for path in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
            try:
                f = repo.get_contents(path, ref=ref)
                # get_contents returns a single ContentFile here, but the
                # type hint is broader; guard against the list case.
                if isinstance(f, list):
                    continue
                return base64.b64decode(f.content).decode("utf-8", errors="replace")
            except (UnknownObjectException, GithubException):
                continue
        return None

    def recent_committers_for_paths(
        self,
        owner: str,
        name: str,
        paths: Iterable[str],
        *,
        per_path_limit: int = 5,
    ) -> dict[str, list[str]]:
        """For each path, return the recent committers (most recent first).

        We hit the commits-by-path endpoint with `per_page=per_path_limit`.
        On large monorepos this can be expensive — only call this for the
        paths we actually need a reviewer signal on.
        """
        out: dict[str, list[str]] = {}
        repo = self.repo(owner, name)
        for path in paths:
            seen: list[str] = []
            try:
                commits = repo.get_commits(path=path)
                # Don't slice PyGithub's PaginatedList — slicing past the
                # underlying length raises IndexError (not StopIteration)
                # for paths with very short history (e.g. files newly
                # added in *this* PR). Iterate directly and break early.
                for i, c in enumerate(commits):
                    if i >= per_path_limit:
                        break
                    if c.author and c.author.login and c.author.login not in seen:
                        seen.append(c.author.login)
            except (GithubException, IndexError, StopIteration) as e:
                # IndexError catches the PyGithub paginated-list edge case
                # described above; GithubException catches 404s on paths
                # that don't exist on the base ref (e.g. file moves).
                log.warning("blame lookup failed for %s: %s", path, e)
            out[path] = seen
        return out

    # ---------- writes (used by Phase 4) ----------

    def post_review(
        self,
        pr: PullRequest,
        *,
        body: str,
        event: str,
        comments: list[dict] | None = None,
    ):
        """Post a review with line-level comments in a single API call.

        `comments` shape per GitHub REST:
          {"path": str, "line": int, "body": str, "side": "RIGHT"}
        """
        return pr.create_review(body=body, event=event, comments=comments or [])

    def request_reviewers(self, pr: PullRequest, logins: list[str]) -> None:
        if not logins:
            return
        try:
            pr.create_review_request(reviewers=logins)
        except GithubException as e:
            # PRs from forks can't request reviewers from a non-collaborator;
            # also you can't request a review from the PR author.
            log.warning("request_reviewers failed for %s: %s", logins, e)

    def post_issue_comment(self, pr: PullRequest, body: str):
        return pr.create_issue_comment(body)


def collapse_committers(per_path: dict[str, list[str]], *, top_n: int = 3) -> list[str]:
    """Reduce a {path: [logins]} dict to the top-N most frequent committers,
    weighted toward recency (first-in-list gets a boost).

    Simple weighted vote; defensible and stable. The full distribution is
    still in `per_path` for traceability in the comment we leave.
    """
    score: Counter[str] = Counter()
    for logins in per_path.values():
        for i, login in enumerate(logins):
            score[login] += max(1, 5 - i)  # 5,4,3,2,1 — recency boost
    return [login for login, _ in score.most_common(top_n)]
