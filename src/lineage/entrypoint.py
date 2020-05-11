#!/usr/bin/env python3
"""GitHub Action to create pull requests for upstream changes."""

# Standard Python Libraries
import logging
import os
from pathlib import Path
import subprocess  # nosec
import sys
from typing import Generator, List, Optional, Tuple
from urllib.parse import ParseResult, urlparse

# Third-Party Libraries
from github import Github, Repository
import pkg_resources
import pystache
import requests
import yaml

# Constants
ALREADY_UP_TO_DATE = "Already up to date."
CONFIG_FILENAME = ".github/lineage.yml"
GIT = "/usr/bin/git"
PR_METADATA = 'lineage:metadata:{"slayed":true}'
UNRELATED_HISTORY = "fatal: refusing to merge unrelated histories"


def run(
    cmd: List[str],
    cwd: Optional[str] = None,
    comment: Optional[str] = None,
    error_ok: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command and display its output and return code."""
    if comment:
        logging.info(f"💬 {comment}")
    logging.debug(f"➤  {cmd}")
    proc = subprocess.run(
        cmd,
        shell=False,  # nosec
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.returncode == 0:
        logging.debug(proc.stdout.decode())
        logging.info("✅ success")
    elif error_ok:
        logging.debug(proc.stdout.decode())
        logging.info(f"✅ (error ok) return code: {proc.returncode}")
    else:
        logging.warning(proc.stdout.decode())
        logging.warning(f"❌ ERROR! return code: {proc.returncode}")
    return proc


def load_template(github_workspace_dir, default_filename, custom_filename=None):
    """Load specified template file or local default."""
    template_data: str
    if custom_filename:
        template_path: Path = Path(github_workspace_dir) / Path(custom_filename)
        logging.info(f"Loading template file: {template_path}")
        with template_path.open() as f:
            template_data: str = f.read()
    else:
        logging.info(f"Loading default template: {default_filename}")
        template_data = (
            pkg_resources.resource_string("lineage", f"templates/{default_filename}")
            .decode("utf-8")
            .strip()
        )
    return template_data


def get_repo_list(
    g: Github, repo_query: str
) -> Generator[Repository.Repository, None, None]:
    """Generate a list of repositories based on the query."""
    logging.info(f"Querying for repositories: {repo_query}")
    matching_repos = g.search_repositories(query=repo_query)
    for repo in matching_repos:
        yield repo


def get_config(repo) -> Optional[dict]:
    """Read the lineage configuration for this repo without checking it out."""
    config_url: str = f"https://raw.githubusercontent.com/{repo.full_name}/{repo.default_branch}/{CONFIG_FILENAME}"
    logging.debug(f"Checking for config at: {config_url}")
    response = requests.get(config_url)
    if response.status_code == 200:
        return yaml.safe_load(response.content)
    else:
        return None


def switch_branch(
    repo, lineage_id: str, local_branch: Optional[str]
) -> Tuple[str, bool]:
    """Switch to the PR branch, and possibly create it."""
    branch_name = f"lineage/{lineage_id}"
    logging.info(f"Attempting to switch to branch: {branch_name}")
    proc = run([GIT, "switch", branch_name], cwd=repo.full_name, error_ok=True)
    if proc.returncode:
        if not local_branch:
            local_branch = repo.default_branch
        # branch does not exist, create it
        logging.info(
            f"Branch did not exist.  Creating: {branch_name} from local {local_branch}"
        )
        logging.info(f"Creating branch {branch_name} from {local_branch}")
        # ignore typing on local_branch, it is has been set above if None
        run([GIT, "branch", branch_name, local_branch], cwd=repo.full_name)  # type: ignore
        logging.info(f"Switching to {branch_name}")
        run([GIT, "switch", branch_name], cwd=repo.full_name)
        return branch_name, True  # branch is new
    else:
        return branch_name, False  # branch existed


def fetch(repo, remote_url, remote_branch):
    """Fetch commits from remote branch."""
    if remote_branch:
        logging.info(f"Fetching {remote_url} {remote_branch}")
        run([GIT, "fetch", remote_url, remote_branch], cwd=repo.full_name)
    else:
        logging.info(f"Fetching {remote_url}")
        run([GIT, "fetch", remote_url], cwd=repo.full_name)


def merge(repo) -> Tuple[bool, List[str]]:
    """Merge previously fetched commits."""
    conflict_file_list: List[str] = []
    logging.info(f"Attempting merge of fetched changes.")
    proc = run(
        [GIT, "merge", "--no-commit", "FETCH_HEAD"], cwd=repo.full_name, error_ok=True
    )
    if UNRELATED_HISTORY in proc.stdout.decode():
        logging.warning("Repository lineage is incorrect.  Merge refused.")
        return False, conflict_file_list
    if ALREADY_UP_TO_DATE in proc.stdout.decode():
        logging.info("Branch is already up to date.")
        return False, conflict_file_list
    conflict: bool = proc.returncode != 0
    if conflict:
        logging.info("Conflict detected during merge.  Collecting conflicted files.")
        proc = run([GIT, "diff", "--name-only", "--diff-filter=U"], cwd=repo.full_name)
        conflict_file_list = proc.stdout.decode().split()
        logging.info("Adding conflicts into branch for later resolution.")
        run([GIT, "add", "."], cwd=repo.full_name)
    logging.info("Committing merge.")
    run([GIT, "commit", "--file=.git/MERGE_MSG"], cwd=repo.full_name)
    return True, conflict_file_list


def push(repo, branch_name, username, password):
    """Push changes to remote."""
    parts: ParseResult = urlparse(repo.clone_url)
    cred_url = parts._replace(netloc=f"{username}:{password}@{parts.netloc}").geturl()
    logging.info("Assigning credentials for push.")
    run([GIT, "remote", "set-url", "origin", cred_url], cwd=repo.full_name)
    logging.info(f"Pushing {branch_name} to remote.")
    run([GIT, "push", "--set-upstream", "origin", branch_name], cwd=repo.full_name)


def create_pull_request(
    repo,
    pr_branch_name: str,
    local_branch: Optional[str],
    title: str,
    body: str,
    draft: bool,
):
    """Create a pull request."""
    logging.info(f"Creating a new pull request in {repo.full_name}")
    if not local_branch:
        local_branch = repo.default_branch
    repo.create_pull(
        title=title, head=pr_branch_name, base=local_branch, body=body, draft=draft
    )


def main() -> int:
    """Parse evironment and perform requested actions."""
    # Set up logging
    logging.basicConfig(format="%(levelname)s %(message)s", level="INFO")

    # Get inputs from the environment
    access_token: Optional[str] = os.environ.get("INPUT_ACCESS_TOKEN")
    github_workspace_dir: Optional[str] = os.environ.get("GITHUB_WORKSPACE")
    repo_query: Optional[str] = os.environ.get("INPUT_REPO_QUERY")

    # sanity checks
    if access_token is None:
        logging.fatal(
            "Access token environment variable must be set. (INPUT_ACCESS_TOKEN)"
        )
        return -1

    if github_workspace_dir is None:
        logging.fatal(
            "GitHub workspace environment variable must be set. (GITHUB_WORKSPACE)"
        )
        return -1

    if repo_query is None:
        logging.fatal(
            "Reository query environment variable must be set. (INPUT_REPO_QUERY)"
        )
        return -1

    # Ensure we are working in our workspace
    os.chdir(github_workspace_dir)

    # Create a Github access object
    g = Github(access_token)

    # Set up a session to do things the Github library has not yet implemented.
    session: requests.Session = requests.Session()
    session.auth = ("", access_token)

    clean_template = load_template(github_workspace_dir, "clean_template.md", None)
    conflict_template = load_template(
        github_workspace_dir, "conflict_template.md", None
    )

    repos = get_repo_list(g, repo_query)
    for repo in repos:
        logging.info(f"Checking: {repo.full_name}")
        config = get_config(repo)
        if not config:
            logging.info(f"Lineage configuration not found for {repo.full_name}")
            continue
        logging.info(f"Lineage configuration found for {repo.full_name}")
        logging.info(f"Cloning repository: {repo.clone_url}")
        run([GIT, "clone", repo.clone_url, repo.full_name])
        lineage_id: str
        local_branch: str
        remote_branch: Optional[str]
        remote_url: str
        if config.get("version") != "1":
            logging.warning(f"Incompatible config version: {config['version']}")
            continue
        if "lineage" not in config:
            logging.warning("Could not find 'lineage' key in config.")
            continue
        for lineage_id, v in config["lineage"].items():
            logging.info(f"Processing lineage: {lineage_id}")
            try:
                local_branch = v.get("local-branch")
                remote_branch = None  # v.get("remote-branch")
                remote_url = v["remote-url"]
            except KeyError as e:
                logging.warning(f"Missing config key while reading {lineage_id}: {e}")
                continue
            logging.info(f"Upstream: {remote_url} {remote_branch or ''}")
            # Check to see if a PR branch already exists
            branch_is_new: bool
            branch_name: str
            pr_branch_name, branch_is_new = switch_branch(
                repo, lineage_id, local_branch
            )
            logging.info(f"Pull request branch is new: {branch_is_new}")
            fetch(repo, remote_url, remote_branch)
            changed: bool
            conflict_file_list: List[str]
            changed, conflict_file_list = merge(repo)
            if not changed:
                logging.info(
                    f"Already up to date with: {remote_url} {remote_branch or ''} "
                )
                continue
            push(repo, pr_branch_name, "git", access_token)

            if branch_is_new:
                logging.info("Creating a new pull request since branch is new.")
                template_data = {
                    "conflict_file_list": conflict_file_list,
                    "metadata": PR_METADATA,
                    "pr_branch_name": pr_branch_name,
                    "remote_branch": remote_branch,
                    "remote_url": remote_url,
                    "ssh_url": repo.ssh_url,
                }
                if conflict_file_list:
                    title = f"⚠️ CONFLICT! Lineage pull request for: {lineage_id}"
                    body = pystache.render(conflict_template, template_data)
                    create_pull_request(
                        repo, pr_branch_name, local_branch, title, body, draft=True
                    )
                else:
                    title = f"Lineage pull request for: {lineage_id}"
                    body = pystache.render(clean_template, template_data)
                    create_pull_request(
                        repo, pr_branch_name, local_branch, title, body, draft=False
                    )
            else:
                logging.info(
                    "Not creating a new pull request since the branch already existed."
                )

    logging.info("Completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
