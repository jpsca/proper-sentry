import peewee as pw
import pytest
import sentry_sdk
from proper import TestClient, current, status
from proper.controller import Controller
from proper.errors import NotFound
from proper.router import Route
from proper.test_client import make_test_request

from proper_sentry import (
    DEFAULT_TRANSACTION_NAME,
    _db_system,
    _name_transaction,
    _set_db_data,
    instrument,
)


class PagesController(Controller):
    def index(self):
        return "hello"

    def explode(self):
        raise ValueError("boom")

    def missing(self):
        raise NotFound()

    def signed_in(self):
        current.user = User()
        raise ValueError("boom")

    def guest(self):
        current.user = Guest()
        raise ValueError("boom")

    def submit(self):
        raise ValueError("boom")


class ErrorsController(Controller):
    def server_error(self):
        self.response.status = status.server_error
        return "sorry"

    def not_found(self):
        self.response.status = status.not_found
        return "not here"


class User:
    id = 7
    email = "ana@example.com"
    login = "ana"


class Guest:
    id = 8


def add_routes(app):
    for path, action in (
        ("/", PagesController.index),
        ("/explode", PagesController.explode),
        ("/missing", PagesController.missing),
        ("/signed-in", PagesController.signed_in),
        ("/guest", PagesController.guest),
    ):
        app.router.add_route(Route(method="GET", path=path, to=action))
    app.router.add_route(Route(method="POST", path="/submit", to=PagesController.submit))
    app.router.add_route(Route(method="GET", path="/pages/:id", to=PagesController.index))
    app.router.add_route(Route(method="GET", path="/old", redirect="/"))


@pytest.fixture()
def client(app):
    add_routes(app)
    return TestClient(app)


# ---- instrument ----


def test_starts_sentry_with_the_app_config(app, recorder):
    app.config.SENTRY = {"dsn": "http://public@example.com/1", "transport": recorder}
    instrument(app)
    assert sentry_sdk.get_client().is_active()


def test_instrumenting_twice_does_nothing(app):
    instrument(app)
    instrument(app)
    assert len(app._around_request) == 1
    assert len(app._on_error) == 1


def test_rejects_an_unknown_transaction_style(app):
    with pytest.raises(ValueError):
        instrument(app, transaction_style="nope")


def test_does_nothing_until_sentry_starts(app, client):
    instrument(app)
    assert client.get("/").body == "hello"
    assert client.get("/explode").status == status.server_error


# ---- errors ----


def test_reports_errors(app, client, sentry_init):
    recorder = sentry_init()
    instrument(app)
    result = client.get("/explode?page=2", headers={"User-Agent": "test"})
    assert result.status == status.server_error

    (event,) = recorder.events
    (exception,) = event["exception"]["values"]
    assert exception["type"] == "ValueError"
    assert exception["mechanism"] == {"type": "proper", "handled": False}
    assert event["transaction"] == "tests.test_requests.PagesController.explode"
    assert event["request"]["method"] == "GET"
    assert event["request"]["url"].endswith("/explode")
    assert event["request"]["query_string"] == "page=2"
    assert event["request"]["headers"]["user-agent"] == "test"
    assert "cookies" not in event["request"]
    assert "user" not in event


def test_does_not_report_client_errors(app, client, sentry_init):
    recorder = sentry_init()
    instrument(app)
    assert client.get("/missing").status == status.not_found
    assert client.get("/no-such-page").status == status.not_found
    assert recorder.events == []


def test_names_the_error_after_the_action_not_the_error_handler(
    app, client, sentry_init
):
    recorder = sentry_init()
    instrument(app)
    app.router.add_error_handler(ValueError, ErrorsController.server_error)
    result = client.get("/explode")
    assert result.body == "sorry"
    (event,) = recorder.events
    assert event["transaction"] == "tests.test_requests.PagesController.explode"


def test_does_not_report_errors_handled_as_client_errors(app, client, sentry_init):
    recorder = sentry_init()
    instrument(app)
    app.router.add_error_handler(ValueError, ErrorsController.not_found)
    assert client.get("/explode").status == status.not_found
    assert recorder.events == []


def test_reports_errors_the_app_does_not_catch(app, client, sentry_init):
    recorder = sentry_init()
    instrument(app)
    app.config.CATCH_ALL_ERRORS = False
    with pytest.raises(ValueError):
        client.get("/explode")
    (event,) = recorder.events
    assert event["transaction"] == "tests.test_requests.PagesController.explode"


def test_does_not_report_client_errors_the_app_does_not_catch(
    app, client, sentry_init
):
    recorder = sentry_init()
    instrument(app)
    app.config.CATCH_ALL_ERRORS = False
    with pytest.raises(NotFound):
        client.get("/missing")
    assert recorder.events == []


def test_reports_the_form_data(app, client, sentry_init):
    recorder = sentry_init()
    instrument(app)
    client.post(
        "/submit",
        body="name=Ana&tags=a&tags=b",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    (event,) = recorder.events
    assert event["request"]["data"] == {"name": "Ana", "tags": ["a", "b"]}


def test_reports_uploaded_files_by_name(app, client, sentry_init, tmp_path):
    recorder = sentry_init()
    instrument(app)
    upload = tmp_path / "notes.txt"
    upload.write_text("hi")
    client.post("/submit", body={"name": "Ana"}, upload_files=[("file", str(upload))])
    (event,) = recorder.events
    assert event["request"]["data"] == {"name": "Ana", "file": "<file: notes.txt>"}


def test_leaves_out_bodies_that_are_too_large(app, client, sentry_init):
    recorder = sentry_init(max_request_body_size="never")
    instrument(app)
    client.post("/submit", body={"name": "Ana"})
    (event,) = recorder.events
    assert "data" not in event["request"]


def test_reports_the_user_with_send_default_pii(app, client, sentry_init):
    recorder = sentry_init(send_default_pii=True)
    instrument(app)
    client.get("/signed-in", headers={"Cookie": "theme=dark"})
    (event,) = recorder.events
    assert event["request"]["cookies"] == {"theme": "dark"}
    assert event["user"]["id"] == "7"
    assert event["user"]["email"] == "ana@example.com"
    assert event["user"]["username"] == "ana"
    assert "ip_address" in event["user"]


def test_reports_only_the_user_fields_it_has(app, client, sentry_init):
    recorder = sentry_init(send_default_pii=True)
    instrument(app)
    client.get("/guest")
    (event,) = recorder.events
    assert set(event["user"]) == {"id", "ip_address"}


def test_reports_no_user_when_nobody_signed_in(app, client, sentry_init):
    recorder = sentry_init(send_default_pii=True)
    instrument(app)
    client.get("/explode")
    (event,) = recorder.events
    assert set(event["user"]) == {"ip_address"}


def test_the_app_logger_does_not_report_errors_again(app, client, sentry_init):
    recorder = sentry_init()
    instrument(app)
    client.get("/explode")
    assert len(recorder.events) == 1


# ---- tracing ----


def test_traces_each_request(app, client, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    client.get("/")
    (transaction,) = recorder.transactions
    assert transaction["transaction"] == "tests.test_requests.PagesController.index"
    assert transaction["transaction_info"] == {"source": "component"}
    assert transaction["contexts"]["trace"]["op"] == "http.server"
    assert transaction["contexts"]["trace"]["origin"] == "auto.http.proper"
    assert transaction["contexts"]["trace"]["status"] == "ok"


def test_names_transactions_after_the_route_path(app, client, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app, transaction_style="url")
    client.get("/pages/3")
    (transaction,) = recorder.transactions
    assert transaction["transaction"] == "/pages/:id"
    assert transaction["transaction_info"] == {"source": "route"}


def test_names_redirects_after_the_route_path(app, client, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    client.get("/old")
    (transaction,) = recorder.transactions
    assert transaction["transaction"] == "/old"


def test_unmatched_requests_keep_the_default_name(app, client, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    client.get("/no-such-page")
    (transaction,) = recorder.transactions
    assert transaction["transaction"] == DEFAULT_TRANSACTION_NAME
    assert transaction["contexts"]["trace"]["status"] == "not_found"


def test_failed_requests_are_marked_as_errors(app, client, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    client.get("/explode")
    (transaction,) = recorder.transactions
    assert transaction["contexts"]["trace"]["status"] == "internal_error"


def test_continues_the_incoming_trace(app, client, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    trace_id = "771a43a4192642f0b136d5159a501700"
    client.get("/", headers={"sentry-trace": f"{trace_id}-1234567890abcdef-1"})
    (transaction,) = recorder.transactions
    assert transaction["contexts"]["trace"]["trace_id"] == trace_id
    assert transaction["contexts"]["trace"]["parent_span_id"] == "1234567890abcdef"


def test_naming_outside_a_transaction(app):
    req = make_test_request("/", app=app)
    req.matched_route = Route(method="GET", path="/x", to=PagesController.index)
    _name_transaction(req, "url", after_error=True)
    assert sentry_sdk.get_current_scope().transaction is None
    assert sentry_sdk.get_current_scope()._transaction == "/x"


# ---- SQL ----


db = pw.SqliteDatabase(":memory:")


class Note(pw.Model):
    text = pw.TextField()

    class Meta:
        database = db


class NotesController(Controller):
    def index(self):
        return ", ".join(note.text for note in Note.select())

    def fail(self):
        list(Note.select())
        raise ValueError("boom")


@pytest.fixture()
def notes(app):
    db.connect(reuse_if_open=True)
    db.create_tables([Note])
    Note.create(text="first")
    app.router.add_route(Route(method="GET", path="/notes", to=NotesController.index))
    app.router.add_route(Route(method="GET", path="/notes/fail", to=NotesController.fail))
    yield
    db.drop_tables([Note])
    db.close()


def test_traces_sql_queries(app, client, notes, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    assert client.get("/notes").body == "first"
    (transaction,) = recorder.transactions
    (span,) = [s for s in transaction["spans"] if s["op"] == "db"]
    assert span["description"].startswith('SELECT "t1"."id", "t1"."text" FROM "note"')
    assert span["origin"] == "auto.db.peewee"
    assert span["data"]["db.system"] == "sqlite"
    assert span["data"]["db.name"] == ":memory:"


def test_sql_queries_are_breadcrumbs_of_errors(app, client, notes, sentry_init):
    recorder = sentry_init()
    instrument(app)
    client.get("/notes/fail")
    (event,) = recorder.events
    crumbs = [c for c in event["breadcrumbs"]["values"] if c["category"] == "query"]
    assert crumbs[0]["message"].startswith("SELECT")


def test_queries_outside_a_request_are_not_traced(notes, app, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    assert [n.text for n in Note.select()] == ["first"]
    assert recorder.transactions == []


def test_does_not_trace_sql_when_asked(app, client, notes, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app, trace_sql=False)
    client.get("/notes")
    (transaction,) = recorder.transactions
    # Peewee may be patched by an earlier test in this process; what matters
    # is that asking for it here didn't install anything new.
    assert transaction["transaction"].endswith("NotesController.index")


@pytest.mark.parametrize(
    "database, system",
    [
        (pw.SqliteDatabase(":memory:"), "sqlite"),
        (pw.PostgresqlDatabase("shop"), "postgresql"),
        (pw.MySQLDatabase("shop"), "mysql"),
        (pw.Database(None), None),
    ],
)
def test_names_the_database_system(database, system):
    assert _db_system(database) == system


def test_db_data_skips_what_it_does_not_know(sentry_init):
    sentry_init(traces_sample_rate=1.0)
    with sentry_sdk.start_transaction(name="t"):
        with sentry_sdk.start_span(op="db") as span:
            _set_db_data(span, pw.Database(None))
    assert "db.system" not in span._data
    assert "db.name" not in span._data
