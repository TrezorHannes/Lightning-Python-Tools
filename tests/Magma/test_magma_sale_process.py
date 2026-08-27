import sys
import os
import pytest
from unittest.mock import MagicMock, patch, mock_open

# --- FIXTURE: Mock Global Side Effects ---
@pytest.fixture(scope="module", autouse=True)
def mock_dependencies():
    mock_telebot = MagicMock()
    mock_telebot.TeleBot = MagicMock()
    mock_configparser = MagicMock()
    mock_logging = MagicMock()
    mock_schedule = MagicMock()
    
    mock_config_data = {
        "telegram": {"magma_bot_token": "fake_token", "telegram_user_id": "123"},
        "credentials": {"amboss_authorization": "fake_auth"},
        "system": {"full_path_bos": "/path/to/bos"},
        "magma": {
            "invoice_expiry_seconds": "1800",
            "max_fee_percentage_of_invoice": "0.9",
            "channel_fee_rate_ppm": "350",
            "auto_approve_buyer_conditions": "true",
            "auto_approve_min_seller_score": "80.0",
        },
        "urls": {"mempool_fees_api": "https://mempool.space/api/v1/fees/recommended"},
        "pubkey": {"banned_magma_pubkeys": "banned_pubkey_1,banned_pubkey_2"},
        "paths": {"lncli_path": "lncli"}
    }
    
    mock_config_instance = MagicMock()
    mock_config_instance.__getitem__.side_effect = mock_config_data.__getitem__
    mock_config_instance.get = MagicMock(side_effect=lambda section, option, fallback=None: mock_config_data.get(section, {}).get(option, fallback))
    mock_config_instance.getint = MagicMock(return_value=10)
    mock_config_instance.getfloat = MagicMock(return_value=0.5)
    mock_config_instance.has_option = MagicMock(return_value=True)
    mock_configparser.ConfigParser.return_value = mock_config_instance

    module_patches = {
        'telebot': mock_telebot,
        'telebot.types': MagicMock(),
        'configparser': mock_configparser,
        'schedule': mock_schedule,
        'logging.handlers': MagicMock(),
    }

    with patch.dict(sys.modules, module_patches):
        with patch("builtins.open", mock_open(read_data="[magma]\nfoo=bar")):
            with patch("os.makedirs"):
                 yield

@pytest.fixture
def magma_module(mock_dependencies):
    if os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Magma')) not in sys.path:
         sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Magma')))
    
    import magma_sale_process
    magma_sale_process.requests = MagicMock()
    magma_sale_process.AMBOSS_TOKEN = "fake_auth"
    return magma_sale_process

# --- TESTS ---

def test_get_node_alias_success(magma_module):
    """Test retrieving node alias successfully from Space endpoint."""
    mock_response = {"data": {"getNodeAlias": "TestNode"}}
    
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    magma_module.requests.post = MagicMock(return_value=mock_post)

    alias = magma_module.get_node_alias("pubkey123")
    assert alias == "TestNode"


def test_get_node_alias_failure(magma_module):
    """Test retrieving node alias when API fails."""
    mock_post = MagicMock()
    mock_post.json.return_value = {} 
    magma_module.requests.post = MagicMock(return_value=mock_post)

    alias = magma_module.get_node_alias("pubkey123")
    assert alias == "ErrorFetchingAlias"


def test_extract_order_info_new_api(magma_module):
    """Test extracting normalized fields from live-verified Magma MarketOrder schema."""
    sample_order = {
        "id": "order_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {
            "satoshi": {
                "sats": "5000000",
                "btc": "0.05",
                "usd": "3000"
            }
        },
        "fees": {
            "fixed": {"sats": "1000"},
            "variable": {"sats": "2500"},
            "seller": {"sats": "3500"},
            "amboss": {"sats": "500"},
            "buyer": {"sats": "4000"}
        },
        "promises": {
            "locked_min_block_length": 4320
        },
        "destination": {
            "pubkey": "03deadbeef1234567890",
            "alias": "LightningBuyer"
        },
        "channel_id": "892345x123x1",
        "created_at": "2026-08-27T12:00:00Z"
    }

    info = magma_module.extract_order_info(sample_order)

    assert info["id"] == "order_001"
    assert info["status"] == "WAITING_FOR_SELLER_APPROVAL"
    assert info["customer_pubkey"] == "03deadbeef1234567890"
    assert info["buyer_alias"] == "LightningBuyer"
    assert info["channel_size"] == 5000000
    assert info["seller_invoice_amount"] == 3500
    assert info["fixed_fee"] == 1000
    assert info["variable_fee"] == 2500
    assert info["amboss_fee"] == 500
    assert info["min_block_length"] == 4320


def test_extract_order_info_legacy_fallback(magma_module):
    """Test extracting normalized fields when encountering legacy flat dict fields."""
    legacy_order = {
        "id": "legacy_001",
        "status": "WAITING_FOR_CHANNEL_OPEN",
        "size": 2000000,
        "seller_invoice_amount": 1500,
        "fixed_fee": 500,
        "variable_fee": 1000,
        "account": "02abcdef123456",
        "locked_min_block_length": 2016
    }

    info = magma_module.extract_order_info(legacy_order)

    assert info["id"] == "legacy_001"
    assert info["status"] == "WAITING_FOR_CHANNEL_OPEN"
    assert info["customer_pubkey"] == "02abcdef123456"
    assert info["channel_size"] == 2000000
    assert info["seller_invoice_amount"] == 1500
    assert info["min_block_length"] == 2016


def test_execute_lncli_addinvoice_success(magma_module, mocker):
    """Test generating an invoice calls lncli correctly."""
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    expected_json = '{"r_hash": "hash123", "payment_request": "lnbc..."}'
    process_mock.communicate.return_value = (expected_json.encode('utf-8'), b"")
    mock_popen.return_value = process_mock

    r_hash, pay_req = magma_module.execute_lncli_addinvoice(1000, "memo", 3600)

    assert r_hash == "hash123"
    assert pay_req == "lnbc..."
    
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "--amt" in args
    amt_index = args.index("--amt")
    assert args[amt_index + 1] == "1000"


def test_execute_lncli_addinvoice_failure(magma_module, mocker):
    """Test error handling when lncli fails."""
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    process_mock.communicate.return_value = (b"", b"Error: something went wrong")
    mock_popen.return_value = process_mock

    r_hash, pay_req = magma_module.execute_lncli_addinvoice(1000, "memo", 3600)
    
    assert r_hash.startswith("Error")
    assert pay_req is None


def test_accept_order_success(magma_module):
    """Test accepting an order on Amboss Magma."""
    mock_response = {
        "data": {
            "market": {
                "order": {
                    "seller": {
                        "accept": {
                            "success": True
                        }
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    magma_module.requests.post = MagicMock(return_value=mock_post)

    result = magma_module.accept_order("order123", "lnbc123")
    assert result == mock_response

    call_args = magma_module.requests.post.call_args
    assert call_args[0][0] == magma_module.MAGMA_GRAPHQL_URL
    payload = call_args[1]["json"]
    assert payload["variables"]["input"]["order_id"] == "order123"
    assert payload["variables"]["input"]["payment_request"] == "lnbc123"


def test_reject_order_success(magma_module):
    """Test rejecting an order on Amboss Magma."""
    mock_response = {
        "data": {
            "market": {
                "order": {
                    "seller": {
                        "reject": {
                            "success": True
                        }
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    magma_module.requests.post = MagicMock(return_value=mock_post)

    result = magma_module.reject_order("order123")
    assert result == mock_response

    call_args = magma_module.requests.post.call_args
    assert call_args[0][0] == magma_module.MAGMA_GRAPHQL_URL
    payload = call_args[1]["json"]
    assert payload["variables"]["input"]["order_id"] == "order123"


def test_confirm_channel_point_to_amboss_success(magma_module):
    """Test confirming a channel point on Amboss Magma."""
    mock_response = {
        "data": {
            "market": {
                "order": {
                    "seller": {
                        "add_transaction": {
                            "success": True
                        }
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    magma_module.requests.post = MagicMock(return_value=mock_post)

    result = magma_module.confirm_channel_point_to_amboss("order123", "5e8a3f...c4f1:0")
    assert result == mock_response

    call_args = magma_module.requests.post.call_args
    assert call_args[0][0] == magma_module.MAGMA_GRAPHQL_URL
    payload = call_args[1]["json"]
    assert payload["variables"]["input"]["order_id"] == "order123"
    assert payload["variables"]["input"]["tx_id"] == "5e8a3f...c4f1:0"


def test_confirm_channel_point_to_amboss_critical_error(magma_module, mocker):
    """Test that Amboss API error in confirm_channel_point writes to critical error flag."""
    mock_response = {
        "errors": [{"message": "Invalid transaction outpoint"}]
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    magma_module.requests.post = MagicMock(return_value=mock_post)

    mock_file = mocker.patch("builtins.open", mock_open())
    mocker.patch.object(magma_module, "send_telegram_notification")

    result = magma_module.confirm_channel_point_to_amboss("order123", "bad_tx:0")
    assert "errors" in result
    mock_file.assert_called()


def test_get_offers_awaiting_seller_approval_success(magma_module):
    """Test fetching sales awaiting seller approval."""
    mock_response = {
        "data": {
            "user": {
                "market": {
                    "orders": {
                        "sales": {
                            "total": 1,
                            "list": [
                                {
                                    "id": "order_pending_01",
                                    "status": "WAITING_FOR_SELLER_APPROVAL",
                                    "amount": {"satoshi": {"sats": "2000000"}},
                                    "fees": {"seller": {"sats": "5000"}},
                                    "destination": {"pubkey": "02goodpubkey123", "alias": "GoodBuyer"}
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
    magma_module.requests.post = MagicMock(return_value=mock_post)

    offer = magma_module.get_offers_awaiting_seller_approval()
    assert offer is not None
    assert offer["id"] == "order_pending_01"


def test_get_offers_awaiting_seller_approval_banned_pubkey_auto_reject(magma_module, mocker):
    """Test that banned buyer pubkeys are automatically rejected."""
    mock_response = {
        "data": {
            "user": {
                "market": {
                    "orders": {
                        "sales": {
                            "total": 1,
                            "list": [
                                {
                                    "id": "order_banned_01",
                                    "status": "WAITING_FOR_SELLER_APPROVAL",
                                    "amount": {"satoshi": {"sats": "2000000"}},
                                    "fees": {"seller": {"sats": "5000"}},
                                    "destination": {"pubkey": "banned_pubkey_1", "alias": "BadActor"}
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
    magma_module.requests.post = MagicMock(return_value=mock_post)

    mock_reject = mocker.patch.object(
        magma_module,
        "reject_order",
        return_value={"data": {"market": {"order": {"seller": {"reject": {"success": True}}}}}}
    )
    mocker.patch.object(magma_module, "send_telegram_notification")

    offer = magma_module.get_offers_awaiting_seller_approval()
    assert offer is None
    mock_reject.assert_called_once_with("order_banned_01")


def test_get_orders_awaiting_channel_open_success(magma_module):
    """Test fetching sales awaiting channel open."""
    mock_response = {
        "data": {
            "user": {
                "market": {
                    "orders": {
                        "sales": {
                            "total": 1,
                            "list": [
                                {
                                    "id": "order_channel_open_01",
                                    "status": "WAITING_FOR_CHANNEL_OPEN",
                                    "amount": {"satoshi": {"sats": "5000000"}},
                                    "fees": {"seller": {"sats": "10000"}},
                                    "destination": {"pubkey": "03peerpubkey456", "alias": "PeerNode"}
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
    magma_module.requests.post = MagicMock(return_value=mock_post)

    order = magma_module.get_orders_awaiting_channel_open()
    assert order is not None
    assert order["id"] == "order_channel_open_01"


def test_get_order_details_from_amboss_direct(magma_module):
    """Test fetching order details by ID via get_order query."""
    mock_response = {
        "data": {
            "user": {
                "market": {
                    "orders": {
                        "get_order": {
                            "id": "order_specific_01",
                            "status": "WAITING_FOR_CHANNEL_OPEN",
                            "amount": {"satoshi": {"sats": "3000000"}},
                            "fees": {"seller": {"sats": "7000"}},
                            "destination": {"pubkey": "03pubkey789", "alias": "TargetBuyer"}
                        }
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    magma_module.requests.post = MagicMock(return_value=mock_post)

    details = magma_module.get_order_details_from_amboss("order_specific_01")
    assert details is not None
    assert details["id"] == "order_specific_01"


def test_calculate_transaction_size(magma_module):
    """Test SegWit P2WPKH transaction virtual size calculation."""
    assert magma_module.calculate_transaction_size(1) == 154.0
    assert magma_module.calculate_transaction_size(2) == 211.5


def test_execute_lnd_command_success(magma_module, mocker):
    """Test successfully opening a channel."""
    mock_run = mocker.patch("subprocess.run")
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '{"funding_txid": "txid123"}'
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    txid, err = magma_module.execute_lnd_command("pubkey", 10, None, 100000, 500)
    
    assert txid == "txid123"
    assert err is None
    
    args = mock_run.call_args[0][0]
    assert "openchannel" in args
    assert "--fee_rate_ppm" in args
    fee_index = args.index("--fee_rate_ppm")
    assert args[fee_index + 1] == "500"


def test_execute_lnd_command_failure(magma_module, mocker):
    """Test failure opening a channel."""
    mock_run = mocker.patch("subprocess.run")
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "not enough funds"
    mock_run.return_value = mock_result

    txid, err = magma_module.execute_lnd_command("pubkey", 10, None, 100000, 500)
    
    assert txid is None
    assert "not enough funds" in err
