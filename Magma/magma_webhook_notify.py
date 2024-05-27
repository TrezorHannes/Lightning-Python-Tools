import apprise
import configparser
import os
import json
import hmac
from flask import Flask, request, jsonify
import logging

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)
SECRET_KEY = config['credentials']['webhook_secret_key']

#Load Config Values
TOKEN = config['telegram']['magma_bot_token']
CHAT_ID = config['telegram']['telegram_user_id']

# Create Apprise object
apobj = apprise.Apprise()

# Construct the Telegram notification URL
telegram_url = f'tgram://{TOKEN}/{CHAT_ID}'

# Add the Telegram notification service
apobj.add(telegram_url)

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')  # Set overall log level
info_logger = logging.getLogger('info')
error_logger = logging.getLogger('error')
error_logger.setLevel(logging.ERROR)

# Webhook endpoint
@app.route('/webhooks/amboss', methods=['POST'])
def handle_webhook():
    # Signature validation
    signature = request.headers.get('X-Amboss-Signature')
    if not signature:
        error_logger.error("Missing signature in webhook request")
        return jsonify({"error": "Missing signature"}), 401

    payload = request.get_data()
    expected_signature = hmac.new(SECRET_KEY.encode(), payload, 'sha256').hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        error_logger.error("Invalid signature in webhook request")
        return jsonify({"error": "Invalid signature"}), 401

    data = request.get_json()
    event_type = data.get('event_type')  # Using 'event_type' instead of 'event'

    info_logger.info(f"Received webhook event: {event_type}, Data: {data}")

    match event_type:
        case "MAGMA":
            order_id = data.get('payload', {}).get('order_id')
            notification_message = f"Magma order created: {order_id}"

        case "OPENCHANNEL":
            edge = data.get('payload', {}).get('edge', {})
            chan_id = edge.get('chan_id')
            notification_message = f"Channel opened: {chan_id}"

        case "validation push":
            notification_message = "Validation push received from Amboss."

        case _:  # Catch-all for other event types
            notification_message = f"Unknown event type: {event_type}"

    apobj.notify(
        body=notification_message,
        title='Amboss Webhook Notification'
    )

    return jsonify({"message": "Webhook received successfully"}), 200


if __name__ == '__main__':
    app.run(host='10.8.1.2', port=8000)
