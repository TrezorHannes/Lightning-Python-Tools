import sys
import os
import logging
import pytest
from unittest.mock import MagicMock, patch, mock_open

# --- FIXTURE: Mock Global Side Effects ---
@pytest.fixture(scope="module", autouse=True)
def mock_dependencies():
    mock_telebot = MagicMock()
    bot_mock = MagicMock()
    bot_mock.callback_query_handler = MagicMock(return_value=lambda f: f)
    bot_mock.message_handler = MagicMock(return_value=lambda f: f)
    mock_telebot.TeleBot = MagicMock(return_value=bot_mock)
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
    import requests as real_requests
    magma_sale_process.requests = MagicMock()
    magma_sale_process.requests.exceptions = real_requests.exceptions
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


def test_execute_lncli_addinvoice_with_route_hints(magma_module, mocker):
    """Test generating an invoice with route hints appends --private."""
    mock_popen = mocker.patch("subprocess.Popen")
    process_mock = MagicMock()
    expected_json = '{"r_hash": "hash_hint", "payment_request": "lnbc_hint..."}'
    process_mock.communicate.return_value = (expected_json.encode('utf-8'), b"")
    mock_popen.return_value = process_mock

    r_hash, pay_req = magma_module.execute_lncli_addinvoice(1000, "memo", 3600, include_route_hints=True)

    assert r_hash == "hash_hint"
    assert pay_req == "lnbc_hint..."
    args = mock_popen.call_args[0][0]
    assert "--private" in args



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


def test_get_offers_awaiting_seller_approval_banned_pubkey_reject_error_null_data(magma_module, mocker):
    """Test auto-rejection handles GraphQL errors with null data without raising AttributeError."""
    mock_response = {
        "data": {
            "user": {
                "market": {
                    "orders": {
                        "sales": {
                            "total": 1,
                            "list": [
                                {
                                    "id": "order_banned_02",
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
        return_value={"errors": [{"message": "Rejection failed"}], "data": None}
    )
    mocker.patch.object(magma_module, "send_telegram_notification")

    # Must NOT raise AttributeError: 'NoneType' object has no attribute 'get'
    offer = magma_module.get_offers_awaiting_seller_approval()
    assert offer is None
    mock_reject.assert_called_once_with("order_banned_02")



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


def test_extract_order_info_unsettled_zero_seller_fee_fallback(magma_module):
    """Test unsettled orders where fees.seller.sats is 0 fall back to fixed + variable fee sum."""
    unsettled_order = {
        "id": "order_unsettled_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "2000000"}},
        "fees": {
            "fixed": {"sats": "1000"},
            "variable": {"sats": "5000"},
            "seller": {"sats": "0"},
            "amboss": {"sats": "200"}
        },
        "destination": {"pubkey": "03buyer123", "alias": "FastBuyer"}
    }
    info = magma_module.extract_order_info(unsettled_order)
    assert info["id"] == "order_unsettled_001"
    assert info["seller_invoice_amount"] == 6000
    assert info["customer_pubkey"] == "03buyer123"
    assert info["channel_size"] == 2000000


def test_extract_order_info_idempotency(magma_module):
    """Test that extract_order_info is idempotent when given an already normalized dictionary."""
    sample_order = {
        "id": "order_norm_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "4000000"}},
        "fees": {
            "fixed": {"sats": "1500"},
            "variable": {"sats": "3500"},
            "seller": {"sats": "5000"},
            "amboss": {"sats": "500"}
        },
        "destination": {"pubkey": "02idempotent456", "alias": "IdemNode"}
    }
    first_pass = magma_module.extract_order_info(sample_order)
    second_pass = magma_module.extract_order_info(first_pass)

    assert second_pass["id"] == "order_norm_001"
    assert second_pass["customer_pubkey"] == "02idempotent456"
    assert second_pass["buyer_alias"] == "IdemNode"
    assert second_pass["channel_size"] == 4000000
    assert second_pass["seller_invoice_amount"] == 5000
    assert second_pass["fixed_fee"] == 1500
    assert second_pass["variable_fee"] == 3500


def test_handle_order_decision_callback_approve_success(magma_module, mocker):
    """Test approving order via Telegram callback processes GraphQL order without KeyError."""
    raw_graphql_order = {
        "id": "order_approve_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "3000000"}},
        "fees": {"seller": {"sats": "12000"}, "fixed": {"sats": "2000"}, "variable": {"sats": "10000"}},
        "destination": {"pubkey": "03approver123456", "alias": "ApproverNode"},
        "buyer_alias": "ApproverNode"
    }
    magma_module.pending_user_confirmations["order_approve_001"] = {
        "message_id": 1234,
        "timestamp": 1000.0,
        "details": raw_graphql_order
    }

    mock_complete = mocker.patch.object(magma_module, "_complete_offer_approval_process")
    mocker.patch.object(magma_module, "send_telegram_notification")

    mock_call = MagicMock()
    mock_call.id = "cb_id_1"
    mock_call.data = "decide_order:approve:order_approve_001"
    mock_call.message.chat.id = 5555
    mock_call.message.message_id = 1234

    magma_module.handle_order_decision_callback(mock_call)

    magma_module.bot.answer_callback_query.assert_called_with("cb_id_1", text="Order order_approve_001 Approved. Processing...")
    magma_module.bot.edit_message_text.assert_called()
    edit_text = magma_module.bot.edit_message_text.call_args[1]["text"]
    assert "12000 sats" in edit_text
    assert "ApproverNode" in edit_text
    assert "03approver" in edit_text

    assert "order_approve_001" not in magma_module.pending_user_confirmations

    import time
    time.sleep(0.05)
    assert mock_complete.called
    called_order_id, called_details = mock_complete.call_args[0]
    assert called_order_id == "order_approve_001"
    assert called_details["seller_invoice_amount"] == 12000


def test_handle_order_decision_callback_reject_success(magma_module, mocker):
    """Test rejecting order via Telegram callback rejects order on Amboss."""
    raw_graphql_order = {
        "id": "order_reject_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "2000000"}},
        "fees": {"seller": {"sats": "8000"}},
        "destination": {"pubkey": "02rejector123", "alias": "RejectNode"},
        "buyer_alias": "RejectNode"
    }
    magma_module.pending_user_confirmations["order_reject_001"] = {
        "message_id": 4321,
        "timestamp": 1000.0,
        "details": raw_graphql_order
    }
    mock_reject = mocker.patch.object(magma_module, "reject_order")
    mocker.patch.object(magma_module, "send_telegram_notification")

    mock_call = MagicMock()
    mock_call.id = "cb_id_2"
    mock_call.data = "decide_order:reject:order_reject_001"
    mock_call.message.chat.id = 5555
    mock_call.message.message_id = 4321

    magma_module.handle_order_decision_callback(mock_call)

    magma_module.bot.answer_callback_query.assert_called_with("cb_id_2", text="Order order_reject_001 Rejected. Processing...")
    magma_module.bot.edit_message_text.assert_called()
    assert "order_reject_001" not in magma_module.pending_user_confirmations

    import time
    time.sleep(0.05)
    assert mock_reject.called
    assert mock_reject.call_args[0][0] == "order_reject_001"


def test_handle_timeout_for_offer_auto_approve(magma_module, mocker):
    """Test timeout auto-approval workflow with GraphQL payload and fresh order fetch."""
    raw_graphql_order = {
        "id": "order_timeout_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "5000000"}},
        "fees": {"seller": {"sats": "15000"}, "fixed": {"sats": "3000"}, "variable": {"sats": "12000"}},
        "destination": {"pubkey": "02timeoutbuyer", "alias": "TimeoutBuyer"},
        "buyer_alias": "TimeoutBuyer"
    }
    confirmation_info = {
        "message_id": 7777,
        "timestamp": 500.0,
        "details": raw_graphql_order
    }

    fresh_order = {
        "id": "order_timeout_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "5000000"}},
        "fees": {"seller": {"sats": "15000"}},
        "destination": {"pubkey": "02timeoutbuyer", "alias": "TimeoutBuyer"}
    }

    mocker.patch.object(magma_module, "get_order_details_from_amboss", return_value=fresh_order)
    mock_complete = mocker.patch.object(magma_module, "_complete_offer_approval_process")
    mocker.patch.object(magma_module, "send_telegram_notification")

    magma_module._handle_timeout_for_offer("order_timeout_001", confirmation_info)

    magma_module.bot.edit_message_text.assert_called()
    edit_text = magma_module.bot.edit_message_text.call_args[1]["text"]
    assert "Auto-Approved (Timeout)" in edit_text
    assert "15000 sats" in edit_text

    assert mock_complete.called
    called_id, called_fresh = mock_complete.call_args[0]
    assert called_id == "order_timeout_001"
    assert called_fresh["seller_invoice_amount"] == 15000
    assert called_fresh["customer_pubkey"] == "02timeoutbuyer"


def test_handle_timeout_for_offer_exception_resilience(magma_module, mocker):
    """Test timeout auto-approval handles exceptions without terminating the scheduler loop."""
    broken_confirmation_info = {
        "message_id": 8888,
        "timestamp": 500.0,
        "details": None
    }
    mock_send = mocker.patch.object(magma_module, "send_telegram_notification")

    # Must NOT raise exception
    magma_module._handle_timeout_for_offer("order_broken_001", broken_confirmation_info)
    assert mock_send.called


def test_complete_offer_approval_process_raw_and_normalized(magma_module, mocker):
    """Test _complete_offer_approval_process works seamlessly with raw GraphQL order."""
    raw_graphql_order = {
        "id": "order_proc_001",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "1000000"}},
        "fees": {"seller": {"sats": "4500"}},
        "destination": {"pubkey": "03buyerproc", "alias": "ProcNode"},
        "buyer_alias": "ProcNode"
    }

    mocker.patch.object(magma_module, "execute_lncli_addinvoice", return_value=("hash_p", "lnbc4500..."))
    mocker.patch.object(magma_module, "accept_order", return_value={
        "data": {"market": {"order": {"seller": {"accept": {"success": True}}}}}
    })
    mock_wait = mocker.patch.object(magma_module, "wait_for_buyer_payment")
    mocker.patch.object(magma_module, "send_telegram_notification")

    magma_module._complete_offer_approval_process("order_proc_001", raw_graphql_order)

    assert magma_module.execute_lncli_addinvoice.called
    assert magma_module.execute_lncli_addinvoice.call_args[0][0] == 4500
    assert magma_module.accept_order.called
    assert magma_module.accept_order.call_args[0] == ("order_proc_001", "lnbc4500...")
    assert mock_wait.called
    assert mock_wait.call_args[0][0] == "order_proc_001"


def test_complete_offer_approval_process_graphql_error_null_data(magma_module, mocker):
    """Test _complete_offer_approval_process handles GraphQL errors with null data gracefully without crashing."""
    raw_graphql_order = {
        "id": "order_proc_002",
        "status": "WAITING_FOR_SELLER_APPROVAL",
        "amount": {"satoshi": {"sats": "5000000"}},
        "fees": {"seller": {"sats": "15926"}},
        "destination": {"pubkey": "03buyer", "alias": "BuyerNode"},
        "buyer_alias": "BuyerNode"
    }

    mocker.patch.object(magma_module, "execute_lncli_addinvoice", return_value=("hash_err", "lnbc15926..."))
    mocker.patch.object(magma_module, "accept_order", return_value={
        "errors": [
            {
                "message": "Unable to find a route to this destination! Please reach out to support!",
                "locations": [{"line": 6, "column": 9}],
                "path": ["market", "order", "seller", "accept"],
                "extensions": {"code": "INTERNAL_SERVER_ERROR"}
            }
        ],
        "data": None
    })
    mock_wait = mocker.patch.object(magma_module, "wait_for_buyer_payment")
    mock_send = mocker.patch.object(magma_module, "send_telegram_notification")
    mock_file_open = mocker.patch("builtins.open", mocker.mock_open())

    # Must NOT raise AttributeError: 'NoneType' object has no attribute 'get'
    magma_module._complete_offer_approval_process("order_proc_002", raw_graphql_order)

    assert not mock_wait.called
    assert mock_send.called
    # Check that error notification contains the actual Amboss error detail
    sent_messages = [call[0][0] for call in mock_send.call_args_list]
    assert any("Unable to find a route to this destination" in msg for msg in sent_messages)
    # Ensure critical error flag file was NOT created/written to
    assert not any(call[0][0] == magma_module.CRITICAL_ERROR_FILE_PATH for call in mock_file_open.call_args_list)


def test_execute_bot_behavior_critical_flag_silenced_to_warning(magma_module, mocker, caplog):
    """Test that existing CRITICAL_ERROR_FILE_PATH suspends bot without sending repeated Telegram alerts."""
    mocker.patch("os.path.exists", return_value=True)
    mock_new_offers = mocker.patch.object(magma_module, "process_new_offers")
    mock_paid_orders = mocker.patch.object(magma_module, "process_paid_orders_for_channel_opening")
    magma_module.bot.send_message.reset_mock()

    with caplog.at_level(logging.WARNING):
        magma_module.execute_bot_behavior()

    # Behavior must be suspended
    assert not mock_new_offers.called
    assert not mock_paid_orders.called

    # Must log at WARNING level, NOT CRITICAL, and NOT dispatch to Telegram
    warning_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
    critical_logs = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert any("CRITICAL ERROR FLAG" in r.message and "suspended" in r.message for r in warning_logs)
    assert not any("CRITICAL ERROR FLAG" in r.message for r in critical_logs)
    assert not magma_module.bot.send_message.called



def test_telegram_logging_handler_captures_errors_and_tracebacks(magma_module):
    """Test TelegramLoggingHandler dispatches ERROR/CRITICAL logs and tracebacks to Telegram."""
    handler = magma_module.TelegramLoggingHandler(bot=magma_module.bot, chat_id=123)

    # 1. Error without exc_info
    record1 = logging.LogRecord(
        name="magma_sale_process",
        level=logging.ERROR,
        pathname="magma_sale_process.py",
        lineno=100,
        msg="Critical database failure: %s",
        args=("disk full",),
        exc_info=None
    )
    handler.emit(record1)
    magma_module.bot.send_message.assert_called()
    sent_text1 = magma_module.bot.send_message.call_args[1]["text"]
    assert "Critical database failure: disk full" in sent_text1
    assert "ERROR" in sent_text1

    # 2. Error with exc_info (traceback)
    try:
        raise ValueError("Simulated failure for traceback testing")
    except ValueError:
        import sys
        exc_info = sys.exc_info()

    record2 = logging.LogRecord(
        name="magma_sale_process",
        level=logging.ERROR,
        pathname="magma_sale_process.py",
        lineno=105,
        msg="Caught unexpected exception",
        args=(),
        exc_info=exc_info
    )
    handler.emit(record2)
    sent_text2 = magma_module.bot.send_message.call_args[1]["text"]
    assert "Caught unexpected exception" in sent_text2
    assert "Simulated failure for traceback testing" in sent_text2
    assert "Traceback" in sent_text2


def test_telegram_logging_handler_recursion_and_filters(magma_module):
    """Test TelegramLoggingHandler guards against recursion and suppresses excluded logs."""
    handler = magma_module.TelegramLoggingHandler(bot=magma_module.bot, chat_id=123)
    magma_module.bot.send_message.reset_mock()

    # Filter telebot/urllib3/requests logs
    for noisy_logger in ["telebot", "urllib3", "requests", "telebot.apihelper"]:
        rec = logging.LogRecord(name=noisy_logger, level=logging.ERROR, pathname="foo.py", lineno=1, msg="Network drop", args=(), exc_info=None)
        handler.emit(rec)
    assert not magma_module.bot.send_message.called

    # Filter notifications already being sent
    rec_notif = logging.LogRecord(name="magma_sale_process", level=logging.ERROR, pathname="foo.py", lineno=1, msg="Telegram NOTIFICATION: 🔥 Error occurred", args=(), exc_info=None)
    handler.emit(rec_notif)
    assert not magma_module.bot.send_message.called

    # Recursion guard: bot.send_message fails with exception
    magma_module.bot.send_message.side_effect = Exception("Telegram API down")
    rec_error = logging.LogRecord(name="magma_sale_process", level=logging.ERROR, pathname="foo.py", lineno=1, msg="Some real error", args=(), exc_info=None)
    # Should not raise exception
    handler.emit(rec_error)
    magma_module.bot.send_message.side_effect = None


def test_telegram_logging_handler_suppresses_telebot_pascal_case_and_infinity_polling(magma_module):
    """Test TelegramLoggingHandler suppresses PascalCase TeleBot records and infinity_polling exceptions."""
    handler = magma_module.TelegramLoggingHandler(bot=magma_module.bot, chat_id=123)
    magma_module.bot.send_message.reset_mock()

    # Case-insensitive checks: TeleBot, telebot, TELEBOT, TeleBot.apihelper, urllib3.connectionpool
    noisy_loggers = [
        "TeleBot",
        "telebot",
        "TELEBOT",
        "TeleBot.apihelper",
        "telebot.apihelper",
        "urllib3.connectionpool",
        "requests.packages.urllib3",
    ]
    for logger_name in noisy_loggers:
        rec = logging.LogRecord(
            name=logger_name,
            level=logging.ERROR,
            pathname="foo.py",
            lineno=1021,
            msg="Connection read timeout",
            args=(),
            exc_info=None,
        )
        handler.emit(rec)
    assert not magma_module.bot.send_message.called

    # Failsafe: funcName is infinity_polling or message contains Infinity polling exception
    rec_poll1 = logging.LogRecord(
        name="some_logger",
        level=logging.ERROR,
        pathname="__init__.py",
        lineno=1021,
        msg="Infinity polling exception: HTTPSConnectionPool: Read timed out.",
        args=(),
        exc_info=None,
        func="infinity_polling",
    )
    handler.emit(rec_poll1)
    assert not magma_module.bot.send_message.called


def test_execute_amboss_graphql_request_timeout_demoted_to_warning(magma_module, mocker, caplog):
    """Test that transient HTTP timeouts during GraphQL queries log as WARNING and do not dispatch error alerts."""
    import requests
    mocker.patch.object(magma_module.requests, "post", side_effect=requests.exceptions.Timeout("Amboss 20s timeout"))
    magma_module.bot.send_message.reset_mock()

    with caplog.at_level(logging.WARNING):
        result = magma_module._execute_amboss_graphql_request({"query": "{ test }"}, "TestTimeoutQuery")

    assert result is None
    # Verify logged as WARNING, not ERROR
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Timeout during TestTimeoutQuery to Amboss" in r.message for r in warning_records)
    assert not any("Timeout during TestTimeoutQuery to Amboss" in r.message for r in error_records)
    # Ensure no Telegram alert was dispatched
    assert not magma_module.bot.send_message.called


def test_telegram_logging_handler_no_duplicate_dispatches(magma_module):
    """Test that logging error on root logger dispatches exactly one Telegram message."""
    root_logger = logging.getLogger()
    telegram_handlers = [h for h in root_logger.handlers if isinstance(h, magma_module.TelegramLoggingHandler)]
    assert len(telegram_handlers) == 1

    magma_module.bot.send_message.reset_mock()
    logging.error("Single operational failure on root logger")
    assert magma_module.bot.send_message.call_count == 1

