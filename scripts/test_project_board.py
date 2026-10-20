import io
import json
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

import project_board as pb

OPTIONS = [{"id": "opt-prog", "name": "In progress 📊"}, {"id": "opt-done", "name": "Done ✅"},
           {"id": "opt-high", "name": "High Priority"}]


class FakeGitHub:
    def __init__(self, project=None, pulls=None, repos=None, fail_add_for=(), fail_list_for=(), throttle_after=None):
        self.project = project if project is not None else {
            "id": "P1", "field": {"id": "F1", "options": OPTIONS}}
        self.pulls = pulls or {}
        self.repos = repos or []
        self.fail_add_for = set(fail_add_for)
        self.fail_list_for = set(fail_list_for)
        self.throttle_after = throttle_after
        self.calls = []
        self.sleeps = []

    def writes(self):
        return [c for c in self.calls if c[0] in ("add", "status", "delete", "POST")]

    def _maybe_throttle(self):
        if self.throttle_after is not None and len(self.writes()) > self.throttle_after:
            raise pb.Throttled("secondary rate limit")

    def graphql(self, query, **v):
        if "projectV2(number" in query:
            return {"organization": {"projectV2": self.project}}
        if "pullRequests(" in query:
            repo = f"{v['owner']}/{v['name']}"
            self.calls.append(("list", repo, v["after"]))
            if repo in self.fail_list_for:
                raise RuntimeError("404")
            start = int(v["after"] or 0)
            items = self.pulls.get(repo, [])
            return {"repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": start + 100 < len(items), "endCursor": str(start + 100)},
                "nodes": items[start:start + 100]}}}
        if "addProjectV2ItemById" in query:
            self.calls.append(("add", v["content"]))
            self._maybe_throttle()
            if v["content"] in self.fail_add_for:
                raise RuntimeError("boom")
            return {"addProjectV2ItemById": {"item": {"id": "item-" + v["content"]}}}
        if "updateProjectV2ItemFieldValue" in query:
            self.calls.append(("status", v["item"], v["option"]))
            self._maybe_throttle()
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["item"]}}}
        if "deleteProjectV2Item" in query:
            self.calls.append(("delete", v["item"]))
            self._maybe_throttle()
            return {"deleteProjectV2Item": {"deletedItemId": v["item"]}}
        raise AssertionError(query)

    def request(self, method, path, body=None):
        if method == "GET":
            page = int(path.split("page=")[-1])
            batch = self.repos[(page - 1) * 100: page * 100]
            return {"repositories": batch}
        self.calls.append((method, path, body))
        self._maybe_throttle()
        return {}


def item(project="P1", status=None, archived=False):
    return {"id": f"I-{project}", "isArchived": archived, "project": {"id": project},
            "status": {"name": status} if status else None}


def pr(n, state="OPEN", login="alice", kind="User", assignees=(), items=(), updated="2026-10-20T08:00:00Z"):
    return {"id": f"PR{n}", "number": n, "state": state, "updatedAt": updated,
            "author": {"login": login, "__typename": kind} if login else None,
            "assignees": {"nodes": [{"login": a} for a in assignees]},
            "projectItems": {"nodes": list(items)}}


def run(gh, *args):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = pb.main(["--owner", "prosopo", "--project-number", "4",
                      "--open-status", "In progress 📊", *args], gh=gh, sleep=gh.sleeps.append)
    return rc, [json.loads(line) for line in out.getvalue().splitlines()]


def sweep(gh, *args, repo="prosopo/captcha"):
    return run(gh, "--repo", repo, *args)


def events(logs, name):
    return [entry for entry in logs if entry["event"] == name]


class PlanTests(unittest.TestCase):
    def test_open_pr_not_on_board_added_in_progress_and_author_assigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(7)]})
        rc, _ = sweep(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(gh.writes(), [("add", "PR7"), ("status", "item-PR7", "opt-prog"),
                                       ("POST", "/repos/prosopo/captcha/issues/7/assignees", {"assignees": ["alice"]})])

    def test_merged_and_closed_prs_not_on_board_are_not_added(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(8, state="MERGED"), pr(9, state="CLOSED")]})
        rc, logs = sweep(gh)
        self.assertEqual((rc, gh.writes()), (0, []))
        self.assertEqual(events(logs, "run.planned")[0]["prs"], 0)

    def test_open_pr_on_board_keeps_its_status(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(1, assignees=["alice"], items=[item(status="High Priority")])]})
        rc, logs = sweep(gh)
        self.assertEqual((rc, gh.writes()), (0, []))
        self.assertEqual(events(logs, "run.planned")[0]["prs"], 0)

    def test_open_pr_on_board_without_status_is_left_alone(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(1, assignees=["alice"], items=[item()])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [])

    def test_open_pr_on_board_still_gets_its_author_assigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(1, items=[item(status="Backlog")])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [("POST", "/repos/prosopo/captcha/issues/1/assignees", {"assignees": ["alice"]})])

    def test_merged_pr_on_board_is_deleted(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(2, state="MERGED", items=[item(status="In progress 📊")])]})
        rc, logs = sweep(gh)
        self.assertEqual((rc, gh.writes()), (0, [("delete", "I-P1")]))
        self.assertEqual(events(logs, "pr.synced")[0]["steps"], ["delete"])

    def test_closed_pr_on_board_is_deleted_whatever_its_status(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(2, state="CLOSED", items=[item(status="Done ✅")])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [("delete", "I-P1")])

    def test_archived_item_of_closed_pr_is_deleted(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(3, state="MERGED", items=[item(status="Done ✅", archived=True)])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [("delete", "I-P1")])

    def test_archived_item_of_open_pr_counts_as_on_board(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(3, assignees=["alice"], items=[item(archived=True)])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [])

    def test_item_on_another_project_does_not_count(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(4, assignees=["alice"], items=[item(project="P9")]),
                                                   pr(5, state="CLOSED", items=[item(project="P9")])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [("add", "PR4"), ("status", "item-PR4", "opt-prog")])

    def test_bot_author_not_assigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(9, login="dependabot", kind="Bot", items=[item()])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [])

    def test_deleted_author_not_assigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(9, login=None, items=[item()])]})
        sweep(gh)
        self.assertEqual(gh.writes(), [])

    def test_already_assigned_author_not_reassigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(10, assignees=["bob", "alice"])]})
        sweep(gh)
        self.assertEqual([c for c in gh.writes() if c[0] == "POST"], [])


class SweepTests(unittest.TestCase):
    def test_defaults_to_every_non_archived_repo(self):
        repos = [{"full_name": "prosopo/a", "archived": False}, {"full_name": "prosopo/old", "archived": True},
                 {"full_name": "prosopo/b", "archived": False}]
        gh = FakeGitHub(repos=repos, pulls={"prosopo/a": [pr(1)], "prosopo/b": [pr(2)]})
        rc, logs = run(gh)
        self.assertEqual(rc, 0)
        self.assertEqual([c[1] for c in gh.calls if c[0] == "list"], ["prosopo/a", "prosopo/b"])
        self.assertEqual(events(logs, "run.start")[0]["repos"], 2)

    def test_pages_through_installation_repos(self):
        repos = [{"full_name": f"prosopo/r{n}", "archived": False} for n in range(150)]
        self.assertEqual(len(pb.org_repos(FakeGitHub(repos=repos))), 150)

    def test_pages_through_prs(self):
        gh = FakeGitHub(pulls={"prosopo/Protect": [pr(n, assignees=["alice"]) for n in range(1, 151)]})
        rc, logs = sweep(gh, "--max-writes", "1000", repo="prosopo/Protect")
        self.assertEqual(rc, 0)
        self.assertEqual(events(logs, "run.done")[0]["synced"], 150)
        self.assertEqual([c[2] for c in gh.calls if c[0] == "list"], [None, "100"])

    def test_newest_prs_across_repos_go_first(self):
        gh = FakeGitHub(pulls={"prosopo/a": [pr(1, updated="2026-01-01T00:00:00Z")],
                               "prosopo/b": [pr(2, updated="2026-10-01T00:00:00Z")]})
        _, logs = run(gh, "--repo", "prosopo/a", "--repo", "prosopo/b")
        self.assertEqual([e["repo"] for e in events(logs, "pr.synced")], ["prosopo/b", "prosopo/a"])

    def test_dry_run_plans_without_writing(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(1), pr(2, state="MERGED", items=[item()])]})
        rc, logs = sweep(gh, "--dry-run")
        self.assertEqual((rc, gh.writes(), gh.sleeps), (0, [], []))
        self.assertEqual(events(logs, "run.planned")[0]["writes"], 4)
        self.assertEqual(events(logs, "pr.would_sync")[0]["steps"], ["add", "status:In progress 📊", "assign:alice"])

    def test_write_budget_stops_the_run_cleanly(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(n, assignees=["alice"]) for n in range(1, 6)]})
        rc, logs = sweep(gh, "--max-writes", "5")
        self.assertEqual(rc, 0)
        self.assertEqual(len(gh.writes()), 4)
        self.assertEqual(events(logs, "run.budget_spent")[0]["remaining_prs"], 3)

    def test_every_write_is_paced(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(1), pr(2, state="MERGED", items=[item()])]})
        sweep(gh, "--write-interval", "0.5")
        self.assertEqual(gh.sleeps, [0.5] * 4)

    def test_throttling_stops_the_run_without_failing_it(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(n, assignees=["alice"]) for n in range(1, 6)]}, throttle_after=2)
        rc, logs = sweep(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(len(events(logs, "run.throttled")), 1)
        self.assertEqual(events(logs, "run.done")[0]["synced"], 1)

    def test_one_failed_pr_does_not_stop_the_sweep(self):
        gh = FakeGitHub(pulls={"prosopo/docs": [pr(1), pr(2), pr(3)]}, fail_add_for={"PR2"})
        rc, logs = sweep(gh, repo="prosopo/docs")
        self.assertEqual(rc, 1)
        self.assertEqual([e["event"] for e in logs][2:], ["pr.synced", "pr.failed", "pr.synced", "run.done"])

    def test_one_failed_repo_does_not_stop_the_others(self):
        gh = FakeGitHub(pulls={"prosopo/docs": [pr(1)]}, fail_list_for={"prosopo/gone"})
        rc, logs = run(gh, "--repo", "prosopo/gone", "--repo", "prosopo/docs")
        self.assertEqual(rc, 1)
        self.assertEqual([e["event"] for e in logs],
                         ["run.start", "repo.failed", "run.planned", "pr.synced", "run.done"])

    def test_unknown_status_fails_before_any_write(self):
        gh = FakeGitHub(project={"id": "P1", "field": {"id": "F1", "options": OPTIONS[1:]}},
                        pulls={"prosopo/captcha": [pr(11)]})
        with self.assertRaises(RuntimeError):
            sweep(gh)
        self.assertEqual(gh.writes(), [])

    def test_missing_project_raises(self):
        with self.assertRaises(RuntimeError):
            pb.Board(_NoProject(), "prosopo", 4)

    def test_missing_status_field_raises(self):
        with self.assertRaises(RuntimeError):
            pb.Board(FakeGitHub(project={"id": "P1", "field": None}), "prosopo", 4)

    def test_rejects_bad_args(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pb.main(["--owner", "o", "--project-number", "x", "--open-status", "a"],
                    gh=FakeGitHub())


class _NoProject(FakeGitHub):
    def graphql(self, query, **v):
        return {"organization": {"projectV2": None}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body, headers=None):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", headers or {}, io.BytesIO(body))


class TransportTests(unittest.TestCase):
    def raising(self, err):
        def opener(req, timeout):
            raise err
        return pb.GitHub("t", opener=opener)

    def test_graphql_errors_raise(self):
        def opener(req, timeout):
            self.assertEqual(req.get_header("Authorization"), "Bearer t")
            return Resp(json.dumps({"errors": [{"message": "nope"}]}).encode())

        with self.assertRaises(RuntimeError):
            pb.GitHub("t", opener=opener).graphql("query{x}")

    def test_graphql_rate_limited_is_throttle(self):
        gh = pb.GitHub("t", opener=lambda req, timeout: Resp(json.dumps({"errors": [{"type": "RATE_LIMITED"}]}).encode()))
        with self.assertRaises(pb.Throttled):
            gh.graphql("query{x}")

    def test_secondary_rate_limit_403_is_throttle(self):
        with self.assertRaises(pb.Throttled):
            self.raising(http_error(403, b"You have exceeded a secondary rate limit")).request("POST", "/x", {})

    def test_429_is_throttle(self):
        with self.assertRaises(pb.Throttled):
            self.raising(http_error(429, b"")).request("POST", "/x", {})

    def test_403_with_retry_after_is_throttle(self):
        with self.assertRaises(pb.Throttled):
            self.raising(http_error(403, b"", {"Retry-After": "60"})).request("POST", "/x", {})

    def test_other_http_errors_are_plain_failures(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.raising(http_error(404, b"Not Found")).request("GET", "/x")
        self.assertNotIsInstance(ctx.exception, pb.Throttled)
        self.assertIn("404", str(ctx.exception))

    def test_empty_body_returns_empty_dict(self):
        self.assertEqual(pb.GitHub("t", opener=lambda req, timeout: Resp(b"")).request("POST", "/x", {}), {})


if __name__ == "__main__":
    unittest.main()
