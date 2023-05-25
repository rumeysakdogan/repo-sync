import os
import shutil
from github import Github
from git import Repo
import json
from concurrent.futures import ThreadPoolExecutor
import logging
import time
import requests
from requests.auth import HTTPBasicAuth

SOURCE_URL = "https://github.com"
DESTINATION_URL = "https://dev.azure.com"
SOURCE_USER = os.environ['USER_ORG']
DESTINATION_ORG = os.environ['USER_ORG']
DESTINATION_PROJECT = "repo-sync"
PERSONAL_ACCESS_TOKEN = os.environ['GH_TOKEN']
DESTINATION_PERSONAL_ACCESS_TOKEN = os.environ['ADO_PERSONAL_ACCESS_TOKEN']
RESTRICTED_PREFIX = "restricted"
ASSET_PREFIX = "asset"
LOCAL_PATH = os.environ['RUNNER_TEMP']

logging.basicConfig(format="[%(levelname)s] %(message)s", level=logging.INFO)


def create_ado_repository(org_name: str, project_name: str, repo_name: str) -> None:
    url = f"https://dev.azure.com/{org_name}/{project_name}/_apis/git/repositories?api-version=6.0"
    data = {"name": repo_name}
    headers = {"Content-Type": "application/json"}
    response = requests.post(
        url,
        data=json.dumps(data),
        headers=headers,
        auth=HTTPBasicAuth("", DESTINATION_PERSONAL_ACCESS_TOKEN),
    )
    response.raise_for_status()


def list_ado_repositories(org_name: str, project_name: str) -> list:
    destination_repos = []
    url = f"https://dev.azure.com/{org_name}/{project_name}/_apis/git/repositories?api-version=6.0"
    headers = {"Content-Type": "application/json"}
    response = requests.get(
        url, headers=headers, auth=HTTPBasicAuth("", DESTINATION_PERSONAL_ACCESS_TOKEN)
    )
    response.raise_for_status()
    repos = json.loads(response.text)["value"]
    for repo in repos:
        destination_repos.append(repo["name"])
    return destination_repos


def get_default_branch(repo_name: str) -> str:
    g = Github(PERSONAL_ACCESS_TOKEN)
    repo = g.get_repo(f"{SOURCE_USER}/{repo_name}")
    return repo.default_branch


def clone_and_push(repo_name: str) -> None:
    logging.info(f'Cloning repo: {repo_name} started.')
    default_branch = get_default_branch(repo_name)

    # Clone only the latest version of the default branch from Github
    clone_url = f"{SOURCE_URL}/{SOURCE_USER}/{repo_name}.git"
    repo_path = f"/tmp/{repo_name}"

    # Check if repo_path already exists and is not empty
    if os.path.exists(repo_path) and os.listdir(repo_path):
        # Delete the existing directory and its contents
        shutil.rmtree(repo_path)

    repo = Repo.clone_from(clone_url, repo_path, branch=default_branch, depth=1)

    # Remove history and initialize new repository
    shutil.rmtree(os.path.join(repo_path, ".git"))
    tmp_repo = Repo.init(repo_path)

    # Make an initial commit
    tmp_repo.git.add(A=True)
    tmp_repo.index.commit("Update from GitHub")

    # Add destination remote
    remote_name = "destination"
    remote_url = f"{DESTINATION_URL}/{DESTINATION_ORG}/{DESTINATION_PROJECT}/_git/{repo_name}"
    remote_url_with_token = remote_url.replace(
        "https://", f"https://{DESTINATION_PERSONAL_ACCESS_TOKEN}@"
    )
    tmp_repo.create_remote(remote_name, url=remote_url_with_token)

    # Push changes to destination
    tmp_repo.remotes.destination.push(f"HEAD:refs/heads/{default_branch}", force=True)

    # Clean up
    shutil.rmtree(repo_path)
    logging.info(f'Cloning repo: {repo_name} complete.')


def synchronize_and_push(repo_name: str) -> None:
    logging.info(f'Synchronization of repo: {repo_name} started.')
    default_branch = get_default_branch(repo_name)

    # Set up source and destination repository URLs
    source_clone_url = f"{SOURCE_URL}/{SOURCE_USER}/{repo_name}.git"
    destination_clone_url = (
        f"{DESTINATION_URL}/{DESTINATION_ORG}/{DESTINATION_PROJECT}/_git/{repo_name}"
    )
    destination_clone_url_with_token = destination_clone_url.replace(
        "https://", f"https://{DESTINATION_PERSONAL_ACCESS_TOKEN}@"
    )

    # Set up temporary repository path
    tmp_repo_path = f"/tmp/{repo_name}"
    if os.path.exists(tmp_repo_path):
        shutil.rmtree(tmp_repo_path)

    # Clone source repository
    Repo.clone_from(source_clone_url, tmp_repo_path, branch=default_branch, depth=1)

    # Add destination remote
    tmp_repo = Repo(tmp_repo_path)
    tmp_repo.create_remote("destination", url=destination_clone_url_with_token)

    # Pull changes from destination repository
    try:
        tmp_repo.remotes.destination.pull(default_branch)
    except Exception as e:
        logging.warning(f"Failed to pull changes from destination repository: {e}")

    # Push changes to destination
    tmp_repo.remotes.destination.push(f"HEAD:refs/heads/{default_branch}", force=True)

    # Clean up
    shutil.rmtree(tmp_repo_path)
    logging.info(f'Synchronization of repo: {repo_name} complete.')

def main():
    start_time = time.time()
    logging.debug(f"Started cloning/syncing at {start_time}")

    g = Github(PERSONAL_ACCESS_TOKEN)

    # Get all repositories from GitHub except repos starting with restricted prefix
    source_repos = g.get_user().get_repos()
    source_repos = [
        repo for repo in source_repos if not repo.name.startswith(RESTRICTED_PREFIX)
        and not repo.name.startswith(ASSET_PREFIX)
        and repo.name.startswith("gh")
    ]
    logging.info(f"{len(source_repos)} repositories found in GitHub.")

    # Get all repositories from destination platform
    destination_repos = list_ado_repositories(DESTINATION_ORG, DESTINATION_PROJECT)
    logging.info(f"{len(destination_repos)} repositories found in ADO.")

    # Find new repositories on GitHub that do not exist on destination platform
    new_repos = [repo for repo in source_repos if repo.name not in destination_repos]

    # Create new repositories on destination platform and push changes from Github
    logging.info(f"{len(new_repos)} repositories need to be created in ADO.")

    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(
                create_ado_repository, DESTINATION_ORG, DESTINATION_PROJECT, repo.name
            )
            for repo in new_repos
        ]
        futures += [executor.submit(clone_and_push, repo.name) for repo in new_repos]

        for future in futures:
            try:
                future.result()
            except Exception as e:
                print(f"Error: Task failed with exception: {e}")

    # Pull latest changes from main/master branch of existing repositories and push to destination platform
    existing_repos = [
        repo for repo in source_repos if repo.name in destination_repos
    ]
    logging.info(f"{len(existing_repos)} repositories in ADO will be synchronized with GitHub.")

    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(synchronize_and_push, repo.name) for repo in existing_repos
        ]

        for future in futures:
            try:
                future.result()
            except Exception as e:
                print(f"Error: Task failed with exception: {e}")
    exec_time = time.gmtime(time.time() - start_time)

    logging.info(
        f"Synchronization time: {exec_time.tm_hour}h {exec_time.tm_min}m {exec_time.tm_sec}s"
    )


if __name__ == "__main__":
    main()