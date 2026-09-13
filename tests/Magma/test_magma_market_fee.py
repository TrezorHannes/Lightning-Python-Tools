import sys
import os
import pytest
from unittest.mock import MagicMock, patch, mock_open

@pytest.fixture(scope="module", autouse=True)
def mock_fee_dependencies():
    mock_telebot = MagicMock()
    mock_telebot.TeleBot = MagicMock()
    mock_configparser = MagicMock()
    mock_logging = MagicMock()
    
    mock_config_data = {
        "credentials": {"amboss_authorization": "fake_auth"},
        "info": {"NODE": "03mypubkey123456"},
        "system": {"log_level": "INFO"},
        "market_analysis": {
            "min_seller_score_filter": "75.0",
            "pricing_percentile_ppm": "50",
            "pricing_percentile_fixed": "50"
        },
        "capital_management": {
            "min_onchain_reserve_sats": "100000",
            "max_capital_allocation_percentage": "80"
        },
        "paths": {"lncli_path": "lncli"}
    }
    
    mock_config_instance = MagicMock()
    mock_config_instance.__getitem__.side_effect = mock_config_data.__getitem__
    mock_config_instance.get = MagicMock(side_effect=lambda section, option, fallback=None: mock_config_data.get(section, {}).get(option, fallback))
    mock_config_instance.getint = MagicMock(return_value=1000)
    mock_config_instance.getfloat = MagicMock(return_value=75.0)
    mock_config_instance.has_option = MagicMock(return_value=True)
    mock_config_instance.has_section = MagicMock(return_value=True)
    mock_config_instance.sections = MagicMock(return_value=["template_1"])
    mock_configparser.ConfigParser.return_value = mock_config_instance

    module_patches = {
        'telebot': mock_telebot,
        'configparser': mock_configparser,
        'logging.handlers': MagicMock(),
    }

    with patch.dict(sys.modules, module_patches):
        with patch("builtins.open", mock_open(read_data="[market_analysis]\nfoo=bar")):
            with patch("os.makedirs"):
                yield

@pytest.fixture
def fee_module(mock_fee_dependencies):
    if os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Magma')) not in sys.path:
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Magma')))
    
    import magma_market_fee
    magma_market_fee.requests = MagicMock()
    magma_market_fee.AMBOSS_TOKEN = "fake_auth"
    magma_market_fee.MY_NODE_PUBKEY = "03mypubkey123456"
    return magma_market_fee

# --- TESTS ---

def test_extract_market_offer_info_new_api(fee_module):
    """Test extracting normalized fields from live-verified SimpleMarketOffer schema."""
    sample_offer = {
        "id": "off_001",
        "status": "ENABLED",
        "side": "SELL",
        "node": {"pubkey": "03othernode999", "alias": "PeerAlias"},
        "total_amount": {
            "satoshi": {"sats": "10000000"}
        },
        "locked_amount": {
            "satoshi": {"sats": "3000000"}
        },
        "fees": {
            "fixed": {"sats": "1000"},
            "variable": {"sats": "450"},
            "amboss": {"sats": "100"}
        },
        "promises": {
            "min_block_length": 4320,
            "base_fee_cap": "1000",
            "fee_rate_cap": "450"
        },
        "seller_score": 92.5
    }

    info = fee_module.extract_market_offer_info(sample_offer)

    assert info["id"] == "off_001"
    assert info["status"] == "ENABLED"
    assert info["side"] == "SELL"
    assert info["account"] == "03othernode999"
    assert info["node_alias"] == "PeerAlias"
    assert info["total_size"] == 10000000
    assert info["locked_size"] == 3000000
    assert info["available_size"] == 7000000
    assert info["base_fee"] == 1000
    assert info["fee_rate"] == 450
    assert info["min_block_length"] == 4320
    assert info["seller_score"] == 92.5


def test_calculate_apr(fee_module):
    """Test APR calculation formula."""
    apr = fee_module.calculate_apr(1000, 500, 5000000, 30.0)
    assert 0.85 <= apr <= 0.86


def test_fetch_public_magma_offers_filtering(fee_module):
    """Test fetching public offers filters out own pubkey and low seller scores."""
    mock_response = {
        "data": {
            "market": {
                "offer": {
                    "offers": {
                        "total": 3,
                        "list": [
                            # 1. Valid other node offer (score 90 >= 75)
                            {
                                "id": "off_valid",
                                "status": "ENABLED",
                                "side": "SELL",
                                "node": {"pubkey": "03peer999", "alias": "PeerNode"},
                                "total_amount": {"satoshi": {"sats": "5000000"}},
                                "locked_amount": {"satoshi": {"sats": "0"}},
                                "fees": {"fixed": {"sats": "500"}, "variable": {"sats": "300"}},
                                "promises": {"min_block_length": 4320},
                                "seller_score": 90.0
                            },
                            # 2. Own node offer (should be excluded)
                            {
                                "id": "off_own",
                                "status": "ENABLED",
                                "side": "SELL",
                                "node": {"pubkey": "03mypubkey123456", "alias": "MyNode"},
                                "total_amount": {"satoshi": {"sats": "5000000"}},
                                "locked_amount": {"satoshi": {"sats": "0"}},
                                "fees": {"fixed": {"sats": "500"}, "variable": {"sats": "300"}},
                                "promises": {"min_block_length": 4320},
                                "seller_score": 95.0
                            },
                            # 3. Low seller score (60 < 75, should be excluded)
                            {
                                "id": "off_low_score",
                                "status": "ENABLED",
                                "side": "SELL",
                                "node": {"pubkey": "03lowscore111", "alias": "LowScoreNode"},
                                "total_amount": {"satoshi": {"sats": "5000000"}},
                                "locked_amount": {"satoshi": {"sats": "0"}},
                                "fees": {"fixed": {"sats": "500"}, "variable": {"sats": "300"}},
                                "promises": {"min_block_length": 4320},
                                "seller_score": 60.0
                            }
                        ]
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    fee_module.requests.post = MagicMock(return_value=mock_post)

    magma_config = MagicMock()
    magma_config.get = MagicMock(return_value="75.0")

    offers = fee_module.fetch_public_magma_offers("03mypubkey123456", magma_config)
    assert len(offers) == 1
    assert offers[0]["id"] == "off_valid"


def test_fetch_my_current_offers(fee_module):
    """Test fetching user's own Magma sell offers."""
    mock_response = {
        "data": {
            "user": {
                "market": {
                    "offers": {
                        "offers": {
                            "total": 1,
                            "list": [
                                {
                                    "id": "my_off_01",
                                    "status": "ENABLED",
                                    "side": "SELL",
                                    "node": {"pubkey": "03mypubkey123456", "alias": "MyNode"},
                                    "total_amount": {"satoshi": {"sats": "15000000"}},
                                    "locked_amount": {"satoshi": {"sats": "5000000"}},
                                    "fees": {"fixed": {"sats": "1000"}, "variable": {"sats": "400"}},
                                    "promises": {"min_block_length": 4320},
                                    "seller_score": 98.0
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
    fee_module.requests.post = MagicMock(return_value=mock_post)

    my_offers = fee_module.fetch_my_current_offers()
    assert len(my_offers) == 1
    assert my_offers[0]["id"] == "my_off_01"
    assert my_offers[0]["available_size"] == 10000000


def test_create_magma_offer_mutation(fee_module):
    """Test creating a Magma offer via market.offer.create."""
    mock_response = {
        "data": {
            "market": {
                "offer": {
                    "create": {
                        "offer_id": "new_off_123"
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    fee_module.requests.post = MagicMock(return_value=mock_post)
    fee_module.DRY_RUN_MODE = False

    pricing = {
        "duration_days": 30,
        "fixed_fee_sats": 1000,
        "ppm_fee_rate": 500,
        "channel_size_sats": 5000000
    }
    result = fee_module.create_magma_offer(pricing, 10000000, "Template30D")
    assert result is not None
    assert result["id"] == "new_off_123"


def test_update_magma_offer_mutation(fee_module):
    """Test updating a Magma offer via market.offer.update."""
    mock_response = {
        "data": {
            "market": {
                "offer": {
                    "update": {
                        "success": True
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    fee_module.requests.post = MagicMock(return_value=mock_post)
    fee_module.DRY_RUN_MODE = False

    pricing = {
        "duration_days": 30,
        "fixed_fee_sats": 1200,
        "ppm_fee_rate": 550,
        "channel_size_sats": 5000000
    }
    result = fee_module.update_magma_offer("off_123", pricing, 10000000, "Template30D")
    assert result is not None
    assert result["status"] == "UPDATED"


def test_toggle_magma_offer_status(fee_module):
    """Test toggling a Magma offer status via market.offer.toggle."""
    mock_response = {
        "data": {
            "market": {
                "offer": {
                    "toggle": {
                        "status": "DISABLED"
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    fee_module.requests.post = MagicMock(return_value=mock_post)
    fee_module.DRY_RUN_MODE = False

    success = fee_module.toggle_magma_offer_status("off_123", "Template30D", "DISABLED")
    assert success is True


def test_dry_run_mode_simulation(fee_module):
    """Test that DRY_RUN_MODE simulates mutations without network calls."""
    fee_module.DRY_RUN_MODE = True
    pricing = {
        "duration_days": 30,
        "fixed_fee_sats": 1000,
        "ppm_fee_rate": 500,
        "channel_size_sats": 5000000
    }

    create_res = fee_module.create_magma_offer(pricing, 10000000, "DryTemplate")
    assert "dryrun-offer-id" in create_res["id"]

    update_res = fee_module.update_magma_offer("off_dry", pricing, 10000000, "DryTemplate")
    assert update_res["status"] == "DRY_RUN_STATUS_POST_UPDATE"

    toggle_res = fee_module.toggle_magma_offer_status("off_dry", "DryTemplate", "DISABLED")
    assert toggle_res is True
