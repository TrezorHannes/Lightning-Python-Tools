import sys
import os
import pytest
from unittest.mock import MagicMock, patch, mock_open

# --- FIXTURE: Mock Global Side Effects ---
@pytest.fixture(scope="module", autouse=True)
def mock_dependencies():
    mock_configparser = MagicMock()
    
    mock_config_data = {
        "credentials": {
            "amboss_authorization": "fake_auth",
            "lndg_username": "admin",
            "lndg_password": "password"
        },
        "lndg": {
            "lndg_api_url": "http://localhost:8889"
        },
        "paths": {
            "charge_lnd_path": "/tmp/charge-lnd"
        }
    }
    
    mock_config_instance = MagicMock()
    mock_config_instance.__getitem__.side_effect = mock_config_data.__getitem__
    mock_config_instance.get = MagicMock(side_effect=lambda section, option, fallback=None: mock_config_data.get(section, {}).get(option, fallback))
    mock_config_instance.has_option = MagicMock(return_value=True)
    mock_config_instance.has_section = MagicMock(return_value=True)
    mock_configparser.ConfigParser.return_value = mock_config_instance

    module_patches = {
        'configparser': mock_configparser,
    }

    with patch.dict(sys.modules, module_patches):
        with patch("builtins.open", mock_open(read_data="[lndg]\nfoo=bar")):
            with patch("os.makedirs"):
                yield

@pytest.fixture
def amboss_pull_module(mock_dependencies):
    if os.path.abspath(os.path.join(os.path.dirname(__file__), '../../LNDg')) not in sys.path:
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../LNDg')))
    
    import amboss_pull
    amboss_pull.requests = MagicMock()
    return amboss_pull

# --- UNIT TESTS ---

def test_scid_to_short_channel_id_math(amboss_pull_module):
    """Test local mathematical conversion from SCID string to 64-bit integer channel ID."""
    # Standard format: 892345x123x1 -> (892345 << 40) | (123 << 16) | 1
    # 892345 = 0x0D9DB9
    # 123 = 0x007B
    # 1 = 0x0001
    expected_id = (892345 << 40) | (123 << 16) | 1
    assert amboss_pull_module.scid_to_short_channel_id("892345x123x1") == str(expected_id)
    assert amboss_pull_module.scid_to_short_channel_id("892345:123:1") == str(expected_id)
    assert amboss_pull_module.scid_to_short_channel_id("892345/123/1") == str(expected_id)


def test_scid_to_short_channel_id_invalid(amboss_pull_module):
    """Test handling invalid SCID string formats."""
    assert amboss_pull_module.scid_to_short_channel_id("invalid_scid") is None
    assert amboss_pull_module.scid_to_short_channel_id("") is None
    assert amboss_pull_module.scid_to_short_channel_id(None) is None


def test_convert_short_to_long_chan_id_space_api_success(amboss_pull_module):
    """Test converting short channel IDs via Amboss Space getEdgeInfoBatch API."""
    mock_response = {
        "data": {
            "getEdgeInfoBatch": [
                {
                    "short_channel_id": "892345x123x1",
                    "long_channel_id": "981249812498124"
                },
                {
                    "short_channel_id": "700000x10x0",
                    "long_channel_id": "770000000000000"
                }
            ]
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    amboss_pull_module.requests.post = MagicMock(return_value=mock_post)

    id_map = amboss_pull_module.convert_short_to_long_chan_id(["892345x123x1", "700000x10x0"])
    
    assert id_map["892345x123x1"] == "981249812498124"
    assert id_map["700000x10x0"] == "770000000000000"


def test_convert_short_to_long_chan_id_fallback_to_math(amboss_pull_module):
    """Test falling back to mathematical SCID conversion if Space API fails."""
    mock_post = MagicMock()
    mock_post.raise_for_status.side_effect = Exception("Space API connection timeout")
    amboss_pull_module.requests.post = MagicMock(return_value=mock_post)

    expected_id = (892345 << 40) | (123 << 16) | 1
    id_map = amboss_pull_module.convert_short_to_long_chan_id(["892345x123x1"])
    
    assert id_map["892345x123x1"] == str(expected_id)


def test_extract_order_channel_info_modern_schema(amboss_pull_module):
    """Test extracting normalized channel info from modern Magma Order schema."""
    modern_order = {
        "id": "order_modern_01",
        "status": "VALID_CHANNEL_OPENING",
        "channel_id": "892345x123x1",
        "promises": {
            "locked_min_block_length": 4320,
            "locked_fee_rate_cap": 500
        },
        "blocks_until_can_be_closed": 1500,
        "created_at": "2026-08-27T12:00:00Z"
    }

    info = amboss_pull_module.extract_order_channel_info(modern_order)
    assert info["status"] == "VALID_CHANNEL_OPENING"
    assert info["channel_id"] == "892345x123x1"
    assert info["locked_min_block_length"] == 4320
    assert info["locked_fee_rate_cap"] == 500
    assert info["blocks_until_close"] == 1500


def test_fetch_magma_orders_success(amboss_pull_module):
    """Test fetching orders from modern Magma GraphQL endpoint."""
    mock_response = {
        "data": {
            "user": {
                "market": {
                    "orders": {
                        "sales": {
                            "list": [
                                {
                                    "id": "sale_01",
                                    "status": "VALID_CHANNEL_OPENING",
                                    "channel_id": "892345x123x1",
                                    "promises": {"locked_min_block_length": 4320, "locked_fee_rate_cap": 500}
                                }
                            ]
                        },
                        "purchases": {
                            "list": [
                                {
                                    "id": "buy_01",
                                    "status": "CHANNEL_MONITORING_FINISHED",
                                    "channel_id": "700000x10x0",
                                    "promises": {"locked_min_block_length": 2016, "locked_fee_rate_cap": 250}
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    amboss_pull_module.requests.post = MagicMock(return_value=mock_post)

    orders = amboss_pull_module.fetch_magma_orders()
    assert len(orders) == 2
    assert orders[0]["id"] == "sale_01"
    assert orders[1]["id"] == "buy_01"


def test_cluster_sold_channels_grouping(amboss_pull_module, mocker):
    """Test clustering orders into active fee cap groups and finished/expired lists."""
    mock_orders = [
        # Active order with fee cap 500
        {
            "id": "ord_active_1",
            "status": "VALID_CHANNEL_OPENING",
            "channel_id": "800000x1x0",
            "promises": {"locked_min_block_length": 4320, "locked_fee_rate_cap": 500},
            "blocks_until_can_be_closed": 2000
        },
        # Active order with fee cap 750
        {
            "id": "ord_active_2",
            "status": "VALID_CHANNEL_OPENING",
            "channel_id": "800000x2x0",
            "promises": {"locked_min_block_length": 4320, "locked_fee_rate_cap": 750},
            "blocks_until_can_be_closed": 1000
        },
        # Expired order
        {
            "id": "ord_expired_1",
            "status": "CHANNEL_MONITORING_FINISHED",
            "channel_id": "800000x3x0",
            "promises": {"locked_min_block_length": 2016, "locked_fee_rate_cap": 250},
            "blocks_until_can_be_closed": 0
        }
    ]
    mocker.patch.object(amboss_pull_module, "fetch_magma_orders", return_value=mock_orders)
    mocker.patch.object(
        amboss_pull_module,
        "convert_short_to_long_chan_id",
        return_value={
            "800000x1x0": "880000000000001",
            "800000x2x0": "880000000000002",
            "800000x3x0": "880000000000003",
        }
    )

    mock_file = mocker.patch("builtins.open", mock_open())

    active_info, non_active_ids, fee_caps = amboss_pull_module.cluster_sold_channels()

    assert len(active_info) == 2
    assert "880000000000003" in non_active_ids
    assert 500 in fee_caps
    assert 750 in fee_caps
    assert fee_caps[500] == ["880000000000001"]
    assert fee_caps[750] == ["880000000000002"]


def test_update_autofees_lndg_api(amboss_pull_module, mocker):
    """Test updating auto_fees and notes in LNDg API for expired channels."""
    mock_get = MagicMock()
    mock_get.status_code = 200
    mock_get.json.return_value = {
        "results": [
            {"chan_id": "880000000000003", "auto_fees": False, "is_active": True, "is_open": True},
            {"chan_id": "880000000000004", "auto_fees": True, "is_active": True, "is_open": True}
        ]
    }

    mock_put = MagicMock()
    mock_put.status_code = 200

    amboss_pull_module.requests.get = MagicMock(return_value=mock_get)
    amboss_pull_module.requests.put = MagicMock(return_value=mock_put)
    mocker.patch("builtins.open", mock_open())

    amboss_pull_module.update_autofees(["880000000000003"])

    amboss_pull_module.requests.put.assert_called_once()
    put_call = amboss_pull_module.requests.put.call_args
    assert "880000000000003" in put_call[0][0]
    payload = put_call[1]["json"]
    assert payload["auto_fees"] is True
    assert "Expired" in payload["notes"]


def test_update_notes_for_active_channels_lndg_api(amboss_pull_module, mocker):
    """Test updating notes in LNDg API for active leased channels."""
    mock_put = MagicMock()
    mock_put.status_code = 200
    amboss_pull_module.requests.put = MagicMock(return_value=mock_put)
    mocker.patch("builtins.open", mock_open())

    active_info = [
        ("880000000000001", 1500, 500, -500), # Proportional fee activated
        ("880000000000002", 2000, 750, 500)   # Proportional fee in future
    ]

    amboss_pull_module.update_notes_for_active_channels(active_info)

    assert amboss_pull_module.requests.put.call_count == 2
    first_put = amboss_pull_module.requests.put.call_args_list[0][1]["json"]
    assert first_put["auto_fees"] is False
    assert "Proportional Fee Rate activated" in first_put["notes"]
