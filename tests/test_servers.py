"""The same reports whichever way the request arrives, and from the queue."""
import asyncio
import io

from proper.controller import Controller
from proper.router import Route
from proper.test_client import HttpProtocolStub, make_test_scope

from proper_sentry import instrument


class PagesController(Controller):
    def explode(self):
        raise ValueError("boom")


def add_routes(app):
    app.router.add_route(Route(method="GET", path="/explode", to=PagesController.explode))


def test_rsgi(app, sentry_init):
    recorder = sentry_init(traces_sample_rate=1.0)
    instrument(app)
    add_routes(app)
    protocol = HttpProtocolStub()

    async def run():
        await app.startup()
        try:
            await app.__rsgi__(make_test_scope("/explode"), protocol)
        finally:
            await app.shutdown()

    asyncio.run(run())
    assert protocol.status == 500
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
