import requests
import hmac
import hashlib
import time
import logging
from logging.handlers import RotatingFileHandler
from cryptography.fernet import Fernet
from multipleapis import ENCRYPTED_API_KEYS
import aiohttp
import certifi
import ssl

# SSL Context
ssl_context = ssl.create_default_context(cafile=certifi.where())

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up a rotating file handler to keep trade logs
trade_log_handler = RotatingFileHandler('futures_trade_log.txt', maxBytes=5000000, backupCount=5)
trade_log_handler.setLevel(logging.INFO)
trade_logger = logging.getLogger('trade_logger')
trade_logger.addHandler(trade_log_handler)
trade_logger.setLevel(logging.INFO)

BASE_URL = 'https://api.bybit.com/'

# Function to decrypt the API credentials
def decrypt_api_keys(account_name):
    encrypted_keys = ENCRYPTED_API_KEYS.get(account_name)
    if not encrypted_keys:
        raise ValueError(f"Account {account_name} not found")

    encryption_key = encrypted_keys['ENCRYPTION_KEY']
    cipher_suite = Fernet(encryption_key)

    try:
        api_key = cipher_suite.decrypt(encrypted_keys['API_KEY']).decode()
        api_secret = cipher_suite.decrypt(encrypted_keys['API_SECRET']).decode()
        return api_key, api_secret
    except Exception as e:
        logger.error(f"Decryption failed: {str(e)}")
        raise e

# Helper function to generate API signature
def generate_signature(secret, params):
    param_str = '&'.join([f'{key}={value}' for key, value in sorted(params.items())])
    return hmac.new(secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()

# Function to fetch precision and prices
async def fetch_precision_and_prices(account_name, symbol, session):
    api_key, api_secret = decrypt_api_keys(account_name)

    precision_url = f"{BASE_URL}v5/market/instruments-info?symbol={symbol}&category=linear"
    prices_url = f"{BASE_URL}v5/market/tickers?symbol={symbol}&category=linear"

    async with session.get(precision_url, ssl=ssl_context) as precision_response, session.get(prices_url, ssl=ssl_context) as prices_response:
        precision_data = await precision_response.json()
        prices_data = await prices_response.json()

        if precision_response.status != 200 or prices_response.status != 200:
            logger.error(f"Error fetching precision and prices.")
            return None

        instrument = precision_data['result']['list'][0]
        tick_size = instrument['priceFilter']['tickSize']
        qty_step = instrument['lotSizeFilter'].get('qtyStep', '0.0001')
        price_precision = len(tick_size.rstrip('0').split('.')[1]) if '.' in tick_size else 0
        qty_precision = len(qty_step.rstrip('0').split('.')[1]) if '.' in qty_step else 0

        ticker = prices_data['result']['list'][0]
        bid_price = float(ticker.get('bid1Price'))
        ask_price = float(ticker.get('ask1Price'))

        return price_precision, qty_precision, bid_price, ask_price
# check balance
async def check_balance(account_name, session):
    api_key, api_secret = decrypt_api_keys(account_name)
    balance_url = f"{BASE_URL}v5/account/wallet-balance?accountType=CONTRACT"

    params = {
        'api_key': api_key,
        'timestamp': str(int(time.time() * 1000)),
    }

    params['sign'] = generate_signature(api_secret, params)

    headers = {'Content-Type': 'application/json'}
    async with session.get(balance_url, params=params, ssl=ssl_context) as response:
        balance_data = await response.json()
        if response.status == 200 and balance_data['retCode'] == 0:
            logger.info(f"Balance fetched successfully: {balance_data}")
            return balance_data['result']['list'][0]['availableBalance']  # Adjust for the specific account you need
        else:
            logger.error(f"API Error fetching balance: {balance_data.get('retMsg', 'No error message')}")
            return None


# Function to calculate TP/SL prices
def calculate_tp_sl(price, tp_type, tp_value, sl_type, sl_value, precision, side):
    if side.lower() == 'buy':
        tp_price = round(price * (1 + tp_value / 100), precision) if tp_type == 'percentage' else round(price + tp_value, precision)
        sl_price = round(price * (1 - sl_value / 100), precision) if sl_type == 'percentage' else round(price - sl_value, precision)
    elif side.lower() == 'sell':
        tp_price = round(price * (1 - tp_value / 100), precision) if tp_type == 'percentage' else round(price - tp_value, precision)
        sl_price = round(price * (1 + sl_value / 100), precision) if sl_type == 'percentage' else round(price + sl_value, precision)
    return tp_price, sl_price


# Function to calculate limit price
def calculate_limit_price(base_price, direction, variance, variance_type, precision):
    """Calculates limit price with a variance (percentage or dollar) to ensure it's a maker order."""

    if variance_type == 'percentage':
        # Calculate the percentage variance correctly
        variance = variance / 100  # Convert the percentage to decimal
        limit_price = base_price * (1 - variance) if direction == 'buy' else base_price * (1 + variance)

    elif variance_type == 'dollar':
        limit_price = base_price - variance if direction == 'buy' else base_price + variance

    return round(limit_price, precision)


# Function to place spot margin order
async def place_spot_margin_order(account_name, symbol, side="Buy", price=None, price_option=None, tp_type=None,
                                  tp_value=None, sl_type=None, sl_value=None, leverage=10, tpsl_mode="Full",
                                  tp_order_type="Limit", sl_order_type="Market", market_unit='baseCoin', session=None,
                                  quantity=None):
    try:
        api_key, api_secret = decrypt_api_keys(account_name)
        logger.info(f"Placing spot margin order for symbol: {symbol}, side: {side}, leverage: {leverage}")

        # Fetch precision and prices
        precision_result = await fetch_precision_and_prices(account_name, symbol, session)
        if not precision_result:
            logger.error("Failed to fetch precision and prices.")
            return
        price_precision, qty_precision, bid_price, ask_price = precision_result

        # Determine base price for the limit order
        if price is not None:
            limit_price = round(float(price), price_precision)
        elif price_option:
            base_price = ask_price if price_option['type'] == 'ask' else bid_price
            limit_price = calculate_limit_price(base_price, price_option['direction'], price_option['variation_value'], price_option['variation_type'], price_precision)
        else:
            limit_price = ask_price if side.lower() == 'buy' else bid_price

        # If quantity is provided in the payload, use it directly
        if quantity is not None:
            quantity = round(float(quantity), qty_precision)
        else:
            account_balance = await get_unified_account_balance(account_name, 'USDC', session)
            if account_balance is None or account_balance <= 0:
                logger.error("Failed to fetch account balance or insufficient balance.")
                return
            quantity = calculate_quantity(account_balance, leverage, limit_price, qty_precision, 0.0001)

        # Log the bid and ask price at the moment of placing the order
        record_trade_prices(symbol, bid_price, ask_price, side)

        # Calculate TP and SL
        tp_price, sl_price = calculate_tp_sl(limit_price, tp_type, tp_value, sl_type, sl_value, price_precision, side)

        # Prepare the order parameters
        params = {
            'category': 'spot',
            'symbol': symbol,
            'side': side.capitalize(),
            'orderType': 'Limit',
            'qty': str(quantity),
            'price': str(round(limit_price, price_precision)),
            'timeInForce': 'GTC',
            'isLeverage': 1,
            'leverage': str(leverage),
            'marketUnit': market_unit,
            'tpslMode': tpsl_mode,
            'api_key': api_key,
            'timestamp': str(int(time.time() * 1000))
        }

        if tp_price is not None:
            params['takeProfit'] = str(tp_price)
            params['tpTriggerPrice'] = str(limit_price)

        if sl_price is not None:
            params['stopLoss'] = str(sl_price)
            params['slOrderType'] = sl_order_type

        # Generate signature
        params['sign'] = generate_signature(api_secret, params)

        # Send the request
        url = f"{BASE_URL}v5/order/create"
        headers = {'Content-Type': 'application/json'}
        async with session.post(url, json=params, ssl=ssl_context) as response:
            response_data = await response.json()
            if response.status == 200 and response_data.get('retCode') == 0:
                logger.info(f"Order Placed Successfully: {response_data}")
            else:
                logger.error(f"API Error: {response_data.get('retMsg', 'No error message')}")

    except Exception as e:
        logger.error(f"Error placing spot margin order: {e}")


# Main function to place futures order with TP/SL
async def place_futures_order(account_name, symbol, side="Buy", qty=0.1, leverage=10, price=None, price_option=None,
                              tp_type=None, tp_value=None, sl_type=None, sl_value=None, session=None, trigger_by='LastPrice'):
    try:
        api_key, api_secret = decrypt_api_keys(account_name)
        logger.info(f"Placing futures order for symbol: {symbol}, side: {side}, leverage: {leverage}")

        # Fetch precision and prices
        precision_result = await fetch_precision_and_prices(account_name, symbol, session)
        if not precision_result:
            logger.error("Failed to fetch precision and prices.")
            return
        price_precision, qty_precision, bid_price, ask_price = precision_result

        # Determine base price for the limit order
        if price is not None:
            limit_price = round(float(price), price_precision)
        elif price_option:
            base_price = ask_price if price_option['type'] == 'ask' else bid_price
            limit_price = calculate_limit_price(base_price, price_option['direction'], price_option['variation_value'], price_option['variation_type'], price_precision)
        else:
            limit_price = ask_price if side.lower() == 'buy' else bid_price

        # Calculate TP and SL
        tp_price, sl_price = calculate_tp_sl(limit_price, tp_type, tp_value, sl_type, sl_value, price_precision, side)

        # Prepare the order parameters
        params = {
            'category': 'linear',
            'symbol': symbol,
            'side': side.capitalize(),
            'orderType': 'Limit',
            'qty': str(qty),
            'price': str(limit_price),
            'timeInForce': 'GTC',
            'leverage': str(leverage),
            'api_key': api_key,
            'timestamp': str(int(time.time() * 1000))
        }

        if tp_price is not None:
            params['takeProfit'] = str(tp_price)
            params['tpTriggerBy'] = trigger_by

        if sl_price is not None:
            params['stopLoss'] = str(sl_price)
            params['slTriggerBy'] = trigger_by

        # Generate signature
        params['sign'] = generate_signature(api_secret, params)

        # Send the request to place the order
        url = f"{BASE_URL}v5/order/create"
        headers = {'Content-Type': 'application/json'}
        async with session.post(url, json=params, ssl=ssl_context) as response:
            response_data = await response.json()
            if response.status == 200 and response_data.get('retCode') == 0:
                logger.info(f"Futures Order Placed Successfully: {response_data}")
            else:
                logger.error(f"API Error: {response_data.get('retMsg', 'No error message')}")

    except Exception as e:
        logger.error(f"Error placing futures order: {e}")

