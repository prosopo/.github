"""Keep exactly the open pull requests in an org's repos on an org Projects v2 board.

An open PR (drafts included) that is not on the board is added with the "open" status,
and its author is assigned. An open PR already on the board keeps whatever status
people gave it. A merged or closed PR is deleted from the board, archived items
included: the board is a view of work in flight, and a project holds at most 50,000
items with archived ones counting, so only deleting frees room. Deleting removes the
item's field values (status, priority) for good; reopening the PR re-adds it fresh.

Project workflows built into GitHub cannot assign people, and their settings cannot
be changed through the API, so this runs from Actions instead. It sweeps every
non-archived repo the GitHub App can see on a schedule rather than reacting to
pull_request events: Actions only fires on events in the repo holding the workflow,
which would mean a file in every repo, and pull_request_target (needed to give fork
PRs the token) is a known injection risk.

GitHub caps content-creating requests at about 80 a minute and 500 an hour, and the
first sweep has over a thousand old PR items to delete. So each run makes at most
--max-writes writes, --write-interval seconds apart, newest PRs first, and stops
cleanly if GitHub throttles it anyway. Later runs skip what is already in step, so a
backlog drains over successive runs.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def log(event, **kw):
    print(json.dumps({"event": event, "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}, ensure_ascii=False), flush=True)


class Throttled(Exception):
    pass


def is_throttle(status, headers, body):
    if status == 429:
        return True
    return status == 403 and ("retry-after" in headers or "rate limit" in body.lower())


class GitHub:
    def __init__(self, token, opener=urllib.request.urlopen):
        self.token = token
        self.opener = opener

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            path if path.startswith("https://") else API + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            if is_throttle(e.code, headers, text):
                raise Throttled(f"{e.code} {text[:200]}") from e
            raise RuntimeError(f"{method} {path}: {e.code} {text[:300]}") from e
        return json.loads(raw) if raw else {}

    def graphql(self, query, **variables):
        out = self.request("POST", "/graphql", {"query": query, "variables": variables})
        errors = out.get("errors")
        if errors:
            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                raise Throttled(f"graphql: {errors}")
            raise RuntimeError(f"graphql errors: {errors}")
        return out["data"]


PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      field(name: "Status") { ... on ProjectV2SingleSelectField { id options { id name } } }
    }
  }
}"""

PULLS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id number state updatedAt
        author { login __typename }
        assignees(first: 20) { nodes { login } }
        projectItems(first: 20, includeArchived: true) {
          nodes {
            id isArchived project { id }
            status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
          }
        }
      }
    }
  }
}"""

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}"""

DELETE_ITEM = """
mutation($project: ID!, $item: ID!) {
  deleteProjectV2Item(input: {projectId: $project, itemId: $item}) { deletedItemId }
}"""

SET_STATUS = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}}) { projectV2Item { id } }
}"""


class Board:
    def __init__(self, gh, owner, number):
        project = gh.graphql(PROJECT_QUERY, owner=owner, number=number)["organization"]["projectV2"]
        if project is None:
            raise RuntimeError(f"project {owner}/{number} not found or not visible to the token")
        field = project["field"]
        if not field:
            raise RuntimeError(f"project {owner}/{number} has no Status single-select field")
        self.gh = gh
        self.id = project["id"]
        self.field_id = field["id"]
        self.options = {o["name"]: o["id"] for o in field["options"]}

    def option(self, name):
        if name not in self.options:
            raise RuntimeError(f"status {name!r} not one of {sorted(self.options)}")
        return self.options[name]

    def add(self, pr_node_id):
        return self.gh.graphql(ADD_ITEM, project=self.id, content=pr_node_id)["addProjectV2ItemById"]["item"]["id"]

    def set_status(self, item, status):
        self.gh.graphql(SET_STATUS, project=self.id, item=item, field=self.field_id, option=self.option(status))

    def delete(self, item):
        self.gh.graphql(DELETE_ITEM, project=self.id, item=item)


def org_repos(gh):
    """Non-archived repos the app installation can see, as owner/name."""
    repos, page = [], 1
    while True:
        batch = gh.request("GET", f"/installation/repositories?per_page=100&page={page}")["repositories"]
        repos += [r["full_name"] for r in batch if not r["archived"]]
        if len(batch) < 100:
            return repos
        page += 1


def repo_prs(gh, repo):
    owner, name = repo.split("/", 1)
    after = None
    while True:
        conn = gh.graphql(PULLS_QUERY, owner=owner, name=name, after=after)["repository"]["pullRequests"]
        for pr in conn["nodes"]:
            yield pr
        if not conn["pageInfo"]["hasNextPage"]:
            return
        after = conn["pageInfo"]["endCursor"]


def author_to_assign(pr):
    author = pr.get("author")
    if pr["state"] != "OPEN" or not author or author["__typename"] == "Bot":
        return None
    if any(a["login"] == author["login"] for a in pr["assignees"]["nodes"]):
        return None
    return author["login"]


def plan(pr, board, open_status):
    """Writes this PR needs, as (kind, arg) pairs; empty when it is already in step."""
    item = next((i for i in pr["projectItems"]["nodes"] if i["project"]["id"] == board.id), None)
    if pr["state"] != "OPEN":
        return (item["id"], [("delete", None)]) if item else (None, [])
    steps = [("add", None), ("status", open_status)] if item is None else []
    login = author_to_assign(pr)
    if login:
        steps.append(("assign", login))
    return item and item["id"], steps


def apply(gh, board, repo, pr, item, steps, pace):
    for kind, arg in steps:
        pace()
        if kind == "add":
            item = board.add(pr["id"])
        elif kind == "status":
            board.set_status(item, arg)
        elif kind == "delete":
            board.delete(item)
        else:
            gh.request("POST", f"/repos/{repo}/issues/{pr['number']}/assignees", {"assignees": [arg]})
    log("pr.synced", repo=repo, pr=pr["number"], item=item, steps=[k if a is None else f"{k}:{a}" for k, a in steps])


def main(argv=None, gh=None, sleep=time.sleep):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--project-number", required=True, type=int)
    ap.add_argument("--open-status", required=True)
    ap.add_argument("--repo", action="append", help="owner/repo to sweep; repeat. Default: every non-archived repo")
    ap.add_argument("--max-writes", type=int, default=400, help="stop after this many writes; the next run continues")
    ap.add_argument("--write-interval", type=float, default=1.0,
                    help="seconds to wait before each write, keeping under ~80 content-creating requests a minute")
    ap.add_argument("--dry-run", action="store_true", help="plan and log the writes without making any")
    a = ap.parse_args(argv)
    gh = gh or GitHub(os.environ["GH_TOKEN"])
    board = Board(gh, a.owner, a.project_number)
    board.option(a.open_status)
    repos = a.repo or org_repos(gh)
    log("run.start", repos=len(repos), max_writes=a.max_writes)

    todo, failures, synced = [], 0, 0
    try:
        for repo in repos:
            try:
                for pr in repo_prs(gh, repo):
                    item, steps = plan(pr, board, a.open_status)
                    if steps:
                        todo.append((pr["updatedAt"], repo, pr, item, steps))
            except Throttled:
                raise
            except Exception as e:  # one unreachable repo must not stop the others
                failures += 1
                log("repo.failed", repo=repo, error=str(e)[:300])
        todo.sort(key=lambda t: t[0], reverse=True)
        log("run.planned", prs=len(todo), writes=sum(len(t[4]) for t in todo))
        if a.dry_run:
            for _, repo, pr, _, steps in todo[:20]:
                log("pr.would_sync", repo=repo, pr=pr["number"], steps=[k if v is None else f"{k}:{v}" for k, v in steps])
            return 1 if failures else 0

        writes = 0
        for done, (_, repo, pr, item, steps) in enumerate(todo):
            if writes + len(steps) > a.max_writes:
                log("run.budget_spent", writes=writes, remaining_prs=len(todo) - done)
                break
            try:
                apply(gh, board, repo, pr, item, steps, lambda: sleep(a.write_interval))
                synced += 1
            except Throttled:
                raise
            except Exception as e:  # one bad PR must not stop the sweep
                failures += 1
                log("pr.failed", repo=repo, pr=pr["number"], error=str(e)[:300])
            writes += len(steps)
    except Throttled as e:
        log("run.throttled", error=str(e)[:300])
    log("run.done", planned=len(todo), synced=synced, failed=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
