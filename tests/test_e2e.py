"""Send real events to a Sentry project and read them back through its API.

Skipped unless these are set:

- `SENTRY_DSN`: the DSN of a project for tests, where the events go.
- `SENTRY_AUTH_TOKEN`: a token with `org:read` and `project:read`, to read
  them back.
- `SENTRY_ORG`: the slug of the organization of that project.
- `SENTRY_API_URL` (optional): defaults to `https://sentry.io/api/0`.

Each run tags its events with a release of its own, so runs don't mix.
Sentry takes a while to make events searchable: this waits up to three
minutes for them.
"""
import asyncio
import json
import os
import time
import urllib.parse
import urllib.request

import peewee as pw
import pytest
import sentry_sdk
from proper import App, TestClient, current
from proper.controller import Controller
from proper.errors import NotFound
from proper.router import Route
from proper.test_client import HttpProtocolStub, make_test_scope

from proper_sentry import instrument


ENV_VARS = ("SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_ORG")

pytestmark = pytest.mark.skipif(
    not all(os.getenv(name) for name in ENV_VARS),
    reason=f"needs {', '.join(ENV_VARS)}",
)

RELEASE = f"proper-sentry-e2e-{int(time.time())}"
WAIT_SECONDS = 180

db = pw.SqliteDatabase(":memory:")


class Note(pw.Model):
    text = pw.TextField()

    class Meta:
        database = db


class User:
    id = 42
    email = "e2e@example.com"
    login = "e2e"


class PagesController(Controller):
    def explode(self):
        current.user = User()
        list(Note.select())
        raise ValueError(f"wsgi boom {RELEASE}")

    def explode_rsgi(self):
        raise RuntimeError(f"rsgi boom {RELEASE}")

    def missing(self):
        raise NotFound()

    def notes(self):
        return ", ".join(note.text for note in Note.select())


# ---- Sending ----


def send_everything(compiled_path: str) -> None:
    app = App(__name__, {
        "SECRET_KEYS": ["*" * 50],
        "DEBUG": False,
        "COMPILED_PATH": compiled_path,
        "SENTRY": {
            "dsn": os.environ["SENTRY_DSN"],
            "release": RELEASE,
            "environment": "e2e",
            "traces_sample_rate": 1.0,
            "send_default_pii": True,
        },
    })
    for path, action in (
        ("/explode", PagesController.explode),
        ("/explode-rsgi", PagesController.explode_rsgi),
        ("/missing", PagesController.missing),
        ("/notes", PagesController.notes),
    ):
        app.router.add_route(Route(method="GET", path=path, to=action))
    instrument(app)

    @app.queue.task()
    def e2e_failing_task():
        raise KeyError(f"huey boom {RELEASE}")

    db.connect(reuse_if_open=True)
    db.create_tables([Note])
    Note.create(text="first")

    client = TestClient(app)
    assert client.get("/explode?x=1").status == 500
    assert client.get("/missing").status == 404
    assert client.get("/notes").body == "first"
    e2e_failing_task()

    async def rsgi():
        await app.startup()
        try:
            protocol = HttpProtocolStub()
            await app.__rsgi__(make_test_scope("/explode-rsgi"), protocol)
            return protocol.status
        finally:
            await app.shutdown()

    assert asyncio.run(rsgi()) == 500
    sentry_sdk.flush(timeout=10)
    db.close()


# ---- Reading back ----


def api_get(path: str, **params) -> dict:
    base = os.getenv("SENTRY_API_URL", "https://sentry.io/api/0").rstrip("/")
    url = f"{base}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {os.environ['SENTRY_AUTH_TOKEN']}"}
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def project_id() -> str:
    """The last part of the DSN's path."""
    return urllib.parse.urlparse(os.environ["SENTRY_DSN"]).path.strip("/")


def query(dataset: str, fields: list[str], extra: str = "") -> list[dict]:
    return api_get(
        f"/organizations/{os.environ['SENTRY_ORG']}/events/",
        dataset=dataset,
        field=fields,
        query=f"release:{RELEASE} {extra}".strip(),
        project=project_id(),
        statsPeriod="1h",
    )["data"]


@pytest.fixture(scope="module")
def received(tmp_path_factory):
    """Send everything once, then wait until Sentry has all of it."""
    send_everything(str(tmp_path_factory.mktemp("compiled")))
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        errors = query("errors", ["id", "title", "transaction", "error.mechanism"])
        # Sentry stores transactions as spans now.
        spans = query(
            "spans",
            ["transaction", "span.op", "span.status", "span.description"],
        )
        if (len(errors) >= 3 and len(spans) >= 7) or time.monotonic() > deadline:
            return {"errors": errors, "spans": spans}
        time.sleep(10)


def find_error(received, text):
    return next(e for e in received["errors"] if text in e["title"])


def test_reports_server_errors_only(received):
    titles = sorted(e["title"].split(":")[0] for e in received["errors"])
    assert titles == ["KeyError", "RuntimeError", "ValueError"]


def test_wsgi_error(received):
    error = find_error(received, "wsgi boom")
    assert error["transaction"] == "tests.test_e2e.PagesController.explode"
    assert error["error.mechanism"] == ["proper"]

    org, pid = os.environ["SENTRY_ORG"], project_id()
    projects = api_get(f"/organizations/{org}/projects/")
    slug = next(p["slug"] for p in projects if p["id"] == pid)
    event = api_get(f"/projects/{org}/{slug}/events/{error['id']}/")
    entries = {entry["type"]: entry["data"] for entry in event["entries"]}

    assert entries["request"]["method"] == "GET"
    assert entries["request"]["url"].endswith("/explode")
    assert entries["request"]["query"] == [["x", "1"]]
    assert event["user"]["id"] == "42"
    assert event["user"]["email"] == "e2e@example.com"
    queries = [c for c in entries["breadcrumbs"]["values"] if c["category"] == "query"]
    assert queries[0]["message"].startswith("SELECT")


def test_rsgi_error(received):
    error = find_error(received, "rsgi boom")
    assert error["transaction"] == "tests.test_e2e.PagesController.explode_rsgi"
    assert error["error.mechanism"] == ["proper"]


def test_queue_error(received):
    error = find_error(received, "huey boom")
    assert error["error.mechanism"] == ["huey"]


def test_transactions(received):
    statuses = {
        span["transaction"].rsplit(".", 1)[-1]: span["span.status"]
        for span in received["spans"]
        if span["span.op"] == "http.server"
    }
    assert statuses == {
        "explode": "internal_error",
        "explode_rsgi": "internal_error",
        "missing": "not_found",
        "notes": "ok",
    }


def test_sql_spans(received):
    notes_queries = [
        span
        for span in received["spans"]
        if span["span.op"] == "db" and span["transaction"].endswith("notes")
    ]
    assert notes_queries[0]["span.description"].startswith("SELECT")
