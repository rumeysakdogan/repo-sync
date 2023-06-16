import os
import subprocess
import shutil
from github import Github
from git import Repo
import json
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

logging.basicConfig(format="[%(levelname)s] %(message)s", level=logging.INFO)


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

        # Clone the repository with a depth of 1 and without checkout
        clone_url = f"{SOURCE_URL}/{SOURCE_USER}/{repo_name}.git"
        repo_path = f"{LOCAL_PATH}/tempdir/{repo_name}"

        # Check if repo_path already exists and is not empty
        if os.path.exists(repo_path) and os.listdir(repo_path):
            # Delete the existing directory and its contents
            shutil.rmtree(repo_path)

        subprocess.run(
            ["git", "clone", "--no-checkout", "--depth", "1", clone_url, repo_path],
            check=True,
        )

        # Change to the cloned repository directory
        os.chdir(repo_path)

        # Fetch the latest commit from the default branch
        subprocess.run(
            ["git", "fetch", "origin", default_branch],
            check=True,
        )

        # Create a new branch for Azure DevOps
        azure_devops_branch = "azure-devops-branch"
        subprocess.run(
            ["git", "checkout", "-b", azure_devops_branch],
            check=True,
        )

        # Add the Azure DevOps remote URL
        remote_name = "destination"
        remote_url = f"{DESTINATION_URL}/{DESTINATION_ORG}/{DESTINATION_PROJECT}/_git/{repo_name}"
        remote_url_with_token = remote_url.replace(
            "https://", f"https://{DESTINATION_PERSONAL_ACCESS_TOKEN}@"
        )
        subprocess.run(
            ["git", "remote", "add", remote_name, remote_url_with_token],
            check=True,
        )

        # Push the new branch to Azure DevOps
        subprocess.run(
            [
                "git",
                "push",
                "--force",
                remote_name,
                f"{azure_devops_branch}:refs/heads/{default_branch}",
            ],
            check=True,
        )
        # Clean up
        os.chdir("..")
        shutil.rmtree(repo_path)
        logging.info(f"Cloning repo: {repo_name} complete.")
        return (repo_name, None)
    except Exception as e:
        return (repo_name, e)


def synchronize_repository(repo_name):
    result = clone_and_push(repo_name)
    if result is not None:
        repo_name, error = result
        if error:
            print(
                f"Error: Task failed with exception: {error} for repository: {repo_name}"
            )


def main():
    start_time = time.time()
    logging.info(f"Started cloning/syncing at {start_time}")

    g = Github(PERSONAL_ACCESS_TOKEN)

    # Get all repositories from GitHub except repos starting with restricted prefix
    source_repos = g.get_user().get_repos()
    source_repos = [
        repo
        for repo in source_repos
        if not repo.name.startswith(RESTRICTED_PREFIX) and repo.name.startswith("pet")
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
        synchronize_repository(repo.name)

    # Pull latest changes from main/master branch of existing repositories and push to destination platform
    existing_repos = [repo for repo in source_repos if repo.name in destination_repos]
    logging.info(
        f"{len(existing_repos)} repositories in ADO will be synchronized with GitHub."
    )

    for repo in existing_repos:
        synchronize_repository(repo.name)

    exec_time = time.gmtime(time.time() - start_time)

    logging.info(
        f"Synchronization time: {exec_time.tm_hour}h {exec_time.tm_min}m {exec_time.tm_sec}s"
    )


if __name__ == "__main__":
    main()
