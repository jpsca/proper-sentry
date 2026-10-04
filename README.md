# proper-sentry

Report the errors and the performance of a [Proper](https://properproject.org) app to [Sentry](https://sentry.io).

```bash
uv add proper-sentry
```

## Usage

Add a `SENTRY` dict to your config. It takes the same options as `sentry_sdk.init()`:

```python
SENTRY = {
    "dsn": "https://<key>@<org>.ingest.sentry.io/<project>",
    "environment": "production",
    "traces_sample_rate": 0.1,
}
```

Then instrument the app, right after creating it:

```python
from proper import App
from proper_sentry import instrument

app = App(__name__, config=config)
instrument(app)
```

Leave `SENTRY` out of your development config and nothing is reported.

If you'd rather start Sentry yourself, call `sentry_sdk.init(...)` and then `instrument(app)` without a `SENTRY` config.

## What gets reported

- **Errors**: every request that ends in a server error (5xx), with the request's method, URL, headers and form fields. Errors that become a 4xx page (not found, forbidden, invalid CSRF token...) aren't reported.
- **Performance**: each request is a transaction, named after its controller action (`myapp.controllers.posts.PostsController.show`), with a span for every SQL query. Traces from other services continue through the `sentry-trace` and `baggage` headers.
- **Background tasks**: Sentry's own Huey integration reports the tasks of the queue. It turns on by itself.

With `"send_default_pii": True` in `SENTRY`, the reports also include the cookies, the IP address, and the `id`, `email` and `login` of `current.user`.

The values of Proper's session and sign-in cookies (`_session` and `_auth`), and of any form field with "password" in its name, are never sent, whatever the data scrubbing settings of your Sentry project.

## Options

```python
instrument(app, transaction_style="url", trace_sql=False)
```

- `transaction_style`: `"endpoint"` (the default) names transactions after the controller action; `"url"` uses the route path (`/posts/:id`).
- `trace_sql`: set to `False` to skip the SQL spans and breadcrumbs.

## Not supported yet

- WebSocket channels.
- Sentry's span streaming mode (`trace_lifecycle="stream"`): errors are still reported, but no transactions are sent.

## Development

```bash
make install
make test
make lint
```

`tests/test_e2e.py` sends real events to a Sentry project and reads them back through the Sentry API. It's skipped unless these environment variables are set:

```bash
export SENTRY_DSN=...         # DSN of a project used only for tests
export SENTRY_AUTH_TOKEN=...  # a token with org:read and project:read
export SENTRY_ORG=...         # the organization slug
make e2e
```

It takes a minute or two, while Sentry indexes the events.
