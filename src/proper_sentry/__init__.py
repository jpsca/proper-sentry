"""Sentry for Proper apps.

```python
from proper_sentry import instrument

app = App(__name__, config=config)
instrument(app)
```

`instrument` reports to Sentry the errors of every request, with the data of
the request, and traces each one as an `http.server` transaction with spans
for its SQL queries. The tasks of the queue (Huey) are reported by Sentry's
own Huey integration, which turns on by itself.
"""
import functools
import typing as t

import peewee
import sentry_sdk
from proper.constants import AUTH_COOKIE_NAME, SESSION_COOKIE_NAME
from proper.global_context import current
from proper.helpers import logger
from sentry_sdk.consts import OP, SPANDATA
from sentry_sdk.integrations._wsgi_common import (
    _filter_headers,
    request_body_within_bounds,
)
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.scope import should_send_default_pii
from sentry_sdk.sessions import track_session
from sentry_sdk.tracing import Span, TransactionSource
from sentry_sdk.tracing_utils import record_sql_queries
from sentry_sdk.utils import (
    AnnotatedValue,
    capture_internal_exceptions,
    event_from_exception,
    transaction_from_function,
)


if t.TYPE_CHECKING:
    from collections.abc import Callable

    from proper import App
    from proper.core.request import Request
    from proper.core.response import Response
    from sentry_sdk._types import Event, EventProcessor
    from sentry_sdk.tracing import Transaction


__all__ = ("instrument",)

ORIGIN = "auto.http.proper"
DB_ORIGIN = "auto.db.peewee"

# The name of a transaction until its route is known, and of those that
# never match one, like a 404.
DEFAULT_TRANSACTION_NAME = "generic Proper request"

TRANSACTION_STYLES = ("endpoint", "url")

# Cookies that let whoever has them act as the user. Their values are never
# sent, whatever the data scrubbing settings of the Sentry project.
SECRET_COOKIES = (SESSION_COOKIE_NAME, AUTH_COOKIE_NAME)

_INSTRUMENTED_ATTR = "_sentry_instrumented"


def instrument(
    app: "App",
    *,
    transaction_style: str = "endpoint",
    trace_sql: bool = True,
) -> None:
    """Report the errors and the performance of `app` to Sentry.

    If the app config has a `SENTRY` dict, Sentry is started with it
    (`sentry_sdk.init(**config.SENTRY)`). Otherwise, call `sentry_sdk.init()`
    yourself; until then, this does nothing.

    Arguments:
        app:
            The Proper app.
        transaction_style:
            How to name each request in Sentry: `"endpoint"` uses the
            controller action (`myapp.controllers.posts.PostsController.show`),
            `"url"` uses the route path (`/posts/:id`).
        trace_sql:
            Add a span and a breadcrumb for each SQL query.

    Calling it again on the same app does nothing.
    """
    if transaction_style not in TRANSACTION_STYLES:
        raise ValueError(
            f"Invalid transaction_style: {transaction_style!r}"
            f" (must be one of {TRANSACTION_STYLES})"
        )
    if getattr(app, _INSTRUMENTED_ATTR, False):
        return
    setattr(app, _INSTRUMENTED_ATTR, True)

    config = app.config.get("SENTRY")
    if config:
        sentry_sdk.init(**config)

    # The app logs every error it catches. Sentry's logging integration would
    # report each one a second time, as a message without the traceback.
    ignore_logger(logger.name)

    if trace_sql:
        _patch_peewee()

    @app.on_error
    def sentry_name_transaction() -> None:
        _name_transaction_on_error(transaction_style)

    @app.around_request
    def sentry_around_request(request, response, call_next):
        return _around_request(transaction_style, request, response, call_next)


def _around_request(
    transaction_style: str,
    request: "Request",
    response: "Response",
    call_next: "Callable[[Request, Response], Response]",
) -> "Response":
    if not sentry_sdk.get_client().is_active():
        return call_next(request, response)

    with sentry_sdk.isolation_scope() as scope, track_session(scope, session_mode="request"):
        with capture_internal_exceptions():
            scope.clear_breadcrumbs()
            scope._name = "proper"
            scope.add_event_processor(_make_event_processor(request))

        transaction = sentry_sdk.continue_trace(
            dict(request.headers),
            op=OP.HTTP_SERVER,
            name=DEFAULT_TRANSACTION_NAME,
            source=TransactionSource.ROUTE,
            origin=ORIGIN,
        )
        with sentry_sdk.start_transaction(
            transaction, custom_sampling_context={"proper_request": request}
        ):
            try:
                response = call_next(request, response)
            except Exception as error:
                # The app doesn't catch errors when `CATCH_ALL_ERRORS` is off.
                _name_transaction(request, transaction_style, after_error=True)
                _capture(error, getattr(error, "status", 500))
                raise

            _name_transaction(
                request, transaction_style, after_error=response.error is not None
            )
            if response.error is not None:
                _capture(response.error, response.status)
            _set_status(transaction, response.status)
            return response


def _name_transaction_on_error(transaction_style: str) -> None:
    """Name the transaction while the request still points to the route that
    failed: a custom error handler takes its place right after this."""
    if sentry_sdk.get_client().is_active():
        _name_transaction(current.request, transaction_style)


def _name_transaction(
    request: "Request", transaction_style: str, *, after_error: bool = False
) -> None:
    """Name the transaction after the matched route.

    With `after_error`, a name given before (by `_name_transaction_on_error`)
    is kept, since by then the route may point to the error handler.
    """
    scope = sentry_sdk.get_current_scope()
    if after_error and scope.transaction is not None:
        if scope.transaction.name != DEFAULT_TRANSACTION_NAME:
            return

    route = request.matched_route
    if route is None:
        return
    if transaction_style == "endpoint" and route.to is not None:
        name = transaction_from_function(route.to) or route.path
        source = TransactionSource.COMPONENT
    else:
        name = route.path
        source = TransactionSource.ROUTE
    scope.set_transaction_name(name, source=source)


def _set_status(transaction: "Transaction", status: int) -> None:
    with capture_internal_exceptions():
        transaction.set_http_status(status)


def _capture(error: BaseException, status: int) -> None:
    """Report an error, unless it became a response that isn't a server
    error, like a 404 or a 403."""
    if status < 500:
        return
    event, hint = event_from_exception(
        error,
        client_options=sentry_sdk.get_client().options,
        mechanism={"type": "proper", "handled": False},
    )
    sentry_sdk.capture_event(event, hint=hint)


def _make_event_processor(request: "Request") -> "EventProcessor":
    """Add the data of the request to every event reported during it."""
    method = request.request_method
    url = f"{request.scheme}://{request.host_with_port}{request.path}"
    query_string = request.query_string
    headers = _filter_headers(dict(request.headers))

    def event_processor(event: "Event", hint: dict[str, t.Any]) -> "Event":
        with capture_internal_exceptions():
            info = event.setdefault("request", {})
            info["method"] = method
            info["url"] = url
            info["query_string"] = query_string
            info["headers"] = headers

            client = sentry_sdk.get_client()
            if request.form and request_body_within_bounds(client, len(request.body)):
                info["data"] = _form_data(request)

            if should_send_default_pii():
                info["cookies"] = _cookies(request)
                info["env"] = {"REMOTE_ADDR": request.remote_ip}
                user = event.setdefault("user", {})
                user.setdefault("ip_address", request.remote_ip)
                _add_current_user(user)

        return event

    return event_processor


def _cookies(request: "Request") -> dict[str, t.Any]:
    return {
        name: _filtered() if name in SECRET_COOKIES else value
        for name, value in request.cookies.items()
    }


def _form_data(request: "Request") -> dict[str, t.Any]:
    """The submitted fields, one value each unless the field was sent more
    than once. Uploaded files are listed by name only, and the value of any
    field with "password" in its name is never sent."""
    data = {}
    for name, values in request.form.items():
        if "password" in name.lower():
            data[name] = _filtered()
            continue
        values = [
            v if isinstance(v, str) else f"<file: {getattr(v, 'filename', '')}>"
            for v in values
        ]
        data[name] = values[0] if len(values) == 1 else values
    return data


def _filtered() -> AnnotatedValue:
    """What Sentry shows as `[Filtered]`."""
    return AnnotatedValue.substituted_because_contains_sensitive_data()


def _add_current_user(user: dict[str, t.Any]) -> None:
    current_user = current.user
    if current_user is None:
        return
    for key, attr in (("id", "id"), ("email", "email"), ("username", "login")):
        value = getattr(current_user, attr, None)
        if value is not None:
            user.setdefault(key, str(value))


# ---- SQL ----

_peewee_patched = False


def _patch_peewee() -> None:
    """Record every query sent through Peewee. Only once per process: the
    patch is on the `Database` class, shared by every app."""
    global _peewee_patched
    if _peewee_patched:
        return
    _peewee_patched = True

    original = peewee.Database.execute_sql

    @functools.wraps(original)
    def execute_sql(self, sql, params=None, *args, **kwargs):
        # Outside of a request or a task there is nothing to attach it to.
        if sentry_sdk.get_current_span() is None:
            return original(self, sql, params, *args, **kwargs)

        with record_sql_queries(
            cursor=None,
            query=sql,
            params_list=params,
            paramstyle="qmark" if self.param == "?" else "format",
            executemany=False,
            span_origin=DB_ORIGIN,
        ) as span:
            _set_db_data(span, self)
            return original(self, sql, params, *args, **kwargs)

    peewee.Database.execute_sql = execute_sql  # ty: ignore[invalid-assignment]


def _set_db_data(span: Span, db: peewee.Database) -> None:
    with capture_internal_exceptions():
        system = _db_system(db)
        if system:
            span.set_data(SPANDATA.DB_SYSTEM, system)
        if db.database:
            span.set_data(SPANDATA.DB_NAME, str(db.database))


def _db_system(db: peewee.Database) -> str | None:
    if isinstance(db, peewee.SqliteDatabase):
        return "sqlite"
    if isinstance(db, peewee.PostgresqlDatabase):
        return "postgresql"
    if isinstance(db, peewee.MySQLDatabase):
        return "mysql"
    return None
