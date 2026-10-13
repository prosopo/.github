import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import project_board as pb

OPTIONS = [{"id": "opt-prog", "name": "In progress 📊"}, {"id": "opt-done", "name": "Done ✅"}]


class FakeGitHub:
    def __init__(self, project=None, pulls=None, fail_add_for=()):
        self.project = project if project is not None else {
            "id": "P1", "field": {"id": "F1", "options": OPTIONS}}
        self.pulls = pulls or {}
        self.fail_add_for = set(fail_add_for)
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
            page = int(rest.split("page=")[-1])
            items = self.pulls.get(repo, [])
            return items[(page - 1) * 100: page * 100]
        return {}

    def mutations(self, name):
        return [c[2] for c in self.calls if c[0] == "graphql" and name in str(c)]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def pr(n, state="open", login="alice", kind="User", assignees=()):
    return {"number": n, "node_id": f"PR{n}", "state": state,
            "user": {"login": login, "type": kind}, "assignees": [{"login": a} for a in assignees]}


def run(gh, *args):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = pb.main(["--owner", "prosopo", "--project-number", "4",
                      "--open-status", "In progress 📊", "--closed-status", "Done ✅", *args], gh=gh)
    return rc, [json.loads(line) for line in out.getvalue().splitlines()]


def event_file(payload):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(payload, f)
    f.close()
    return f.name


class SyncTests(unittest.TestCase):
    def sync_event(self, gh, p, repo="prosopo/captcha"):
        path = event_file({"repository": {"full_name": repo}, "pull_request": p})
        try:
            return run(gh, "--event-path", path)
        finally:
            os.unlink(path)

    def test_open_pr_added_in_progress_and_author_assigned(self):
        gh = FakeGitHub()
        rc, logs = self.sync_event(gh, pr(7))
        self.assertEqual(rc, 0)
        status = [c[2] for c in gh.calls if "option" in c[2]]
        self.assertEqual(status[0]["option"], "opt-prog")
        self.assertEqual(status[0]["item"], "item-PR7")
        self.assertEqual(gh.posts(), [("POST", "/repos/prosopo/captcha/issues/7/assignees", {"assignees": ["alice"]})])
        self.assertTrue(logs[0]["assigned"])

    def test_closed_pr_moves_to_done_without_assigning(self):
        gh = FakeGitHub()
        rc, _ = self.sync_event(gh, pr(8, state="closed"))
        self.assertEqual(rc, 0)
        self.assertEqual([c[2]["option"] for c in gh.calls if "option" in c[2]], ["opt-done"])
        self.assertEqual(gh.posts(), [])

    def test_bot_author_not_assigned(self):
        gh = FakeGitHub()
        self.sync_event(gh, pr(9, login="dependabot[bot]", kind="Bot"))
        self.assertEqual(gh.posts(), [])

    def test_already_assigned_author_not_reassigned(self):
        gh = FakeGitHub()
        self.sync_event(gh, pr(10, assignees=["alice", "bob"]))
        self.assertEqual(gh.posts(), [])

    def test_backfill_pages_through_open_prs(self):
        pulls = [pr(n) for n in range(1, 151)]
        gh = FakeGitHub(pulls={"prosopo/Protect": pulls})
        rc, logs = run(gh, "--backfill-repo", "prosopo/Protect")
        self.assertEqual(rc, 0)
        self.assertEqual(logs[-1], {**logs[-1], "event": "run.done", "synced": 150, "failed": 0})
        self.assertEqual(len(gh.posts()), 150)

    def test_backfill_keeps_going_after_one_failure(self):
        gh = FakeGitHub(pulls={"prosopo/docs": [pr(1), pr(2), pr(3)]}, fail_add_for={"PR2"})
        rc, logs = run(gh, "--backfill-repo", "prosopo/docs")
        self.assertEqual(rc, 1)
        self.assertEqual([entry["event"] for entry in logs], ["pr.synced", "pr.failed", "pr.synced", "run.done"])

    def test_unknown_status_fails(self):
        gh = FakeGitHub(project={"id": "P1", "field": {"id": "F1", "options": OPTIONS[:1]}})
        rc, logs = self.sync_event(gh, pr(11, state="closed"))
        self.assertEqual(rc, 1)
        self.assertIn("Done", logs[0]["error"])

    def test_missing_project_raises(self):
        with self.assertRaises(RuntimeError):
            pb.Board(_NoProject(), "prosopo", 4)

    def test_missing_status_field_raises(self):
        with self.assertRaises(RuntimeError):
            pb.Board(FakeGitHub(project={"id": "P1", "field": None}), "prosopo", 4)

    def test_needs_a_target(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
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
