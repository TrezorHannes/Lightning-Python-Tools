
import sys
import os
import pytest
from unittest.mock import MagicMock

# --- FIXTURE: Mock Global Side Effects ---
@pytest.fixture(scope="module", autouse=True)
def mock_dependencies():
    """
    Patcher fixture that runs BEFORE the test module logic is fully utilized.
    Since 'import magma_sale_process' has side effects, we patch sys.modules 
    so the import uses our mocks.
    """
    mock_telebot = MagicMock()
    mock_telebot.TeleBot = MagicMock()
    mock_configparser = MagicMock()
    mock_logging = MagicMock()
    mock_schedule = MagicMock()
    
    # Mock config dict
    mock_config_data = {
        "telegram": {"magma_bot_token": "fake_token", "telegram_user_id": "123"},
        "credentials": {"amboss_authorization": "fake_auth"},
        "system": {"full_path_bos": "/path/to/bos"},
        "magma": {"invoice_expiry_seconds": "1800", "max_fee_percentage_of_invoice": "0.9", "channel_fee_rate_ppm": "350"},
        "urls": {"mempool_fees_api": "https://mempool.space/api/v1/fees/recommended"},
        "pubkey": {"banned_magma_pubkeys": ""},
        "paths": {"lncli_path": "lncli"}
    }
    
    mock_config_instance = MagicMock()
    mock_config_instance.__getitem__.side_effect = mock_config_data.__getitem__
    mock_config_instance.get = MagicMock(side_effect=lambda section, option, fallback=None: mock_config_data.get(section, {}).get(option, fallback))
    mock_config_instance.getint = MagicMock(return_value=10)
    mock_config_instance.getfloat = MagicMock(return_value=0.5)
    mock_configparser.ConfigParser.return_value = mock_config_instance

    module_patches = {
        'telebot': mock_telebot,
        'telebot.types': MagicMock(),
        'configparser': mock_configparser,
        'schedule': mock_schedule,
        'logging.handlers': MagicMock(),
        # We don't actully want to strictly mock logging or it suppresses output, but we prevent file handler creation
    }

    from unittest.mock import patch, mock_open
    
    # Apply patches
    with patch.dict(sys.modules, module_patches):
        with patch("builtins.open", mock_open(read_data="[magma]\nfoo=bar")):
            with patch("os.makedirs"):
                 # Normally we'd import here.
                 # However, since we are inside a fixture, and pytest collects modules first,
                 # we need to ensure the import happens strictly under this context.
                 # But python imports are cached.
                 
                 # To make this robust, we import inside the test functions OR use 'importlib.reload' if needed.
                 # But since we use autouse=True scope=module, tests in this file will "see" the mocked modules 
                 # if we import right here or if we import at top level BUT rely on this fixture running first?
                 # No, top level imports happen at collection time.
                 # So we MUST move the import `import magma_sale_process` INTO the test functions or a fixture that returns the module.
                 yield

@pytest.fixture
def magma_module(mock_dependencies):
    """
    Imports and returns the magma_sale_process module ensuring it is mocked.
    """
    # Verify we can import it now
    # We might need to handle sys.path if pyproject.toml didn't kick in yet or for safety
    if os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Magma')) not in sys.path:
         sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Magma')))
    
    import magma_sale_process
    # Reset vital mocks
    magma_sale_process.requests = MagicMock()
    return magma_sale_process

# --- TESTS ---

def test_get_node_alias_success(magma_module):
    """Test retrieving node alias successfully."""
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
    
    # Strict Argument Checking
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    
    # Check that --amt matches the passed amount 1000
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
    """Test accepting an order on Amboss."""
    mock_response = {"data": {"sellerAcceptOrder": True}} 
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    mock_post.raise_for_status.return_value = None
    magma_module.requests.post = MagicMock(return_value=mock_post)

    result = magma_module.accept_order("order123", "lnbc123")
    assert result == mock_response

def test_reject_order_success(magma_module):
    """Test rejecting an order on Amboss."""
    mock_response = {"data": {"sellerRejectOrder": True}}
    mock_post = MagicMock()
    mock_post.json.return_value = mock_response
    magma_module.requests.post = MagicMock(return_value=mock_post)

    result = magma_module.reject_order("order123")
    assert result == mock_response

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
    
    # Strict Argument Checking
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
