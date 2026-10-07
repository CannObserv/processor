"""``processor.checkin``: one check-in to a Status monitor (#35), against a local stub.

Status's contract (CannObserv/status spec D6, notifier's unchanged):
``POST /api/v1/monitors/{id}/checkin`` with ``{"status", "variables"}`` and an
``X-API-Key``, answered 202.
"""

import json

import pytest

from processor.checkin import CREDENTIAL_NAME, CheckinFailed, post_checkin, read_key

MONITOR = "01M46EXP45TVCVAMQXK043N1Q7"
KEY = "sk-test-0123456789abcdef"
PATH = f"/api/v1/monitors/{MONITOR}/checkin"


def test_a_check_in_posts_status_and_variables_with_the_key_as_a_header(http_stub):
    http_stub.route("POST", PATH, 202, {"state": "ok"})
    assert post_checkin(http_stub.url, MONITOR, KEY, "alert", {"kind": "lag"}) == 202
    (request,) = http_stub.requests
    assert json.loads(request["body"]) == {"status": "alert", "variables": {"kind": "lag"}}
    assert request["headers"]["X-API-Key"] == KEY
    assert request["headers"]["Content-Type"] == "application/json"


def test_a_trailing_slash_on_the_base_url_is_harmless(http_stub):
    http_stub.route("POST", PATH, 202)
    assert post_checkin(http_stub.url + "/", MONITOR, KEY, "ok", {}) == 202


@pytest.mark.parametrize(
    ("code", "body", "message"),
    [
        (401, {"detail": "invalid API key"}, "^401 invalid API key$"),
        (422, {"detail": {"section": "title", "message": "x"}}, "^422 .*section.*title"),
        (503, b"<html>down</html>", "^503$"),
        (200, {}, "^200$"),
    ],
)
def test_anything_but_202_is_a_failure_naming_statuss_answer(http_stub, code, body, message):
    http_stub.route("POST", PATH, code, body)
    with pytest.raises(CheckinFailed, match=message):
        post_checkin(http_stub.url, MONITOR, KEY, "ok", {})


def test_unreachable_is_a_failure_that_never_quotes_the_key():
    with pytest.raises(CheckinFailed, match="^ConnectionError") as caught:
        post_checkin("http://127.0.0.1:9", MONITOR, KEY, "ok", {})
    assert KEY not in str(caught.value)


@pytest.mark.parametrize(
    "key",
    ["sk-test\npart-two", "sk-test\u2019s", "sk test"],
    ids=["line-break", "unicode", "space"],
)
def test_a_malformed_key_is_refused_before_any_request_and_never_quoted(http_stub, key):
    """A bad paste at install (CR 1): requests' InvalidHeader quotes the value, and a
    non-latin-1 one escaped as UnicodeEncodeError, with no drift check record."""
    http_stub.route("POST", PATH, 202)
    with pytest.raises(CheckinFailed, match=CREDENTIAL_NAME) as caught:
        post_checkin(http_stub.url, MONITOR, key, "ok", {})
    assert "sk-test" not in str(caught.value)
    assert http_stub.requests == []


def test_a_stall_is_cut_off(http_stub):
    http_stub.route("POST", PATH, 202, delay=1.0)
    with pytest.raises(CheckinFailed, match="Timeout"):
        post_checkin(http_stub.url, MONITOR, KEY, "ok", {}, timeout=0.2)


class TestReadKey:
    """The key is a systemd credential (status D13), never an environment variable."""

    def test_the_credential_stripped(self, tmp_path):
        (tmp_path / CREDENTIAL_NAME).write_text(f"{KEY}\n")
        assert read_key(tmp_path) == KEY

    @pytest.mark.parametrize("content", [None, "", "\n"], ids=["absent", "empty", "fallback"])
    def test_none_reads_as_empty(self, tmp_path, content):
        if content is not None:
            (tmp_path / CREDENTIAL_NAME).write_text(content)
        assert read_key(tmp_path) == ""

    def test_outside_a_unit_there_is_no_directory(self):
        assert read_key(None) == ""
