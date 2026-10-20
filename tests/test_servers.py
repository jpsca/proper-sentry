"""The same reports whichever way the request arrives, and from the queue."""
import io

from bearings import Listener
from proper.controller import Controller
from proper.router import Route

from proper_sentry import instrument


class PagesController(Controller):
    def explode(self):
        raise ValueError("boom")


def add_routes(app):
    app.router.add_route(Route(method="GET", path="/explode", to=PagesController.explode))


def test_bearings(app, sentry_init):
    """The raw interface that `proper run` serves with Bearings."""
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    add_routes(app)
    # The head as Bearings sends it: method, path, query, version, client
    # address and port, then the headers, each field ended by NUL
    fields = ["GET", "/explode", "", "HTTP/1.1", "10.0.0.9", "5555", "Host", "example.com"]
    head = "".join(field + "\0" for field in fields)

    status, _, _ = app._bearings(head, io.BytesIO(), Listener("example.com", 80, "http"))
    assert status == 500
    (event,) = recorder.events
    assert event["transaction"] == "tests.test_servers.PagesController.explode"
    assert event["request"]["url"] == "http://example.com/explode"
    (transaction,) = recorder.transactions
    assert transaction["contexts"]["trace"]["status"] == "internal_error"


def test_wsgi(app, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    add_routes(app)
    statuses = []
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/explode",
        "QUERY_STRING": "",
        "SERVER_NAME": "example.com",
        "SERVER_PORT": "80",
        "HTTP_HOST": "example.com",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(),
    }
    app(environ, lambda status, headers: statuses.append(status))
    assert statuses == ["500 Internal Server Error"]
    (event,) = recorder.events
    assert event["transaction"] == "tests.test_servers.PagesController.explode"
    assert len(recorder.transactions) == 1


def test_queue_tasks_are_reported_by_sentry_huey_integration(app, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)

    @app.queue.task()
    def failing_task():
        raise ValueError("task boom")

    failing_task()
    (event,) = recorder.events
    (exception,) = event["exception"]["values"]
    assert exception["value"] == "task boom"
    assert exception["mechanism"]["type"] == "huey"
    (transaction,) = recorder.transactions
    assert transaction["transaction"].endswith("failing_task")
