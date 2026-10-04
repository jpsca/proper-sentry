import pytest
import sentry_sdk
from proper import App, current
from sentry_sdk.transport import Transport


class Recorder(Transport):
    """Keeps what Sentry would send, instead of sending it."""

    def __init__(self, options=None):
        super().__init__(options)
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    @property
    def events(self):
        return [
            env.get_event() for env in self.envelopes if env.get_event() is not None
        ]

    @property
    def transactions(self):
        return [
            env.get_transaction_event()
            for env in self.envelopes
            if env.get_transaction_event() is not None
        ]


@pytest.fixture(autouse=True)
def _compiled_views(tmp_path, monkeypatch):
    """Compile the views of the test apps into a folder of their own."""
    from proper.core import config

    monkeypatch.setitem(config.default_config, "COMPILED_PATH", str(tmp_path / "_compiled"))


@pytest.fixture(autouse=True)
def _reset_sentry():
    yield
    sentry_sdk.get_client().close()
    for scope in (
        sentry_sdk.get_global_scope(),
        sentry_sdk.get_isolation_scope(),
        sentry_sdk.get_current_scope(),
    ):
        scope.clear()
        scope.set_client(None)


@pytest.fixture()
def recorder():
    return Recorder()


@pytest.fixture()
def sentry_init(recorder):
    def init(**options):
        sentry_sdk.init(
            dsn="http://public@example.com/1",
            transport=recorder,
            **options,
        )
        return recorder

    return init


@pytest.fixture()
def app():
    app = App(__name__, {"SECRET_KEYS": ["*" * 50], "DEBUG": False})
    current.app = app
    return app
