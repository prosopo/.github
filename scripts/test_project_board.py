import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

import project_board as pb

OPTIONS = [{"id": "opt-prog", "name": "In progress 📊"}, {"id": "opt-done", "name": "Done ✅"}]


class FakeGitHub:
    def __init__(self, project=None, pulls=None, fail_add_for=(), fail_list_for=()):
        self.project = project if project is not None else {
            "id": "P1", "field": {"id": "F1", "options": OPTIONS}}
        self.pulls = pulls or {}
        self.fail_add_for = set(fail_add_for)
        self.fail_list_for = set(fail_list_for)
        self.calls = []

    def graphql(self, query, **v):
        self.calls.append(("graphql", query.split("(")[0].split()[-1] if "mutation" in query else "project", v))
        if "projectV2(number" in query:
            return {"organization": {"projectV2": self.project}}
        if "addProjectV2ItemById" in query:
            if v["content"] in self.fail_add_for:
                raise RuntimeError("boom")
            return {"addProjectV2ItemById": {"item": {"id": "item-" + v["content"]}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["item"]}}}
        raise AssertionError(query)

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            repo, _, rest = path[len("/repos/"):].partition("/pulls")
            if repo in self.fail_list_for:
                raise RuntimeError("404")
            page = int(rest.split("page=")[-1])
            items = self.pulls.get(repo, [])
            return items[(page - 1) * 100: page * 100]
        return {}

    def mutations(self, name):
        return [c[2] for c in self.calls if c[0] == "graphql" and name in str(c)]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


NOW = 1_800_000_000


def stamp(minutes_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))


def pr(n, state="open", login="alice", kind="User", assignees=(), minutes_ago=1):
    return {"number": n, "node_id": f"PR{n}", "state": state, "updated_at": stamp(minutes_ago),
            "user": {"login": login, "type": kind}, "assignees": [{"login": a} for a in assignees]}


def run(gh, *args):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = pb.main(["--owner", "prosopo", "--project-number", "4",
                      "--open-status", "In progress 📊", "--closed-status", "Done ✅", *args], gh=gh, now=NOW)
    return rc, [json.loads(line) for line in out.getvalue().splitlines()]


def statuses(gh):
    return [c[2]["option"] for c in gh.calls if c[0] == "graphql" and "option" in c[2]]


class SweepTests(unittest.TestCase):
    def sweep(self, gh, *args, repo="prosopo/captcha"):
        return run(gh, "--repo", repo, *args)

    def test_open_pr_added_in_progress_and_author_assigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(7)]})
        rc, logs = self.sweep(gh)
        self.assertEqual(rc, 0)
        self.assertEqual(statuses(gh), ["opt-prog"])
        self.assertEqual(gh.posts(), [("POST", "/repos/prosopo/captcha/issues/7/assignees", {"assignees": ["alice"]})])
        self.assertTrue(logs[0]["assigned"])

    def test_closed_pr_in_window_moves_to_done_without_assigning(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(8, state="closed")]})
        rc, _ = self.sweep(gh, "--since-minutes", "30")
        self.assertEqual(rc, 0)
        self.assertEqual(statuses(gh), ["opt-done"])
        self.assertEqual(gh.posts(), [])

    def test_window_stops_at_first_older_pr(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(1, minutes_ago=5), pr(2, minutes_ago=29), pr(3, minutes_ago=31)]})
        rc, logs = self.sweep(gh, "--since-minutes", "30")
        self.assertEqual([entry.get("pr") for entry in logs if entry["event"] == "pr.synced"], [1, 2])
        self.assertIn("state=all", [c for c in gh.calls if c[0] == "GET"][0][1])

    def test_no_window_lists_only_open_prs(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(1, minutes_ago=10_000)]})
        self.sweep(gh)
        self.assertIn("state=open", [c for c in gh.calls if c[0] == "GET"][0][1])
        self.assertEqual(statuses(gh), ["opt-prog"])

    def test_bot_author_not_assigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(9, login="dependabot[bot]", kind="Bot")]})
        self.sweep(gh)
        self.assertEqual(gh.posts(), [])

    def test_already_assigned_author_not_reassigned(self):
        gh = FakeGitHub(pulls={"prosopo/captcha": [pr(10, assignees=["alice", "bob"])]})
        self.sweep(gh)
        self.assertEqual(gh.posts(), [])

    def test_pages_through_open_prs(self):
        gh = FakeGitHub(pulls={"prosopo/Protect": [pr(n) for n in range(1, 151)]})
        rc, logs = self.sweep(gh, repo="prosopo/Protect")
        self.assertEqual(rc, 0)
        self.assertEqual((logs[-1]["event"], logs[-1]["synced"], logs[-1]["failed"]), ("run.done", 150, 0))
        self.assertEqual(len(gh.posts()), 150)

    def test_one_failed_pr_does_not_stop_the_sweep(self):
        gh = FakeGitHub(pulls={"prosopo/docs": [pr(1), pr(2), pr(3)]}, fail_add_for={"PR2"})
        rc, logs = self.sweep(gh, repo="prosopo/docs")
        self.assertEqual(rc, 1)
        self.assertEqual([entry["event"] for entry in logs], ["pr.synced", "pr.failed", "pr.synced", "run.done"])

    def test_one_failed_repo_does_not_stop_the_others(self):
        gh = FakeGitHub(pulls={"prosopo/docs": [pr(1)]}, fail_list_for={"prosopo/gone"})
        rc, logs = run(gh, "--repo", "prosopo/gone", "--repo", "prosopo/docs")
        self.assertEqual(rc, 1)
        self.assertEqual([entry["event"] for entry in logs], ["repo.failed", "pr.synced", "run.done"])

    def test_unknown_status_fails(self):
        gh = FakeGitHub(project={"id": "P1", "field": {"id": "F1", "options": OPTIONS[:1]}},
                        pulls={"prosopo/captcha": [pr(11, state="closed")]})
        rc, logs = self.sweep(gh, "--since-minutes", "30")
        self.assertEqual(rc, 1)
        self.assertIn("Done", logs[0]["error"])

    def test_missing_project_raises(self):
        with self.assertRaises(RuntimeError):
            pb.Board(_NoProject(), "prosopo", 4)

    def test_missing_status_field_raises(self):
        with self.assertRaises(RuntimeError):
            pb.Board(FakeGitHub(project={"id": "P1", "field": None}), "prosopo", 4)

    def test_needs_a_repo(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pb.main(["--owner", "o", "--project-number", "1", "--open-status", "a", "--closed-status", "b"],
                    gh=FakeGitHub())


class _NoProject(FakeGitHub):
    def graphql(self, query, **v):
        return {"organization": {"projectV2": None}}


class TransportTests(unittest.TestCase):
    def test_graphql_errors_raise(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            self.assertEqual(req.get_header("Authorization"), "Bearer t")
            return Resp(json.dumps({"errors": [{"message": "nope"}]}).encode())

        with self.assertRaises(RuntimeError):
            pb.GitHub("t", opener=opener).graphql("query{x}")

    def test_empty_body_returns_empty_dict(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        self.assertEqual(pb.GitHub("t", opener=lambda req, timeout: Resp(b"")).request("POST", "/x", {}), {})


if __name__ == "__main__":
    unittest.main()
