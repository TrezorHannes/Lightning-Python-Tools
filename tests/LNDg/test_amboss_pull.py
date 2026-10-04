"""Tests for LNDg/amboss_pull.py.

HTTP is mocked at the transport layer (requests-mock) so the real request, retry,
classification and parsing code paths execute. File output uses real temp dirs.
"""
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../LNDg")))
import amboss_pull  # noqa: E402

MAGMA = amboss_pull.MAGMA_GRAPHQL_URL
SPACE = amboss_pull.AMBOSS_SPACE_GRAPHQL_URL
LNDG = "http://lndg.test:8889"


@pytest.fixture(autouse=True)
def isolated_module(monkeypatch, tmp_path):
    """Deterministic config, a real temp charge-lnd dir, and no retry sleeps."""
    out = tmp_path / "charge-lnd"
    out.mkdir()
    monkeypatch.setattr(amboss_pull, "AMBOSS_TOKEN", "test-token")
    monkeypatch.setattr(amboss_pull, "LNDG_BASE_URL", LNDG)
    monkeypatch.setattr(amboss_pull, "LNDG_USERNAME", "admin")
    monkeypatch.setattr(amboss_pull, "LNDG_PASSWORD", "pw")
    monkeypatch.setattr(amboss_pull, "CHARGE_LND_PATH", str(out))
    monkeypatch.setattr(amboss_pull.time, "sleep", lambda *_: None)
    return out


# --- helpers ---------------------------------------------------------------

def _sales_page(orders, total):
    return {"data": {"user": {"market": {"orders": {"sales": {"total": total, "list": orders}}}}}}


def _order_details(order_id, channel_id, blocks, min_len, fee_cap):
    return {"data": {"user": {"market": {"orders": {"get_order": {
        "id": order_id, "status": "VALID_CHANNEL_OPENING", "channel_id": channel_id,
        "blocks_until_can_be_closed": blocks,
        "promises": {"locked_min_block_length": min_len, "locked_fee_rate_cap": {"sats": str(fee_cap)}},
    }}}}}}


def _simple(order_id, status, channel_id):
    """A SimpleMarketOrder as returned by the live sales list (no promises/blocks)."""
    return {"id": order_id, "status": status, "channel_id": channel_id, "created_at": "2026-09-01T00:00:00Z"}


def magma_router(sales_pages, details=None):
    """requests-mock callback dispatching on GraphQL operation + variables."""
    details = details or {}

    def respond(request, context):
        body = request.json()
        query = body["query"]
        if "GetMagmaSales" in query:
            offset = body["variables"]["page"]["offset"]
            return sales_pages[offset]
        if "GetOrderLeaseDetails" in query:
            return details[body["variables"]["orderId"]]
        context.status_code = 400
        return {"errors": [{"message": f"unexpected query {query[:40]}"}]}

    return respond


def long_id(scid):
    return amboss_pull.scid_to_short_channel_id(scid)


def read_lines(path):
    with open(path) as f:
        return f.read().splitlines()


# --- SCID conversion -------------------------------------------------------

def test_scid_to_short_channel_id_math():
    expected_id = (892345 << 40) | (123 << 16) | 1
    assert amboss_pull.scid_to_short_channel_id("892345x123x1") == str(expected_id)
    assert amboss_pull.scid_to_short_channel_id("892345:123:1") == str(expected_id)
    assert amboss_pull.scid_to_short_channel_id("892345/123/1") == str(expected_id)
    # Live-verified against Amboss Space getEdgeInfoBatch
    assert amboss_pull.scid_to_short_channel_id("967276x1075x1") == "1063531209339109377"


def test_scid_to_short_channel_id_invalid():
    assert amboss_pull.scid_to_short_channel_id("invalid_scid") is None
    assert amboss_pull.scid_to_short_channel_id("") is None
    assert amboss_pull.scid_to_short_channel_id(None) is None


def test_convert_short_to_long_chan_id_space_api_success(requests_mock):
    requests_mock.post(SPACE, json={"data": {"getEdgeInfoBatch": [
        {"short_channel_id": "892345x123x1", "long_channel_id": "981249812498124"},
    ]}})
    id_map = amboss_pull.convert_short_to_long_chan_id(["892345x123x1", "700000x10x0"])
    assert id_map["892345x123x1"] == "981249812498124"
    assert id_map["700000x10x0"] == long_id("700000x10x0")  # missing from API -> math


def test_convert_short_to_long_chan_id_fallback_to_math(requests_mock):
    requests_mock.post(SPACE, exc=requests.exceptions.ConnectTimeout)
    id_map = amboss_pull.convert_short_to_long_chan_id(["892345x123x1"])
    assert id_map["892345x123x1"] == long_id("892345x123x1")


# --- Cycle 1: schema-correct list query + lease enrichment ------------------

def test_sales_list_query_only_selects_simple_market_order_fields():
    """Regression for #56: `promises`/`blocks_until_can_be_closed` do not exist on
    SimpleMarketOrder, and requesting them fails GraphQL validation (HTTP 400)."""
    list_selection = amboss_pull.GET_SALES_PAGE_QUERY.split("list", 1)[1]
    assert "promises" not in list_selection
    assert "blocks_until_can_be_closed" not in list_selection
    details = amboss_pull.GET_ORDER_LEASE_DETAILS_QUERY
    assert "get_order" in details and "promises" in details and "blocks_until_can_be_closed" in details


def test_fetch_magma_orders_enriches_active_leases_with_promises(requests_mock):
    pages = {0: _sales_page([
        _simple("active-1", "VALID_CHANNEL_OPENING", "967276x1075x1"),
        _simple("done-1", "CHANNEL_MONITORING_FINISHED", "956914x1301x0"),
        _simple("rejected-1", "SELLER_REJECTED", None),
    ], total=3)}
    details = {"active-1": _order_details("active-1", "967276x1075x1", 6084, 8640, 1650)}
    requests_mock.post(MAGMA, json=magma_router(pages, details))

    orders = amboss_pull.fetch_magma_orders()

    info = {o["id"]: amboss_pull.extract_order_channel_info(o) for o in orders}
    assert info["active-1"]["locked_fee_rate_cap"] == 1650
    assert info["active-1"]["locked_min_block_length"] == 8640
    assert info["active-1"]["blocks_until_close"] == 6084
    # Only the active lease needs a detail lookup; finished/rejected do not.
    detail_calls = [r for r in requests_mock.request_history if "GetOrderLeaseDetails" in r.json()["query"]]
    assert [r.json()["variables"]["orderId"] for r in detail_calls] == ["active-1"]
    assert all(r.headers["Authorization"] == "Bearer test-token" for r in requests_mock.request_history)


# --- Cycle 2: pagination ----------------------------------------------------

def test_fetch_magma_sales_paginates_until_total(requests_mock):
    first = [_simple(f"o{i}", "CHANNEL_MONITORING_FINISHED", f"800000x{i}x0") for i in range(2)]
    second = [_simple("o2", "CHANNEL_MONITORING_FINISHED", "800000x2x0")]
    requests_mock.post(MAGMA, json=magma_router({0: _sales_page(first, 3), 2: _sales_page(second, 3)}))

    orders = amboss_pull.fetch_magma_sales(page_size=2)

    assert [o["id"] for o in orders] == ["o0", "o1", "o2"]
    offsets = [r.json()["variables"]["page"] for r in requests_mock.request_history]
    assert offsets == [{"limit": 2, "offset": 0}, {"limit": 2, "offset": 2}]


def test_fetch_magma_sales_raises_when_page_guard_exceeded(requests_mock):
    page = [_simple("x", "CHANNEL_MONITORING_FINISHED", "800000x1x0")]
    requests_mock.post(MAGMA, json=lambda req, ctx: _sales_page(page, 10_000))
    with pytest.raises(amboss_pull.AmbossAPIError, match="pagination exceeded"):
        amboss_pull.fetch_magma_sales(page_size=1, max_pages=3)


# --- Cycle 3: sales only ----------------------------------------------------

def test_lease_sync_queries_sales_only_not_purchases():
    """Seller fee-cap promises apply only to channels we sold."""
    assert "purchases" not in amboss_pull.GET_SALES_PAGE_QUERY
    assert "sales" in amboss_pull.GET_SALES_PAGE_QUERY


# --- Cycle 4: error surfacing & retry classification -----------------------

def test_graphql_validation_error_is_logged_and_not_retried(requests_mock, caplog):
    body = {"errors": [{"message": 'Cannot query field "promises" on type "SimpleMarketOrder".',
                        "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}
    requests_mock.post(MAGMA, status_code=400, json=body)

    with caplog.at_level("ERROR"), pytest.raises(amboss_pull.AmbossAPIError) as exc:
        amboss_pull.fetch_magma_sales()

    assert requests_mock.call_count == 1
    assert exc.value.status_code == 400 and not exc.value.transient
    assert "Cannot query field" in caplog.text and "SimpleMarketOrder" in caplog.text


def test_graphql_errors_on_200_raise(requests_mock):
    requests_mock.post(MAGMA, json={"errors": [{"message": "Unauthorized"}], "data": None})
    with pytest.raises(amboss_pull.AmbossAPIError, match="Unauthorized"):
        amboss_pull.fetch_magma_sales()
    assert requests_mock.call_count == 1


@pytest.mark.parametrize("failure", [
    {"status_code": 502, "text": "Bad Gateway"},
    {"status_code": 429, "text": "Too Many Requests"},
    {"exc": requests.exceptions.ReadTimeout},
    {"exc": requests.exceptions.ConnectionError},
])
def test_transient_failures_are_retried_then_succeed(requests_mock, failure):
    ok = _sales_page([], total=0)
    requests_mock.post(MAGMA, [failure, {"json": ok}])
    assert amboss_pull.fetch_magma_sales() == []
    assert requests_mock.call_count == 2


def test_transient_failures_exhaust_attempts(requests_mock):
    requests_mock.post(MAGMA, status_code=503, text="unavailable")
    with pytest.raises(amboss_pull.AmbossAPIError, match="exceeded 3 attempts") as exc:
        amboss_pull.execute_graphql("query { x }", None, "Probe", max_attempts=3)
    assert exc.value.transient and exc.value.status_code == 503
    assert requests_mock.call_count == 3


def test_non_json_200_body_raises(requests_mock):
    requests_mock.post(MAGMA, status_code=200, text="<html>maintenance</html>")
    with pytest.raises(amboss_pull.AmbossAPIError, match="non-JSON"):
        amboss_pull.execute_graphql("query { x }", None, "Probe")


def test_failed_detail_lookup_aborts_whole_fetch(requests_mock):
    """A missing fee cap for an active lease must never be silently dropped."""
    pages = {0: _sales_page([_simple("a", "VALID_CHANNEL_OPENING", "967276x1075x1")], 1)}

    def respond(request, context):
        if "GetOrderLeaseDetails" in request.json()["query"]:
            context.status_code = 400
            return {"errors": [{"message": "order not found"}]}
        return pages[0]

    requests_mock.post(MAGMA, json=respond)
    with pytest.raises(amboss_pull.AmbossAPIError, match="order not found"):
        amboss_pull.fetch_magma_orders()


# --- Cycle 5: fail closed end-to-end ---------------------------------------

def test_main_fails_closed_on_api_error(requests_mock, isolated_module):
    cap_file = isolated_module / "magma-channels_1650.txt"
    finished = isolated_module / "magma-finished.txt"
    cap_file.write_text("1052138069866905600\n")
    finished.write_text("111\n222\n")
    requests_mock.post(MAGMA, status_code=400, json={"errors": [{"message": "schema drift"}]})
    lndg_get = requests_mock.get(requests_mock_any_lndg(), json={"results": []})
    lndg_put = requests_mock.put(requests_mock_any_lndg(), json={})

    assert amboss_pull.main() == 1

    assert cap_file.read_text() == "1052138069866905600\n"
    assert finished.read_text() == "111\n222\n"
    assert not lndg_get.called and not lndg_put.called


def requests_mock_any_lndg():
    import re
    return re.compile(re.escape(LNDG) + r"/api/channels/.*")


def test_main_happy_path_writes_files_and_updates_lndg(requests_mock, isolated_module):
    active_scid, done_scid = "967276x1075x1", "956914x1301x0"
    pages = {0: _sales_page([
        _simple("active-1", "VALID_CHANNEL_OPENING", active_scid),
        _simple("done-1", "CHANNEL_MONITORING_FINISHED", done_scid),
    ], 2)}
    details = {"active-1": _order_details("active-1", active_scid, 6084, 8640, 1650)}
    requests_mock.post(MAGMA, json=magma_router(pages, details))
    requests_mock.post(SPACE, json={"data": {"getEdgeInfoBatch": []}})
    requests_mock.get(requests_mock_any_lndg(), json={"results": [
        {"chan_id": long_id(done_scid), "auto_fees": False},
    ]})
    put = requests_mock.put(requests_mock_any_lndg(), json={})

    assert amboss_pull.main() == 0

    assert read_lines(isolated_module / "magma-channels_1650.txt") == [long_id(active_scid)]
    assert read_lines(isolated_module / "magma-finished.txt") == [long_id(done_scid)]
    payloads = {p.json()["chan_id"]: p.json() for p in put.request_history}
    assert payloads[long_id(done_scid)]["auto_fees"] is True
    active_note = payloads[long_id(active_scid)]
    assert active_note["auto_fees"] is False
    assert "Lease Expiration: 6084 blocks" in active_note["notes"]
    # grace = -(8640 - 6084 - 2016) = -540 -> proportional already active
    assert "Proportional Fee Rate activated" in active_note["notes"]


def test_main_returns_nonzero_when_lndg_update_fails(requests_mock, isolated_module):
    pages = {0: _sales_page([_simple("a", "VALID_CHANNEL_OPENING", "967276x1075x1")], 1)}
    details = {"a": _order_details("a", "967276x1075x1", 100, 8640, 1650)}
    requests_mock.post(MAGMA, json=magma_router(pages, details))
    requests_mock.post(SPACE, json={"data": {"getEdgeInfoBatch": []}})
    requests_mock.put(requests_mock_any_lndg(), status_code=500)
    assert amboss_pull.main() == 1


# --- Cycle 6: rule file reconciliation & atomic writes ----------------------

def test_stale_fee_cap_files_are_emptied_not_deleted(isolated_module, requests_mock):
    requests_mock.post(SPACE, json={"data": {"getEdgeInfoBatch": []}})
    (isolated_module / "magma-channels_350.txt").write_text("1000\n")
    (isolated_module / "magma-channels_650.txt").write_text("2000\n")
    (isolated_module / "magma_pubkey.txt").write_text("keep-me\n")
    orders = [
        {**_simple("a", "VALID_CHANNEL_OPENING", "967276x1075x1"),
         "blocks_until_can_be_closed": 6084,
         "promises": {"locked_min_block_length": 8640, "locked_fee_rate_cap": {"sats": "1650"}}},
    ]

    _, _, groups = amboss_pull.cluster_sold_channels(orders=orders)

    assert groups == {1650: [long_id("967276x1075x1")]}
    assert read_lines(isolated_module / "magma-channels_1650.txt") == [long_id("967276x1075x1")]
    assert read_lines(isolated_module / "magma-channels_350.txt") == []
    assert read_lines(isolated_module / "magma-channels_650.txt") == []
    assert (isolated_module / "magma_pubkey.txt").read_text() == "keep-me\n"



def test_atomic_write_preserves_existing_mode_and_leaves_no_temp_files(isolated_module):
    target = isolated_module / "magma-channels_1650.txt"
    target.write_text("old\n")
    os.chmod(target, 0o664)

    amboss_pull._atomic_write_lines(str(target), ["a", "b"])

    assert read_lines(target) == ["a", "b"]
    assert (os.stat(target).st_mode & 0o777) == 0o664
    assert sorted(os.listdir(isolated_module)) == ["magma-channels_1650.txt"]


def test_atomic_write_new_file_is_world_readable(isolated_module):
    """charge-lnd runs as a different user; mkstemp's 0600 default would lock it out."""
    target = isolated_module / "magma-finished.txt"
    amboss_pull._atomic_write_lines(str(target), [])
    assert (os.stat(target).st_mode & 0o777) == 0o644


def test_atomic_write_failure_keeps_original_and_cleans_temp(isolated_module, monkeypatch):
    target = isolated_module / "magma-finished.txt"
    target.write_text("original\n")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(amboss_pull.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        amboss_pull._atomic_write_lines(str(target), ["new"])
    assert target.read_text() == "original\n"
    assert sorted(os.listdir(isolated_module)) == ["magma-finished.txt"]


def test_channel_with_active_lease_is_never_marked_finished(isolated_module, requests_mock):
    requests_mock.post(SPACE, json={"data": {"getEdgeInfoBatch": []}})
    scid = "967276x1075x1"
    orders = [
        _simple("old", "CHANNEL_MONITORING_FINISHED", scid),
        {**_simple("new", "VALID_CHANNEL_OPENING", scid), "blocks_until_can_be_closed": 10,
         "promises": {"locked_min_block_length": 8640, "locked_fee_rate_cap": {"sats": "1650"}}},
    ]
    _, finished, groups = amboss_pull.cluster_sold_channels(orders=orders)
    assert finished == []
    assert groups == {1650: [long_id(scid)]}


# --- extract_order_channel_info ----------------------------------------------

def test_extract_order_channel_info_live_shapes():
    info = amboss_pull.extract_order_channel_info({
        "id": "o", "status": "valid_channel_opening", "channel_id": "1x2x3",
        "blocks_until_can_be_closed": 6084,
        "promises": {"locked_min_block_length": 8640.0, "locked_fee_rate_cap": {"sats": "1650"}},
    })
    assert info["status"] == "VALID_CHANNEL_OPENING"
    assert info["locked_min_block_length"] == 8640
    assert info["locked_fee_rate_cap"] == 1650
    assert info["blocks_until_close"] == 6084


def test_extract_order_channel_info_simple_order_defaults_to_zero():
    info = amboss_pull.extract_order_channel_info(_simple("o", "CHANNEL_MONITORING_FINISHED", "1x2x3"))
    assert info["locked_fee_rate_cap"] == 0
    assert info["blocks_until_close"] == 0


def test_import_has_no_logging_side_effects():
    """Logging must be configured in the entry point, not at import time."""
    import logging
    root = logging.getLogger()
    assert not any(
        isinstance(h, logging.FileHandler) and h.baseFilename.endswith("amboss-LNDg_changes.log")
        for h in root.handlers
    )
