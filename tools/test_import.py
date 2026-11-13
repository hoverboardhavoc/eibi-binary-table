"""Tests of tools/import.py: a dry run of the import against local copies of EiBi's CSV, with a fake HTTP fetch.
Nothing here contacts eibispace.de.
"""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")

_spec = importlib.util.spec_from_file_location("guide_import", os.path.join(HERE, "import.py"))
imp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(imp)

UTC = datetime.timezone.utc
B26_MODIFIED = "Sat, 31 Oct 2026 12:00:00 GMT"
B26_LATER = "Tue, 10 Nov 2026 08:00:00 GMT"
IDENTITY = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def rows(data):
    """The CSV's data rows, as raw lines."""
    return [l for l in data.split(b"\r\n")[1:] if l]


def with_rows(data, drop=(), add=()):
    """The CSV with the rows at the indexes in drop removed and the raw rows in add appended."""
    head = data.split(b"\r\n")[0]
    kept = [r for i, r in enumerate(rows(data)) if i not in drop] + list(add)
    return b"\r\n".join([head] + kept) + b"\r\n"


class FakeFetch:
    """Answers each GET with the next queued (status, headers, body) and records the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        return self.answers.pop(0)


def ok(body, modified=B26_MODIFIED, etag='"b26-1"'):
    return 200, {"etag": etag, "last-modified": modified}, body


class ImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = dict(os.environ)
        os.environ.update(IDENTITY)

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls.env)

    def setUp(self):
        base = os.path.join(ROOT, ".test-work")
        os.makedirs(base, exist_ok=True)
        self.repo = tempfile.mkdtemp(dir=base)
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args, env=None):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True,
                              env=dict(os.environ, **(env or {}))).stdout

    def seed(self, state, files=(), when=None):
        """The repository's first commit: state.json and files, committed at when."""
        imp.write_state(self.repo, state)
        for path, data in files:
            os.makedirs(os.path.dirname(os.path.join(self.repo, path)), exist_ok=True)
            with open(os.path.join(self.repo, path), "wb") as f:
                f.write(data)
        self.git("add", "-A")
        env = {"GIT_COMMITTER_DATE": when.isoformat(), "GIT_AUTHOR_DATE": when.isoformat()} if when else None
        self.git("commit", "-q", "-m", "Seed", env=env)

    def run_import(self, fetch, now, bounds=(1, 30000)):
        return imp.run(self.repo, now, fetch=fetch, bounds=bounds)

    def commits(self):
        return int(self.git("rev-list", "--count", "HEAD"))

    def read(self, path):
        with open(os.path.join(self.repo, path), "rb") as f:
            return f.read()

    def state(self):
        return json.loads(self.read("state.json"))

    def assert_clean(self):
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")

    def first_import(self, now=datetime.datetime(2026, 11, 11, 4, 37, tzinfo=UTC)):
        self.seed({"season": None, "etag": None, "last_modified": None, "checked": None})
        fetch = FakeFetch(ok(fixture("sked-b26.csv")))
        message = self.run_import(fetch, now)
        return fetch, message

    # ---- seasons ----

    def test_season_by_date(self):
        cases = {
            (2026, 3, 28): "B25", (2026, 3, 29): "A26",       # last Sunday of March 2026 is the 29th
            (2026, 10, 24): "A26", (2026, 10, 25): "B26",     # last Sunday of October 2026 is the 25th
            (2026, 12, 31): "B26", (2027, 1, 1): "B26",       # B runs into the next year
            (2027, 3, 27): "B26", (2027, 3, 28): "A27",
            (2027, 10, 30): "A27", (2027, 10, 31): "B27",
            (2099, 12, 31): "B99", (2100, 1, 1): "B99",
        }
        for (y, m, d), season in cases.items():
            self.assertEqual(imp.season_for(datetime.date(y, m, d)), season, f"{y}-{m}-{d}")

    def test_season_by_utc(self):
        # 23:30 on 24 Oct 2026 in UTC-2 is 01:30 on the 25th in UTC: B26 has started.
        self.seed({"season": "A26", "etag": None, "last_modified": None, "checked": None})
        fetch = FakeFetch((404, {}, b""))
        self.run_import(fetch, datetime.datetime(2026, 10, 24, 23, 30, tzinfo=datetime.timezone(
            datetime.timedelta(hours=-2))))
        self.assertTrue(fetch.requests[0][0].endswith("/sked-b26.csv"))

    # ---- the 200 path ----

    def test_first_import(self):
        fetch, message = self.first_import()
        self.assertEqual(len(fetch.requests), 1)
        url, headers = fetch.requests[0]
        self.assertEqual(url, "http://www.eibispace.de/dx/sked-b26.csv")
        self.assertEqual(headers, {"User-Agent": imp.USER_AGENT})
        manifest = json.loads(self.read("v1/manifest.json"))
        self.assertEqual(message, f"EiBi B26 of 31 Oct 2026: {manifest['entries']:,} entries")
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), message)
        self.assertEqual(self.read("eibi/sked-b26.csv"), fixture("sked-b26.csv"))
        table = self.read("v1/stations.bin")
        self.assertEqual(manifest["length"], len(table))
        self.assertEqual(manifest["csv"], "eibi/sked-b26.csv")
        self.assertEqual(manifest["source_modified"], "2026-10-31T12:00:00Z")
        self.assertEqual(self.state(), {"season": "B26", "etag": '"b26-1"', "last_modified": B26_MODIFIED,
                                        "checked": "2026-11-11T04:37:00Z"})
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(),
                         ["eibi/sked-b26.csv", "state.json", "v1/manifest.json", "v1/stations.bin"])
        self.assert_clean()

    def test_revision_counts_the_change(self):
        self.first_import()
        csv = fixture("sked-b26.csv")
        revised = with_rows(csv, drop={1, 4}, add=[b"6070;0000-2400;;CAN;Test Station;E;NAm;;;;"])
        fetch = FakeFetch(ok(revised, modified=B26_LATER, etag='"b26-2"'))
        message = self.run_import(fetch, datetime.datetime(2026, 11, 12, 4, 37, tzinfo=UTC))
        self.assertEqual(fetch.requests[0][1], {"User-Agent": imp.USER_AGENT, "If-None-Match": '"b26-1"',
                                                "If-Modified-Since": B26_MODIFIED})
        entries = json.loads(self.read("v1/manifest.json"))["entries"]
        self.assertEqual(message, f"EiBi B26 of 10 Nov 2026: {entries:,} entries (+1, -2)")
        self.assertEqual(self.read("eibi/sked-b26.csv"), revised)
        self.assertEqual(self.state()["etag"], '"b26-2"')
        self.assertEqual(self.commits(), 3)
        self.assert_clean()

    # ---- the 304 path ----

    def test_not_modified_commits_nothing(self):
        self.first_import()
        head = self.git("rev-parse", "HEAD")
        fetch = FakeFetch((304, {"etag": '"b26-1"'}, b""))
        message = self.run_import(fetch, datetime.datetime(2026, 11, 12, 4, 37, tzinfo=UTC))
        self.assertEqual(message, "EiBi B26: not modified")
        self.assertEqual(fetch.requests[0][1]["If-None-Match"], '"b26-1"')
        self.assertEqual(fetch.requests[0][1]["If-Modified-Since"], B26_MODIFIED)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assert_clean()

    # ---- a new season ----

    def test_new_season_moves_the_csv(self):
        a26 = fixture("sked-b26.csv")
        self.seed({"season": "A26", "etag": '"a26-9"', "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT",
                   "checked": "2026-10-24T04:37:00Z"}, files=[("eibi/sked-a26.csv", a26)])
        b26 = with_rows(a26, drop={4}, add=[b"6070;0000-2400;;CAN;Test Station;E;NAm;;;;"])
        fetch = FakeFetch(ok(b26))
        message = self.run_import(fetch, datetime.datetime(2026, 10, 25, 4, 37, tzinfo=UTC))
        url, headers = fetch.requests[0]
        self.assertTrue(url.endswith("/sked-b26.csv"))
        self.assertEqual(headers, {"User-Agent": imp.USER_AGENT})
        self.assertTrue(message.endswith(" entries (+1, -1)"), message)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "eibi/sked-a26.csv")))
        self.assertEqual(self.read("eibi/sked-b26.csv"), b26)
        status = self.git("diff", "--name-status", "-M", "HEAD~1", "HEAD").splitlines()
        self.assertIn("R", [l.split("\t")[0][0] for l in status])
        self.assertIn("eibi/sked-a26.csv\teibi/sked-b26.csv", "\n".join(status))
        self.assertEqual(self.git("log", "--follow", "--format=%s", "--", "eibi/sked-b26.csv").splitlines(),
                         [message, "Seed"])
        self.assertEqual(self.state()["season"], "B26")
        self.assert_clean()

    def test_new_season_not_published_keeps_the_old(self):
        self.seed({"season": "A26", "etag": '"a26-9"', "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT",
                   "checked": "2026-10-24T04:37:00Z"}, files=[("eibi/sked-a26.csv", fixture("sked-a26.csv"))])
        head = self.git("rev-parse", "HEAD")
        fetch = FakeFetch((404, {}, b""))
        message = self.run_import(fetch, datetime.datetime(2026, 10, 25, 4, 37, tzinfo=UTC))
        self.assertEqual(message, "EiBi B26 not published yet, A26 kept")
        self.assertEqual(len(fetch.requests), 1)
        self.assertEqual(fetch.requests[0][1], {"User-Agent": imp.USER_AGENT})
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.state()["season"], "A26")
        self.assert_clean()

    # ---- failing checks ----

    def assert_fails_and_commits_nothing(self, answer, bounds=(1, 30000)):
        self.first_import()
        head = self.git("rev-parse", "HEAD")
        before = {p: self.read(p) for p in ("eibi/sked-b26.csv", "v1/stations.bin", "v1/manifest.json",
                                             "state.json")}
        with self.assertRaises(imp.CheckFailed):
            self.run_import(FakeFetch(answer), datetime.datetime(2026, 11, 12, 4, 37, tzinfo=UTC), bounds=bounds)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual({p: self.read(p) for p in before}, before)
        self.assert_clean()

    def test_converter_error_commits_nothing(self):
        bad = with_rows(fixture("sked-b26.csv"), add=[b"6070;0000-2500;;CAN;Bad Time;E;NAm;;;;"])
        self.assert_fails_and_commits_nothing(ok(bad, modified=B26_LATER))

    def test_too_few_entries_commits_nothing(self):
        self.assert_fails_and_commits_nothing(ok(fixture("sked-b26.csv"), modified=B26_LATER),
                                              bounds=(imp.MIN_ENTRIES, imp.MAX_ENTRIES))

    def test_too_many_entries_commits_nothing(self):
        self.assert_fails_and_commits_nothing(ok(fixture("sked-b26.csv"), modified=B26_LATER), bounds=(1, 10))

    def test_server_error_commits_nothing(self):
        self.assert_fails_and_commits_nothing((500, {}, b""))

    def test_empty_body_commits_nothing(self):
        self.assert_fails_and_commits_nothing((200, {}, b""))

    # ---- keeping the schedule alive ----

    def keepalive_case(self, days):
        last = datetime.datetime(2026, 11, 1, 4, 37, tzinfo=UTC)
        state = {"season": "B26", "etag": '"b26-1"', "last_modified": B26_MODIFIED,
                 "checked": "2026-11-01T04:37:00Z"}
        self.seed(state, when=last)
        now = last + datetime.timedelta(days=days)
        message = self.run_import(FakeFetch((304, {}, b"")), now)
        return state, now, message

    def test_keepalive_after_30_days(self):
        state, now, message = self.keepalive_case(31)
        self.assertEqual(self.commits(), 2)
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["state.json"])
        self.assertEqual(self.state(), dict(state, checked=imp.iso(now)))
        self.assertIn("committed: EiBi B26 checked 2026-12-02T04:37:00Z: no change", message)
        self.assert_clean()

    def test_no_keepalive_within_30_days(self):
        self.keepalive_case(29)
        self.assertEqual(self.commits(), 1)
        self.assert_clean()

    def test_keepalive_while_waiting_for_a_new_season(self):
        last = datetime.datetime(2026, 9, 20, 4, 37, tzinfo=UTC)
        self.seed({"season": "A26", "etag": None, "last_modified": None, "checked": None}, when=last)
        self.run_import(FakeFetch((404, {}, b"")), datetime.datetime(2026, 10, 25, 4, 37, tzinfo=UTC))
        self.assertEqual(self.commits(), 2)
        self.assertEqual(self.state()["season"], "A26")


if __name__ == "__main__":
    unittest.main()
