import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# --- MOCKING GLOBAL SIDE EFFECTS BEFORE IMPORT ---
# We must mock modules that cause side effects on import (telebot connection, config reading, logging file creation)

# Create mock objects
mock_telebot = MagicMock()
mock_telebot.TeleBot = MagicMock()
mock_configparser = MagicMock()
mock_logging = MagicMock()
mock_schedule = MagicMock()
mock_rotating_handler = MagicMock()

# Configure the mock config parser to behave like a dict-like object where needed
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
# Allow dictionary-style access config['section']['key']
mock_config_instance.__getitem__.side_effect = mock_config_data.__getitem__
mock_config_instance.get = MagicMock(side_effect=lambda section, option, fallback=None: mock_config_data.get(section, {}).get(option, fallback))
mock_config_instance.getint = MagicMock(return_value=10)
mock_config_instance.getfloat = MagicMock(return_value=0.5)
mock_configparser.ConfigParser.return_value = mock_config_instance

# Patch sys.modules to inject our mocks
# This prevents the real modules from being loaded/executed
module_patches = {
    'telebot': mock_telebot,
    'telebot.types': MagicMock(),
    'configparser': mock_configparser,
    'schedule': mock_schedule,
    # We don't actully want to strictly mock logging or it suppresses output, 
    # but we want to stop it from creating files.
    'logging.handlers': MagicMock(), 
}

# We need to setup these patches before importing the target module
with patch.dict(sys.modules, module_patches):
    # We also need to patch open() to prevent config file reading and log/flag file creation during import
    with patch("builtins.open", unittest.mock.mock_open(read_data="[magma]\nfoo=bar")):
        # We also need to prevent os.makedirs
        with patch("os.makedirs"):
             # Now we can import the module. 
             # We need to add the parent directory to sys.path so it can resolve relative imports if any (though it seems self-contained)
             sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Magma')))
             import magma_sale_process

# --- END PRE-IMPORT MOCKING ---

class TestMagmaSaleProcess(unittest.TestCase):

    def setUp(self):
        # Reset mocks before each test
        magma_sale_process.requests = MagicMock() # Mock requests module inside the imported module
        
        # Ensure constants are set to know values if needed (though we mocked config)
        magma_sale_process.AMBOSS_TOKEN = "test_token"
        magma_sale_process.LNCLI_PATH = "lncli"

    def test_get_node_alias_success(self):
        """Test retrieving node alias successfully."""
        mock_response = {
            "data": {
                "getNodeAlias": "TestNode"
            }
        }
        
        # Mock requests.post
        mock_post = MagicMock()
        mock_post.json.return_value = mock_response
        mock_post.raise_for_status.return_value = None
        magma_sale_process.requests.post = MagicMock(return_value=mock_post)

        alias = magma_sale_process.get_node_alias("pubkey123")
        self.assertEqual(alias, "TestNode")

    def test_get_node_alias_failure(self):
        """Test retrieving node alias when API fails."""
        # Mock requests.post to return no data
        mock_post = MagicMock()
        mock_post.json.return_value = {} # Empty JSON
        magma_sale_process.requests.post = MagicMock(return_value=mock_post)

        alias = magma_sale_process.get_node_alias("pubkey123")
        self.assertEqual(alias, "ErrorFetchingAlias")

    @patch("subprocess.Popen")
    def test_execute_lncli_addinvoice_success(self, mock_popen):
        """Test generating an invoice calls lncli correctly."""
        # Setup mock process
        process_mock = MagicMock()
        expected_json = '{"r_hash": "hash123", "payment_request": "lnbc..."}'
        process_mock.communicate.return_value = (expected_json.encode('utf-8'), b"")
        mock_popen.return_value = process_mock

        r_hash, pay_req = magma_sale_process.execute_lncli_addinvoice(1000, "memo", 3600)

        self.assertEqual(r_hash, "hash123")
        self.assertEqual(pay_req, "lnbc...")
        
        # Verify command arguments
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        self.assertIn("addinvoice", args)
        self.assertIn("1000", args)
        self.assertIn("3600", args)

    @patch("subprocess.Popen")
    def test_execute_lncli_addinvoice_failure(self, mock_popen):
        """Test error handling when lncli fails."""
        process_mock = MagicMock()
        process_mock.communicate.return_value = (b"", b"Error: something went wrong")
        mock_popen.return_value = process_mock

        r_hash, pay_req = magma_sale_process.execute_lncli_addinvoice(1000, "memo", 3600)
        
        self.assertTrue(r_hash.startswith("Error"))
        self.assertIsNone(pay_req)

    def test_accept_order_success(self):
        """Test accepting an order on Amboss."""
        # Mock successful mutation response
        mock_response = {"data": {"sellerAcceptOrder": True}} 
        mock_post = MagicMock()
        mock_post.json.return_value = mock_response
        mock_post.raise_for_status.return_value = None
        magma_sale_process.requests.post = MagicMock(return_value=mock_post)

        result = magma_sale_process.accept_order("order123", "lnbc123")
        
        self.assertEqual(result, mock_response)
        
        # Verify payload contains mutation
        call_args = magma_sale_process.requests.post.call_args
        self.assertIn('sellerAcceptOrder', call_args[1]['json']['query'])
        self.assertEqual(call_args[1]['json']['variables']['sellerAcceptOrderId'], "order123")

    def test_reject_order_success(self):
        """Test rejecting an order on Amboss."""
        mock_response = {"data": {"sellerRejectOrder": True}}
        mock_post = MagicMock()
        mock_post.json.return_value = mock_response
        magma_sale_process.requests.post = MagicMock(return_value=mock_post)

        result = magma_sale_process.reject_order("order123")
        self.assertEqual(result, mock_response)

    @patch("subprocess.run")
    def test_execute_lnd_command_success(self, mock_run):
        """Test successfully opening a channel."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"funding_txid": "txid123"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        txid, err = magma_sale_process.execute_lnd_command("pubkey", 10, None, 100000, 500)
        
        self.assertEqual(txid, "txid123")
        self.assertIsNone(err)
        
        # Verify CLI args
        args = mock_run.call_args[0][0]
        self.assertIn("openchannel", args)
        self.assertIn("pubkey", args)
        self.assertIn("500", args) # Fee rate ppm

    @patch("subprocess.run")
    def test_execute_lnd_command_failure(self, mock_run):
        """Test failure opening a channel."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "not enough funds"
        mock_run.return_value = mock_result

        txid, err = magma_sale_process.execute_lnd_command("pubkey", 10, None, 100000, 500)
        
        self.assertIsNone(txid)
        self.assertIn("not enough funds", err)

if __name__ == '__main__':
    unittest.main()
