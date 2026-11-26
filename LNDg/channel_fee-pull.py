import os
import tempfile
import requests
import configparser

DEFAULT_HTTP_TIMEOUT = 30

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, "..", "config.ini")
config = configparser.ConfigParser()
config.read(config_file_path)

# API endpoint URL
_lndg_base = config.get("lndg", "lndg_api_url", fallback="").strip().rstrip("/")
api_url = f"{_lndg_base}/api/channels?limit=500&is_open=true" if _lndg_base else ""

# Authentication credentials
username = config.get("credentials", "lndg_username", fallback="").strip()
password = config.get("credentials", "lndg_password", fallback="").strip()

# File path for storing data. This txt is populated to have charge-lnd pick it up
file_path = os.path.expanduser("~/.chargelnd/.config/0_fee.txt")

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkeys = [
    p.strip()
    for p in config.get("pubkey", "base_fee_ignore", fallback="").split(",")
    if p.strip()
]


def get_chan_ids_to_write():
    if not api_url:
        raise ValueError("Missing [lndg] lndg_api_url in config.ini")
    chan_ids_to_write = []  # Initialize the list
    try:
        # Make the API request with authentication
        response = requests.get(
            api_url, auth=(username, password), timeout=DEFAULT_HTTP_TIMEOUT
        )

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            if "results" in data:
                results = data["results"]
                for result in results:
                    remote_pubkey = result.get("remote_pubkey", "")
                    local_fee_rate = result.get("local_fee_rate", 0)
                    chan_id = result.get("chan_id", "")
                    if (
                        local_fee_rate == 0
                        and remote_pubkey not in ignore_remote_pubkeys
                    ):
                        chan_ids_to_write.append(chan_id)
        else:
            print(f"API request failed with status code: {response.status_code}")

    except Exception as e:
        print(f"Error: {e}")

    return chan_ids_to_write


def write_chan_ids_atomic(target_path, chan_ids):
    """Atomically write 0_fee.txt with 0o644 permissions."""
    target_dir = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".0_fee_", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            for chan_id in chan_ids:
                tmp_file.write(f"{chan_id}\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def main():
    chan_ids = get_chan_ids_to_write()
    print(f"Channel-IDs: {chan_ids}")
    write_chan_ids_atomic(file_path, chan_ids)
    if chan_ids:
        print("Data written to file.")
    else:
        print("No channel IDs to write. File has been emptied.")


if __name__ == "__main__":
    main()
