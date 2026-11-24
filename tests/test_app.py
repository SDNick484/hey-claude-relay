import logging

import pytest
from starlette.routing import Route

from conftest import client_for
from hey_claude_relay import __version__
from hey_claude_relay.app import create_app
from hey_claude_relay.errors import RelayError, error_response


@pytest.fixture
def app_that_raises(cfg, secrets):
    app = create_app(cfg, secrets)

    async def boom(request):
        raise RuntimeError("kaboom with detail")

    async def relay_error(request):
        raise RelayError("upstream_error", "I couldn't reach Claude just now.")

    app.router.routes.append(Route("/boom", boom))
    app.router.routes.append(Route("/relay-error", relay_error))
    return app


async def test_healthz(app):
    async with client_for(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["version"] == __version__


async def test_healthz_reports_a_valid_commit_only(monkeypatch, cfg, secrets):
    monkeypatch.setenv("RELAY_COMMIT", "0123abc")
    async with client_for(create_app(cfg, secrets)) as c:
        assert (await c.get("/healthz")).json()["commit"] == "0123abc"
    monkeypatch.setenv("RELAY_COMMIT", "<script>")
    async with client_for(create_app(cfg, secrets)) as c:
        assert "commit" not in (await c.get("/healthz")).json()


async def test_unknown_path_uses_the_error_shape(app):
    async with client_for(app) as c:
        r = await c.get("/nope")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "bad_request", "spoken": "I didn't understand that request."}}


async def test_wrong_method_uses_the_error_shape(app):
    async with client_for(app) as c:
        r = await c.post("/healthz")
    assert r.status_code == 405 and r.json()["error"]["code"] == "bad_request"


async def test_a_crash_is_internal_and_spoken(app_that_raises, caplog):
    caplog.set_level(logging.ERROR)
    async with client_for(app_that_raises) as c:
        r = await c.get("/boom")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal", "spoken": "Something went wrong on my end."}}
    assert "kaboom" not in r.text  # the detail goes to the log, not to the caller
    assert "kaboom" in caplog.text


async def test_a_relay_error_answers_with_its_own_code_and_line(app_that_raises):
    async with client_for(app_that_raises) as c:
        r = await c.get("/relay-error")
    assert r.status_code == 502
    assert r.json() == {"error": {"code": "upstream_error", "spoken": "I couldn't reach Claude just now."}}


@pytest.mark.parametrize(
    "code, status",
    [
        ("unauthorized", 403),
        ("bad_request", 400),
        ("upstream_timeout", 504),
        ("upstream_error", 502),
        ("rate_limited", 429),
        ("internal", 500),
    ],
)
def test_every_code_has_its_status(code, status):
    err = RelayError(code, "spoken line")
    assert (err.status, err.code, err.spoken, err.store) == (status, code, "spoken line", True)
    assert error_response(code, "spoken line").status_code == status
