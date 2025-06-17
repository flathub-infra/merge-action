#!/usr/bin/env python3

import glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Any, cast

import gi
import github
import pygit2
import yaml
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport

gi.require_version("Json", "1.0")
from gi.repository import (  # type: ignore[attr-defined] # noqa: E402 # noqa: I001 # type: ignore[attr-defined]
    GLib,
    Json,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logging.getLogger("gql").setLevel(logging.WARNING)


def _read_json_flatpak_manifest(path: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {}

    if os.path.isfile(path) and os.path.basename(path).endswith(".json"):
        parser = Json.Parser()
        try:
            parser.load_from_file(path)
        except GLib.Error as err:
            logging.error("Failed to load JSON Flatpak manifest: %s", err.message)
        manifest = json.loads(Json.to_string(parser.get_root(), False))

    return manifest


def _read_yaml_flatpak_manifest(path: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {}

    if os.path.isfile(path) and os.path.basename(path).endswith((".yml", ".yaml")):
        try:
            with open(path) as f:
                manifest = yaml.safe_load(f)
        except yaml.YAMLError as err:
            logging.error("Failed to load YAML Flatpak manifest: %s", err)

    return manifest


def _read_flatpak_manifest(path: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {}

    if os.path.basename(path).endswith((".yaml", ".yml")):
        manifest = _read_yaml_flatpak_manifest(path)
    else:
        manifest = _read_json_flatpak_manifest(path)

    return manifest


def _get_id_from_flatpak_manifest(path: str) -> str | None:
    appid = None

    manifest = _read_flatpak_manifest(path)
    if isinstance(manifest, dict):
        appid = manifest.get("app-id") or manifest.get("id")

    if not appid:
        logging.error("Manifest %s does not contain 'app-id' or 'id'", path)

    return appid


def detect_appid(dirname: str) -> tuple[str | None, str | None]:
    ret = (None, None)

    files: list[str] = [
        file
        for ext in ("yml", "yaml", "json")
        for file in glob.glob(os.path.join(dirname, f"*.{ext}"))
        if os.path.isfile(file)
    ]

    if not files:
        logging.error(
            "Found no valid Flatpak manifest files in %s", os.path.abspath(dirname)
        )
        return ret

    for filename in files:
        manifest_file = os.path.basename(filename)
        logging.info("Checking file %s for Flatpak ID", manifest_file)
        appid = _get_id_from_flatpak_manifest(filename)
        if appid and os.path.splitext(manifest_file)[0] == appid:
            logging.info(
                "Detected Flatpak ID %s and manifest file %s", appid, manifest_file
            )
            return (manifest_file, appid)

    return ret


def load_github_event() -> dict[str, Any]:
    github_event: dict[str, Any] = {}
    github_event_path = os.environ.get("GITHUB_EVENT_PATH")

    if github_event_path:
        try:
            with open(github_event_path, encoding="utf-8") as f:
                github_event = json.load(f)
        except OSError:
            logging.error(
                "Failed to open GITHUB_EVENT_PATH %s: %s",
                github_event_path,
                github_event,
            )
        except json.JSONDecodeError:
            logging.error(
                "Failed to parse GITHUB_EVENT_PATH %s: %s",
                github_event_path,
                github_event,
            )

    return github_event


def parse_merge_command(
    github_comment: str,
) -> tuple[str | None, str | None, list[str] | None]:
    ret = (None, None, None)

    if not github_comment.startswith("/merge"):
        logging.info("The comment does not start with '/merge'")
        return ret

    command_pattern = re.compile(r"^/merge(?::([\w.-]+))? head=([a-fA-F0-9]{40})(.*)$")
    matched = command_pattern.search(github_comment)
    if not matched:
        logging.info(
            "The comment is not a valid '/merge' command.\n"
            "Format: '/merge:<optional target repo default branch, default: master> "
            "head=<pr head commit sha 40 chars> "
            "<optional extra collaborators @foo @baz, default: pr author>'"
        )
        return ret

    branch_match = matched.group(1) or "master"
    if branch_match in ("master", "beta"):
        target_repo_default_branch = branch_match
    else:
        target_repo_default_branch = f"branch/{branch_match}"

    logging.info("Got target branch %s from comment", target_repo_default_branch)

    pr_head_sha = matched.group(2)

    logging.info("Got PR HEAD SHA %s from comment", pr_head_sha)

    rest_comment = matched.group(3)
    # https://docs.github.com/en/enterprise-cloud@latest/admin/managing-iam/iam-configuration-reference/username-considerations-for-external-authentication#about-username-normalization
    # > Usernames for user accounts on GitHub can only contain alphanumeric
    # > characters and dashes
    # > If the username is longer than 39 characters
    # > (including underscore and short code),
    # > the provisioning attempt will fail with a 400 error.
    additional_colbs = [m[1:] for m in re.findall(r"@[a-zA-Z0-9-]{1,39}", rest_comment)]

    logging.info("Got additional collaborators %s from comment", additional_colbs)

    return target_repo_default_branch, pr_head_sha, additional_colbs


def get_repo_in_org(
    org: github.Organization.Organization, repo_name: str
) -> None | github.Repository.Repository:
    try:
        return org.get_repo(repo_name)
    except github.GithubException as err:
        if err.status == 404:
            logging.info("Repository %s does not exist in the organization", repo_name)
            return None
        logging.error(
            "Unexpected error from GitHub while fetching repository %s: %s",
            repo_name,
            err,
        )
        return None


def repo_exists_in_org(org: github.Organization.Organization, repo_name: str) -> bool:
    return get_repo_in_org(org, repo_name) is not None


def create_new_flathub_repo(
    org: github.Organization.Organization, repo_name: str
) -> None | github.Repository.Repository:
    if repo_exists_in_org(org, repo_name):
        return None

    try:
        logging.info("Creating repository %s", repo_name)
        repo = org.create_repo(repo_name)
        time.sleep(5)
        repo.edit(
            homepage=f"https://flathub.org/apps/details/{repo_name}",
            delete_branch_on_merge=True,
        )
        return repo
    except github.GithubException as err:
        logging.error(
            "Failed to create or edit GitHub repository %s: %s", repo_name, err
        )
        return None


def push_to_flathub_remote(
    repo_path: str,
    local_branch: str,
    remote_branch: str,
) -> bool:
    try:
        logging.info("Pushing changes to the new Flathub repo")
        git_push_cmd = ["git", "push", "flathub", f"{local_branch}:{remote_branch}"]
        subprocess.run(
            git_push_cmd,
            cwd=repo_path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return True
    except subprocess.CalledProcessError as err:
        logging.error("Git push failed:\n%s", err.output.decode().strip())
        return False


def set_protected_branch(token: str, repo: str, branch: str) -> dict[str, Any]:
    transport = RequestsHTTPTransport(
        url="https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {token}"},
    )
    client = Client(transport=transport, fetch_schema_from_transport=False)

    gql_get_repo_id = gql(
        """
        query get_repo_id($repo: String!) {
            repository(name: $repo, owner: "flathub") {
                id
            }
        }
        """
    )

    gql_add_branch_protection = gql(
        """
        mutation add_branch_protection($repositoryID: ID!, $pattern: String!) {
            createBranchProtectionRule(
                input: {
                    allowsDeletions: false
                    allowsForcePushes: false
                    dismissesStaleReviews: false
                    isAdminEnforced: false
                    pattern: $pattern
                    repositoryId: $repositoryID
                    requiresApprovingReviews: true
                    requiredApprovingReviewCount: 0
                    requiresCodeOwnerReviews: false
                    requiresStatusChecks: true
                    requiresStrictStatusChecks: true
                    restrictsReviewDismissals: false
                    requiredStatusCheckContexts: ["builds/x86_64"]
                }
            ) {
                branchProtectionRule {
                    id
                }
            }
        }
        """
    )

    repo_id = client.execute(gql_get_repo_id, variable_values={"repo": repo})
    repo_id = repo_id["repository"]["id"]

    return client.execute(
        gql_add_branch_protection,
        variable_values={"repositoryID": repo_id, "pattern": branch},
    )


def add_all_collaborators(
    org: github.Organization.Organization,
    created_repo_obj: github.Repository.Repository,
    collaborators: list[str],
) -> bool:
    teams_to_add: list[str] = ["trusted-maintainers"]

    if created_repo_obj.name.startswith("org.kde."):
        teams_to_add.append("KDE")
    elif (
        created_repo_obj.name.startswith("org.gnome.")
        and created_repo_obj.name.count(".") == 2
    ):
        teams_to_add.append("GNOME")

    try:
        for user in collaborators:
            logging.info("Adding user %s to collaborators", user)
            created_repo_obj.add_to_collaborators(user, permission="push")

        for team in teams_to_add:
            logging.info("Adding team %s to collaborators", team)
            team_slug = org.get_team_by_slug(team)
            team_slug.update_team_repository(created_repo_obj, "push")

        return True
    except github.GithubException as err:
        logging.error("Failed to set collaborators to GitHub repository: %s", err)
        return False


def is_valid_event(event: dict[str, Any]) -> bool:
    if not event:
        return False
    if event["action"] != "created":
        logging.info("The event is not a comment")
        return False
    if "pull_request" not in event["issue"]:
        logging.info("The issue is not a pull request")
        return False
    return True


def is_authorized_commenter(
    org: github.Organization.Organization, comment_author: github.NamedUser.NamedUser
) -> bool:
    admins = org.get_team_by_slug("admins")
    reviewers = org.get_team_by_slug("reviewers")
    if not (
        admins.has_in_members(comment_author)
        or reviewers.has_in_members(comment_author)
    ):
        logging.error(
            "GitHub comment author %s is not a reviewer or an admin", comment_author
        )
        return False
    return True


def clone_pr_fork(
    parent_repo_obj: github.Repository.Repository,
    pr_id: int,
    fork_url: str,
    pr_branch: str,
    approved_head_sha: str,
    clone_path: str,
) -> pygit2.Repository | None:
    logging.info(
        "Cloning the public fork %s from the PR branch %s", fork_url, pr_branch
    )
    clone: pygit2.Repository = pygit2.clone_repository(
        fork_url, clone_path, checkout_branch=pr_branch
    )
    clone_head_sha = str(clone.head.target)
    # https://github.com/libgit2/pygit2/issues/1322
    clone.submodules.update(init=True)  # type: ignore[attr-defined]

    pr_tmp = parent_repo_obj.get_pull(pr_id)
    if pr_tmp.state != "open":
        logging.error("The PR state is unexpectedly not open")
        return None

    if clone_head_sha != approved_head_sha or pr_tmp.head.sha != approved_head_sha:
        logging.error(
            "SHA mismatch: Clone HEAD %s, PR HEAD %s, Expected %s",
            clone_head_sha,
            pr_tmp.head.sha,
            approved_head_sha,
        )
        return None

    return clone


def finalize_new_flathub_repo(
    cloned_repo_obj: pygit2.Repository,
    remote_repo_obj: github.Repository.Repository,
    repo_name: str,
    github_token: str,
    pr_branch: str,
    target_remote_branch: str,
    approved_pr_sha: str,
    clone_path: str,
) -> bool:
    logging.info("Adding Flathub remote")
    # https://github.com/libgit2/pygit2/issues/1322
    cloned_repo_obj.remotes.create(  # type: ignore[attr-defined]
        "flathub",
        f"https://x-access-token:{github_token}@github.com/flathub/{repo_name}",
    )

    if not push_to_flathub_remote(clone_path, pr_branch, target_remote_branch):
        return False

    logging.info("Removing 'flathubbot' from collaborators")
    remote_repo_obj.remove_from_collaborators("flathubbot")

    logging.info("Setting protected branches")
    for branch in ("master", "main", "stable", "branch/*", "beta", "beta/*"):
        set_protected_branch(github_token, repo_name, branch)

    remote_branch_obj = remote_repo_obj.get_branch(target_remote_branch)
    remote_head_sha = str(remote_branch_obj.commit.sha)

    if remote_head_sha != approved_pr_sha:
        logging.error(
            "Remote HEAD SHA %s does not match approved SHA %s",
            remote_head_sha,
            approved_pr_sha,
        )
        return False

    if remote_branch_obj.protected is not True:
        logging.error("Remote branch %s is not protected", target_remote_branch)
        return False

    return True


def get_issue_from_pr(
    pr_obj: github.PullRequest.PullRequest,
) -> None | github.Issue.Issue:
    try:
        return pr_obj.base.repo.get_issue(number=pr_obj.number)
    except github.GithubException as err:
        logging.error("Failed to get issue from PR #%d: %s", pr_obj.number, err)
        return None


def close_pr(
    pr_obj: github.PullRequest.PullRequest,
    created_repo_obj: github.Repository.Repository,
) -> bool:
    maint_doc_url = "https://docs.flathub.org/docs/for-app-authors/maintenance"
    verif_doc_url = "https://docs.flathub.org/docs/for-app-authors/verification"
    blog_url = "https://docs.flathub.org/blog"

    close_comment = (
        "A repository for this submission has been created: "
        f"{created_repo_obj.html_url} "
        "and it will be published to Flathub within a few hours.\n"
        f"You will receive an [invite]({created_repo_obj.html_url}/invitations) "
        "to be a collaborator on the repository. Please make sure to enable "
        "2FA on GitHub and accept the invite within a week.\n"
        f"Please go through the [App maintenance guide]({maint_doc_url}) "
        "if you have never maintained an app on Flathub before.\n"
        "If you are the original developer (or an authorized party), please "
        f"[verify your app]({verif_doc_url}) "
        "to let users know it's coming from you.\n"
        f"Please follow the [Flathub blog]({blog_url}) for the latest "
        "announcements.\n"
        "Thanks!"
    )

    try:
        logging.info("Closing the pull request")
        pr_obj.set_labels("ready")
        pr_obj.create_issue_comment(close_comment)
        pr_obj.edit(state="closed")
        issue_obj = get_issue_from_pr(pr_obj)
        if issue_obj and not issue_obj.locked:
            issue_obj.lock("resolved")
        return True
    except github.GithubException as err:
        logging.error("Unexpected error from GitHub while closing PR: %s", err)
        return False


def main() -> int:
    github_token = os.environ.get("INPUT_TOKEN")
    if not github_token:
        logging.error("Token is not set")
        return 1

    github_event = load_github_event()

    if not is_valid_event(github_event):
        return 1

    github_comment = github_event["comment"]["body"]

    target_repo_default_branch, approved_pr_head_sha, additional_colbs = (
        parse_merge_command(github_comment)
    )

    # For mypy
    if not (
        target_repo_default_branch
        and approved_pr_head_sha
        and additional_colbs is not None
    ):
        return 1

    gh = github.Github(auth=github.Auth.Token(github_token))
    org = gh.get_organization("flathub")
    flathub = org.get_repo("flathub")
    comment_author = gh.get_user(github_event["comment"]["user"]["login"])
    pr_id = int(github_event["issue"]["number"])
    pr = flathub.get_pull(pr_id)
    pr_branch = pr.head.label.split(":")[1]
    fork_url = pr.head.repo.clone_url
    pr_author = pr.user.login

    additional_colbs.append(pr_author)

    comment_author = cast(github.NamedUser.NamedUser, comment_author)

    if not is_authorized_commenter(org, comment_author):
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        cloned_obj = clone_pr_fork(
            parent_repo_obj=flathub,
            pr_id=pr_id,
            fork_url=fork_url,
            pr_branch=pr_branch,
            approved_head_sha=approved_pr_head_sha,
            clone_path=tmpdir,
        )
        if cloned_obj is None:
            return 1

        manifest_file, appid = detect_appid(tmpdir)
        if not (manifest_file and appid):
            logging.error("Failed to detect manifest file or appid")
            return 1

        created_repo_obj = create_new_flathub_repo(org, appid)

        if created_repo_obj is None:
            logging.error("Failed to get GitHub repo after creation")
            return 1

        if not finalize_new_flathub_repo(
            cloned_repo_obj=cloned_obj,
            remote_repo_obj=created_repo_obj,
            repo_name=appid,
            github_token=github_token,
            pr_branch=pr_branch,
            target_remote_branch=target_repo_default_branch,
            approved_pr_sha=approved_pr_head_sha,
            clone_path=tmpdir,
        ):
            return 1

        if not add_all_collaborators(
            org=org, created_repo_obj=created_repo_obj, collaborators=additional_colbs
        ):
            return 1

        if flathub.get_pull(pr_id).state != "open":
            logging.error("The PR state is unexpectedly not open")
            return 1

        if not close_pr(pr_obj=pr, created_repo_obj=created_repo_obj):
            return 1

        if flathub.get_pull(pr_id).state != "closed":
            logging.error("The PR state is unexpectedly not closed")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
