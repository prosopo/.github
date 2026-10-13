"""Keep pull requests on an org Projects v2 board in step with their state.

Open PRs are added to the board with the "open" status and their author assigned;
merged or closed PRs get the "closed" status. Project workflows built into GitHub
cannot assign people, and their settings cannot be changed through the API, so this
runs from Actions instead.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

API = "https://api.github.com"


def log(event, **kw):
    print(json.dumps({"event": event, "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}), flush=True)


class GitHub:
    def __init__(self, token, opener=urllib.request.urlopen):
        self.token = token
        self.opener = opener

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            API + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        with self.opener(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def graphql(self, query, **variables):
        out = self.request("POST", "/graphql", {"query": query, "variables": variables})
        if out.get("errors"):
            raise RuntimeError(f"graphql errors: {out['errors']}")
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

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
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

    def place(self, pr_node_id, status):
        option = self.option(status)
        item = self.gh.graphql(ADD_ITEM, project=self.id, content=pr_node_id)["addProjectV2ItemById"]["item"]["id"]
        self.gh.graphql(SET_STATUS, project=self.id, item=item, field=self.field_id, option=option)
        return item


def assign_author(gh, repo, pr):
    author = pr["user"]
    if author.get("type") == "Bot":
        return False
    if any(a["login"] == author["login"] for a in pr.get("assignees") or []):
        return False
    gh.request("POST", f"/repos/{repo}/issues/{pr['number']}/assignees", {"assignees": [author["login"]]})
    return True


def sync_pr(gh, board, repo, pr, open_status, closed_status):
    is_open = pr["state"] == "open"
    status = open_status if is_open else closed_status
    item = board.place(pr["node_id"], status)
    assigned = assign_author(gh, repo, pr) if is_open else False
    log("pr.synced", repo=repo, pr=pr["number"], status=status, item=item, assigned=assigned)


def open_prs(gh, repo):
    page = 1
    while True:
        batch = gh.request("GET", f"/repos/{repo}/pulls?state=open&per_page=100&page={page}")
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def main(argv=None, gh=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--project-number", required=True, type=int)
    ap.add_argument("--open-status", required=True)
    ap.add_argument("--closed-status", required=True)
    ap.add_argument("--event-path", help="pull_request event payload to sync that one PR")
    ap.add_argument("--backfill-repo", action="append", default=[], help="owner/repo whose open PRs to sync")
    a = ap.parse_args(argv)
    if not a.event_path and not a.backfill_repo:
        ap.error("give --event-path or --backfill-repo")
    gh = gh or GitHub(os.environ["GH_TOKEN"])
    board = Board(gh, a.owner, a.project_number)
    failures = 0
    targets = []
    if a.event_path:
        with open(a.event_path) as f:
            event = json.load(f)
        targets.append((event["repository"]["full_name"], event["pull_request"]))
    for repo in a.backfill_repo:
        targets.extend((repo, pr) for pr in open_prs(gh, repo))
    for repo, pr in targets:
        try:
            sync_pr(gh, board, repo, pr, a.open_status, a.closed_status)
        except Exception as e:  # one bad PR must not stop a backfill
            failures += 1
            log("pr.failed", repo=repo, pr=pr.get("number"), error=str(e)[:300])
    log("run.done", synced=len(targets) - failures, failed=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
