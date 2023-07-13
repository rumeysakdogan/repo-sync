import os
import subprocess
import shutil
from github import Github
from git import Repo
import json
from concurrent.futures import ThreadPoolExecutor
import logging
import time

SOURCE_URL = "https://github.com"
DESTINATION_URL = "https://dev.azure.com"
SOURCE_USER = os.environ["USER_ORG"]
DESTINATION_ORG = os.environ["USER_ORG"]
DESTINATION_PROJECT = "repo-sync"
PERSONAL_ACCESS_TOKEN = os.environ["GH_TOKEN"]
DESTINATION_PERSONAL_ACCESS_TOKEN = os.environ["ADO_PERSONAL_ACCESS_TOKEN"]
RESTRICTED_PREFIX = "restricted"
ASSET_PREFIX = "asset"
LOCAL_PATH = os.environ["RUNNER_TEMP"]

logging.basicConfig(format="[%(levelname)s] %(message)s", level=logging.DEBUG)


def create_ado_repository(org_name: str, project_name: str, repo_name: str) -> None:
    # Replace period at beginning of repo_name with underscore
    if repo_name.startswith("."):
        repo_name = "_" + repo_name[1:]
    # Create the repository
    cmd = [
        "az",
        "repos",
        "create",
        "--org",
        f"https://dev.azure.com/{org_name}",
        "--project",
        project_name,
        "--name",
        repo_name,
        "--output",
        "table",
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Error creating repository {repo_name}: {e}")


def list_ado_repositories(org_name: str, project_name: str) -> list:
    destination_repos = []
    cmd = (
        f"az repos list --org https://dev.azure.com/{org_name} --project {project_name}"
    )
    output = os.popen(cmd).read()
    repos = json.loads(output)
    for repo in repos:
        destination_repos.append(repo["name"])
    return destination_repos


def get_default_branch(repo_name: str) -> str:
    g = Github(PERSONAL_ACCESS_TOKEN)
    repo = g.get_repo(f"{SOURCE_USER}/{repo_name}")
    return repo.default_branch


def clone_and_push(repo_name: str) -> tuple:
    try:
        logging.info(f"Cloning repo: {repo_name} started.")
        default_branch = get_default_branch(repo_name)
        logging.debug(f"Default branch for repo {repo_name}: {default_branch}")

        if default_branch is None:
            logging.info(f"Skipping empty repo: {repo_name}")
            return (repo_name, None)

        # Clone the repository without depth
        clone_url = f"{SOURCE_URL}/{SOURCE_USER}/{repo_name}.git"
        repo_path = f"{LOCAL_PATH}/tempdir/{repo_name}"

        if os.path.exists(repo_path):
            shutil.rmtree(repo_path)

        repo = Repo.clone_from(clone_url, repo_path, branch=default_branch)

        # Reset the repository to remove commit history
        repo.git.reset("--hard", default_branch)

        # Push to destination
        remote_name = "destination"
        remote_url = f"{DESTINATION_URL}/{DESTINATION_ORG}/{DESTINATION_PROJECT}/_git/{repo_name}"
        remote_url_with_token = remote_url.replace(
            "https://", f"https://{DESTINATION_PERSONAL_ACCESS_TOKEN}@"
        )

        repo.create_remote(remote_name, url=remote_url_with_token)
        repo.git.push(remote_name, f"HEAD:refs/heads/{default_branch}", force=True)

        # Clean up
        shutil.rmtree(repo_path)
        logging.info(f"Cloning repo: {repo_name} complete.")
        return (repo_name, None)
    except Exception as e:
        return (repo_name, e)


def synchronize_and_push(repo_name: str) -> tuple:
    try:
        logging.info(f"Synchronization of repo: {repo_name} started.")
        default_branch = get_default_branch(repo_name)
        # Set up source and destination repository URLs
        source_clone_url = f"{SOURCE_URL}/{SOURCE_USER}/{repo_name}.git"
        # Replace period at beginning of repo_name with underscore
        if repo_name.startswith("."):
            repo_name = "_" + repo_name[1:]
        destination_clone_url = f"{DESTINATION_URL}/{DESTINATION_ORG}/{DESTINATION_PROJECT}/_git/{repo_name}"
        destination_clone_url_with_token = destination_clone_url.replace(
            "https://", f"https://{DESTINATION_PERSONAL_ACCESS_TOKEN}@"
        )

        # Set up temporary repository path
        tmp_repo_path = f"{LOCAL_PATH}/tempdir/{repo_name}"
        if os.path.exists(tmp_repo_path):
            shutil.rmtree(tmp_repo_path)

        # Clone source repository
        tmp_repo = Repo.clone_from(
            source_clone_url, tmp_repo_path, branch=default_branch, depth=1
        )

        # Pull LFS objects
        tmp_repo.git.lfs("pull")

        # Remove history and initialize new repository
        shutil.rmtree(os.path.join(tmp_repo_path, ".git"))
        tmp_repo = Repo.init(tmp_repo_path)

        # Make an initial commit
        tmp_repo.git.add(A=True)
        tmp_repo.index.commit("Update from GitHub")

        # Add destination remote
        tmp_repo.create_remote("destination", url=destination_clone_url_with_token)

        # Push changes to destination
        tmp_repo.remotes.destination.push(
            f"HEAD:refs/heads/{default_branch}", force=True
        )

        # Clean up
        shutil.rmtree(tmp_repo_path)
        logging.info(f"Synchronization of repo: {repo_name} complete.")
        return (repo_name, None)
    except Exception as e:
        return (repo_name, e)


def main():
    start_time = time.time()
    logging.debug(f"Started cloning/syncing at {start_time}")

    g = Github(PERSONAL_ACCESS_TOKEN)

    # Get all repositories from GitHub except repos starting with restricted prefix
    source_repos = g.get_user().get_repos()
    source_repos = [
        repo
        for repo in source_repos
        if not repo.name.startswith(RESTRICTED_PREFIX) and repo.name.startswith("skill")
    ]
    logging.info(f"{len(source_repos)} repositories found in GitHub.")

    # Get all repositories from destination platform
    destination_repos = list_ado_repositories(DESTINATION_ORG, DESTINATION_PROJECT)
    logging.info(f"{len(destination_repos)} repositories found in ADO.")

    # Find new repositories on GitHub that do not exist on destination platform
    new_repos = [repo for repo in source_repos if repo.name not in destination_repos]

    # Create new repositories on destination platform and push changes from Github
    logging.info(f"{len(new_repos)} repositories need to be created in ADO.")

    for repo in new_repos:
        create_ado_repository(DESTINATION_ORG, DESTINATION_PROJECT, repo.name)
        result = clone_and_push(repo.name)
        if result is not None:
            repo_name, error = result
            if error:
                print(
                    f"Error: Task failed with exception: {error} for repository: {repo_name}"
                )

    # Pull latest changes from main/master branch of existing repositories and push to destination platform
    existing_repos = [repo for repo in source_repos if repo.name in destination_repos]
    logging.info(
        f"{len(existing_repos)} repositories in ADO will be synchronized with GitHub."
    )

    for repo in existing_repos:
        result = synchronize_and_push(repo.name)
        if result is not None:
            repo_name, error = result
            if error:
                print(
                    f"Error: Task failed with exception: {error} for repository: {repo_name}"
                )

    exec_time = time.gmtime(time.time() - start_time)

    sync_time = f"Synchronization time: {exec_time.tm_hour}h {exec_time.tm_min}m {exec_time.tm_sec}s"
    logging.info(sync_time)
    print(sync_time, file=open("$GITHUB_OUTPUT", "a"))


if __name__ == "__main__":
    main()
