"""Keep pull requests on an org Projects v2 board in step with their state.

Open PRs are added to the board with the "open" status and their author assigned;
merged or closed PRs get the "closed" status. Project workflows built into GitHub
cannot assign people, and their settings cannot be changed through the API, so this
runs from Actions instead.

It sweeps the listed repos on a schedule rather than reacting to pull_request
events: Actions only fires on events in the repo holding the workflow, which would
mean a file in every repo, and pull_request_target (needed to give fork PRs the
token) is a known injection risk. Every operation is idempotent, so overlapping
sweep windows are harmless.
"""

import argparse
import calendar
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


def parse_time(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def recent_prs(gh, repo, since_minutes, now=None):
    """PRs to sync: every open PR when since_minutes is 0, else any PR updated in the window."""
    state, cutoff = ("open", None) if since_minutes == 0 else ("all", (now or time.time()) - since_minutes * 60)
    page = 1
    while True:
        batch = gh.request("GET", f"/repos/{repo}/pulls?state={state}&sort=updated&direction=desc&per_page=100&page={page}")
        for pr in batch:
            if cutoff is not None and parse_time(pr["updated_at"]) < cutoff:
                return
            yield pr
        if len(batch) < 100:
            return
        page += 1


def main(argv=None, gh=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--project-number", required=True, type=int)
    ap.add_argument("--open-status", required=True)
    ap.add_argument("--closed-status", required=True)
    ap.add_argument("--repo", action="append", required=True, help="owner/repo to sweep; repeat")
    ap.add_argument("--since-minutes", type=int, default=0, help="only PRs updated this recently; 0 = every open PR")
    a = ap.parse_args(argv)
    gh = gh or GitHub(os.environ["GH_TOKEN"])
    board = Board(gh, a.owner, a.project_number)
    synced = failures = 0
    for repo in a.repo:
        try:
            prs = list(recent_prs(gh, repo, a.since_minutes, now))
        except Exception as e:  # one unreachable repo must not stop the others
            failures += 1
            log("repo.failed", repo=repo, error=str(e)[:300])
            continue
        for pr in prs:
            try:
                sync_pr(gh, board, repo, pr, a.open_status, a.closed_status)
                synced += 1
            except Exception as e:  # one bad PR must not stop the sweep
                failures += 1
                log("pr.failed", repo=repo, pr=pr.get("number"), error=str(e)[:300])
    log("run.done", synced=synced, failed=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
