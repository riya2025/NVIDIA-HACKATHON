"""Create a public GitHub repo for a generated app and push it (real git).

This is the source the Render API path deploys from: Render clones the public
repo and builds `backend/Dockerfile`. Everything here is real — it talks to the
GitHub REST API and shells out to the `git` CLI. Best-effort: any failure logs
and returns None so the Deployment agent can fall back to the local preview.

Requires:
  - settings.github_token : a PAT with repo scope (classic) or
    contents+administration (fine-grained).
  - the `git` CLI on PATH.
"""
from __future__ import annotations

import errno
import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx

from .config import settings
from .local_deploy import APPS_ROOT, _slug
from .logging_config import log

_GH_API = "https://api.github.com"
_BRANCH = "main"

_log = log.bind(agent="deployment")


def _is_too_many_open_files(exc: BaseException) -> bool:
    """True for the transient OS 'too many open files' (Errno 24) — on Windows
    this often surfaces via an antivirus file-filter (e.g. avgMonFltProxy) and
    clears after a short backoff."""
    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, OSError) and getattr(e, "errno", None) == errno.EMFILE:
            return True
        if "too many open files" in str(e).lower():
            return True
        e = e.__cause__ or e.__context__
    return False


def _http_client(timeout: float = 20.0) -> httpx.Client:
    """httpx client honouring the insecure-SSL flag (SSL-intercepting networks)."""
    return httpx.Client(verify=not settings.tls_verify_off, timeout=timeout)


def github_configured() -> bool:
    """True when a GitHub token is set AND the git CLI is available."""
    return bool(settings.github_token) and shutil.which("git") is not None


def repo_name(project) -> str:
    """Deterministic repo name per project so re-runs reuse the same repo."""
    suffix = hashlib.sha1(str(project.id).encode()).hexdigest()[:6]
    base = _slug(project.name)[:90].strip("-") or "app"
    return f"{base}-{suffix}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _authenticated_login() -> Optional[str]:
    try:
        with _http_client(20.0) as client:
            r = client.get(f"{_GH_API}/user", headers=_headers())
        if r.status_code == 200:
            return r.json().get("login")
        _log.warning("GitHub /user failed: {} {}", r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        if _is_too_many_open_files(exc):
            raise  # transient: let create_and_push_repo back off + retry
        _log.warning("GitHub /user error: {}", exc)
    return None


def _ensure_remote_repo(name: str, description: str) -> Optional[str]:
    """Create a public repo (idempotent). Returns the clone-less html owner/repo
    'full_name' (e.g. 'octocat/my-app') or None on hard failure."""
    login = _authenticated_login()
    if not login:
        return None
    # If it already exists, reuse it.
    try:
        with _http_client(20.0) as client:
            existing = client.get(
                f"{_GH_API}/repos/{login}/{name}", headers=_headers()
            )
        if existing.status_code == 200:
            return existing.json().get("full_name", f"{login}/{name}")
    except Exception as exc:  # noqa: BLE001
        if _is_too_many_open_files(exc):
            raise
        _log.warning("GitHub repo lookup error: {}", exc)

    payload = {
        "name": name,
        "description": description[:300],
        "private": False,
        "auto_init": False,
        "has_issues": False,
        "has_wiki": False,
        "has_projects": False,
    }
    try:
        with _http_client(30.0) as client:
            r = client.post(
                f"{_GH_API}/user/repos", headers=_headers(), json=payload
            )
        if r.status_code in (200, 201):
            return r.json().get("full_name", f"{login}/{name}")
        if r.status_code == 422:  # already exists (race) — reuse
            return f"{login}/{name}"
        _log.warning("GitHub create repo failed: {} {}", r.status_code, r.text[:300])
    except Exception as exc:  # noqa: BLE001
        if _is_too_many_open_files(exc):
            raise
        _log.warning("GitHub create repo error: {}", exc)
    return None


def _git(app_dir: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    import os

    git_env = {**os.environ}
    if settings.tls_verify_off:
        # git uses its own CA bundle; skip verification on SSL-intercepting nets.
        git_env["GIT_SSL_NO_VERIFY"] = "true"
    return subprocess.run(
        ["git", "-C", str(app_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        env=git_env,
    )


def _push(app_dir: Path, full_name: str) -> bool:
    """Initialise (if needed) and force-push the app dir to the repo's main
    branch using the token in the remote URL. Force so re-deploys overwrite."""
    token = settings.github_token
    remote = f"https://x-access-token:{token}@github.com/{full_name}.git"
    name = settings.git_author_name
    email = settings.git_author_email
    try:
        if not (app_dir / ".git").exists():
            if _git(app_dir, "init").returncode != 0:
                return False
        # Use the main branch (rename whatever the default is).
        _git(app_dir, "checkout", "-B", _BRANCH)
        _git(app_dir, "add", "-A")
        commit = _git(
            app_dir,
            "-c", f"user.name={name}",
            "-c", f"user.email={email}",
            "commit", "-m", "Deploy via AI Foundry",
        )
        # An empty commit (nothing changed since last push) is fine to ignore.
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
            _log.warning("git commit failed: {}", (commit.stderr or commit.stdout)[:300])
            # Continue anyway; there may be an existing commit to push.
        # Point origin at the authenticated remote.
        _git(app_dir, "remote", "remove", "origin")
        if _git(app_dir, "remote", "add", "origin", remote).returncode != 0:
            return False
        push = _git(app_dir, "push", "-u", "--force", "origin", _BRANCH, timeout=300)
        if push.returncode != 0:
            out = (push.stderr or push.stdout) or ""
            if "too many open files" in out.lower():
                # Transient OS/AV file-handle exhaustion — signal a retry.
                raise OSError(errno.EMFILE, "git push: too many open files")
            _log.warning("git push failed: {}", out[:400])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_too_many_open_files(exc):
            raise  # transient: let create_and_push_repo back off + retry
        _log.warning("git push error: {}", exc)
        return False


def _owner_repo_from_url(repo_url: str) -> Optional[Tuple[str, str]]:
    """Parse 'https://github.com/owner/repo(.git)' -> ('owner', 'repo')."""
    if not repo_url or "github.com/" not in repo_url:
        return None
    tail = repo_url.rstrip("/").split("github.com/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    parts = [p for p in tail.split("/") if p]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def latest_workflow_run(repo_url: str) -> Optional[dict]:
    """Return the most recent GitHub Actions workflow run for a repo, or None.

    Used by the monitoring watchdog to detect CI failures (which live on GitHub,
    not on the deployed service, so a runtime health check can't see them).
    Best-effort: any error returns None.
    """
    if not github_configured() or not repo_url:
        return None
    parsed = _owner_repo_from_url(repo_url)
    if not parsed:
        return None
    owner, repo = parsed
    try:
        with _http_client(15.0) as client:
            r = client.get(
                f"{_GH_API}/repos/{owner}/{repo}/actions/runs",
                headers=_headers(),
                params={"per_page": 1},
            )
        if r.status_code != 200:
            return None
        runs = r.json().get("workflow_runs") or []
        if not runs:
            return None
        run = runs[0]
        return {
            "id": run.get("id"),
            "name": run.get("name") or run.get("display_title"),
            "status": run.get("status"),          # queued | in_progress | completed
            "conclusion": run.get("conclusion"),  # success | failure | cancelled | ...
            "html_url": run.get("html_url"),
            "branch": run.get("head_branch"),
            "event": run.get("event"),
        }
    except Exception as exc:  # noqa: BLE001
        _log.warning("GitHub actions runs error: {}", exc)
        return None


def _create_and_push_once(project) -> Optional[Tuple[str, str]]:
    app_dir = APPS_ROOT / _slug(project.name)
    if not app_dir.exists():
        return None
    name = repo_name(project)
    full_name = _ensure_remote_repo(name, project.description or project.name)
    if not full_name:
        return None
    if not _push(app_dir, full_name):
        return None
    return f"https://github.com/{full_name}", _BRANCH


def create_and_push_repo(project) -> Optional[Tuple[str, str]]:
    """Create a public GitHub repo for the project and push the generated app.

    Retries on a transient OS "too many open files" (Errno 24) with a short
    backoff, since that error — frequently caused by an antivirus file-filter on
    Windows — usually clears once handles are released. This is the prerequisite
    for the Render backend deploy, so making it resilient directly improves the
    cloud-deploy success rate.

    Returns (html_url, branch) e.g. ("https://github.com/octocat/my-app", "main")
    or None if anything failed.
    """
    if not github_configured():
        return None

    attempts = max(1, int(settings.github_push_retries))
    for attempt in range(1, attempts + 1):
        try:
            result = _create_and_push_once(project)
            if result:
                if attempt > 1:
                    _log.info("GitHub push succeeded on retry {}/{}", attempt, attempts)
                return result
        except Exception as exc:  # noqa: BLE001
            if not _is_too_many_open_files(exc) or attempt == attempts:
                _log.warning("GitHub push error (attempt {}/{}): {}", attempt, attempts, exc)
                return None
            _log.warning(
                "GitHub push hit 'too many open files' (attempt {}/{}); backing off "
                "to let the OS release handles. If this persists, add an antivirus "
                "exclusion for this project folder / git.",
                attempt,
                attempts,
            )
        if attempt < attempts:
            time.sleep(2.0 * attempt)
    return None
