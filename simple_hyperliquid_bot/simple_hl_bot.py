from dotenv import load_dotenv
load_dotenv()  # take environment variables from .env.
import collections
import time
import eth_account
import signal

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
import os

# define our risk management parameters - adapt this to your needs
MAX_LEVERAGE = 50
# how much of our initial balance do we want to risk per trade in percent
# 1% means if we have a balance of 500$ our margin per trade is 10$ 
RISK_PER_TRADE_PERCENT = 2
# what is our risk reward ratio - this is the ratio of the take profit to the stop loss
# eg. 2 means we want to win 2 times more than we are willing to lose and only need to win 33% of the trades to be profitable
RISK_REWARD_RATIO = 2
# what is our stop loss in percent - this is the max we are willing to lose per trade
# this value refers to the entry price of the underlying asset so for example ETH
# so if the ETH price drops by 0.1% we sell the position and limit our losses
STOP_LOSS_PERCENT = 1.0
# what is our take profit in percent - this is the max we want to win per trade
# so if the ETH price rises by 0.2% we sell the position with profits
TAKE_PROFIT_PERCENT = STOP_LOSS_PERCENT * RISK_REWARD_RATIO
# the length of the queue is the period of the donchian channel
# 120 means we look at the last 120 prices, one request every 30 seconds = 1h timeframe
DON_MAX_PERIOD = 12
REQUEST_INTERVAL_SECONDS = 30

def get_market_info(client, asset):
    """Get market info including decimals for the asset"""
    info = Info(client.base_url, skip_ws=True)
    market_info = info.meta()
    asset_info = next((m for m in market_info['universe'] if m['name'] == asset), None)
    if not asset_info:
        raise ValueError(f"Asset {asset} not found")
    return asset_info

def calculate_position_size(balance, current_price):
    try:
        # Calculate risk amount
        risk_amount = balance * (RISK_PER_TRADE_PERCENT/100)
        print(f"\nDebug Position Sizing:")
        print(f"Balance: ${balance:.2f}")
        print(f"Risk per trade: {RISK_PER_TRADE_PERCENT}%")
        print(f"Risk amount: ${risk_amount:.2f}")
        print(f"Current price: ${current_price:.2f}")
        print(f"Stop loss percent: {STOP_LOSS_PERCENT}%")
        
        # Check for zero values
        if STOP_LOSS_PERCENT == 0:
            raise ValueError("STOP_LOSS_PERCENT cannot be 0")
            
        if current_price == 0:
            raise ValueError("Current price cannot be 0")
        
        # Calculate position size
        position_size = risk_amount / (current_price * STOP_LOSS_PERCENT/100)
        notional_size = position_size * current_price
        
        print(f"Calculated position size: {position_size:.5f} BTC")
        print(f"Notional value: ${notional_size:.2f}")
        
        # Handle minimum position size
        min_position_size = 0.001
        if position_size < min_position_size:
            position_size = min_position_size
            print(f"Adjusted to minimum position: {position_size:.5f} BTC")
            
        return round(position_size, 5)
        
    except Exception as e:
        print(f"Error in position sizing calculation: {str(e)}")
        print(f"Variables: balance={balance}, current_price={current_price}, "
              f"RISK_PER_TRADE_PERCENT={RISK_PER_TRADE_PERCENT}, "
              f"STOP_LOSS_PERCENT={STOP_LOSS_PERCENT}")
        return min_position_size  # Return minimum position size as fallback

def buy(client, asset, position_size):
    try:
        # Get market info for proper decimal handling
        market_info = get_market_info(client, asset)
        sz_decimals = market_info['szDecimals']
        
        # Validate inputs
        if position_size <= 0:
            raise ValueError("Position size must be positive")
        
        # Round position size to proper decimals
        position_size = round(position_size, sz_decimals)
        
        print(f"\nPlacing market buy order:")
        print(f"Asset: {asset}")
        print(f"Position size: {position_size}")
        
        # Place market buy order
        order = client.market_open(
            name=asset,
            is_buy=True,
            sz=position_size
        )
        if order['status'] != 'ok':
            raise ValueError(f"Market order failed: {order}")
            
        # Validate order response
        if 'response' not in order or 'data' not in order['response'] or 'statuses' not in order['response']['data']:
            raise ValueError(f"Unexpected order response format: {order}")
            
        # Get the entry price from the order and round to integer for BTC
        entry_price = int(float(order['response']['data']['statuses'][0]['filled']['avgPx']))
        print(f"Entry price: {entry_price}")

        # Calculate stop loss and take profit prices
        stop_loss_price = int(entry_price * (1 - STOP_LOSS_PERCENT / 100))
        take_profit_price = int(entry_price * (1 + TAKE_PROFIT_PERCENT / 100))
        
        print(f"\nPlacing stop loss order at {stop_loss_price} ({STOP_LOSS_PERCENT}% below entry)")
        stop_loss_order = place_stop_loss(client, asset, position_size, stop_loss_price, is_buy=False)
        
        print(f"\nPlacing take profit order at {take_profit_price} ({TAKE_PROFIT_PERCENT}% above entry)")
        take_profit_order = place_take_profit(client, asset, position_size, take_profit_price, is_buy=False)
        
        return True
        
    except Exception as e:
        print(f"Error in buy function: {str(e)}")
        return False

def place_stop_loss(client, asset, position_size, stop_price, is_buy):
    """Helper function to place stop loss orders"""
    order = client.order(
        asset,
        is_buy=is_buy,
        sz=position_size,
        limit_px=stop_price,
        reduce_only=True,
        order_type={"trigger": {
            "triggerPx": stop_price,
            "isMarket": True,
            "tpsl": "sl"
        }}
    )
    
    if order["status"] != "ok":
        raise ValueError(f"Failed to place stop loss order: {order}")
        
    status = order["response"]["data"]["statuses"][0]
    if "error" in status:
        raise ValueError(f"Stop loss order error: {status['error']}")
    
    print("Stop loss order placed successfully")
    return order

def place_take_profit(client, asset, position_size, take_profit_price, is_buy):
    """Helper function to place take profit orders"""
    order = client.order(
        asset,
        is_buy=is_buy,
        sz=position_size,
        limit_px=take_profit_price,
        reduce_only=True,
        order_type={"trigger": {
            "triggerPx": take_profit_price,
            "isMarket": True,
            "tpsl": "tp"
        }}
    )
    
    if order["status"] != "ok":
        raise ValueError(f"Failed to place take profit order: {order}")
        
    status = order["response"]["data"]["statuses"][0]
    if "error" in status:
        raise ValueError(f"Take profit order error: {status['error']}")
    
    print("Take profit order placed successfully")
    return order

def sell(client, asset, position_size):
    try:
        # Get market info for proper decimal handling
        market_info = get_market_info(client, asset)
        sz_decimals = market_info['szDecimals']
        
        # Validate inputs
        if position_size <= 0:
            raise ValueError("Position size must be positive")
        
        # Round position size to proper decimals
        position_size = round(position_size, sz_decimals)
        
        print(f"\nPlacing market sell order:")
        print(f"Asset: {asset}")
        print(f"Position size: {position_size}")
        
        # Place market sell order
        order = client.market_open(
            name=asset,
            is_buy=False,
            sz=position_size
        )
        if order['status'] != 'ok':
            raise ValueError(f"Market order failed: {order}")
            
        # Validate order response
        if 'response' not in order or 'data' not in order['response'] or 'statuses' not in order['response']['data']:
            raise ValueError(f"Unexpected order response format: {order}")
        
        # Get the entry price from the order and round to integer for BTC
        entry_price = int(float(order['response']['data']['statuses'][0]['filled']['avgPx']))
        print(f"Entry price: {entry_price}")

        # Place stop loss order (0.1% above entry)
        stop_loss_price = int(entry_price * (1 + STOP_LOSS_PERCENT / 100))
        print(f"\nPlacing stop loss order at {stop_loss_price} ({STOP_LOSS_PERCENT}% above entry)")
        stop_loss_order = place_stop_loss(client, asset, position_size, stop_loss_price, is_buy=True)
        
        # Place take profit order (0.2% below entry)
        take_profit_price = int(entry_price * (1 - TAKE_PROFIT_PERCENT / 100))
        print(f"\nPlacing take profit order at {take_profit_price} ({TAKE_PROFIT_PERCENT}% below entry)")
        take_profit_order = place_take_profit(client, asset, position_size, take_profit_price, is_buy=True)
        
        return True
        
    except Exception as e:
        print(f"Error in sell function: {str(e)}")
        return False

def get_current_position(client, asset):
    """Check if we already have a position in this asset"""
    info = Info(client.base_url, skip_ws=True)
    user_state = info.user_state(client.wallet.address)
    positions = user_state.get('assetPositions', [])
    return next((p for p in positions if p['position']['coin'] == asset), None)


def run_trading_strategy(client, asset, margin_per_position, last_prices, public_address):
    # Get positions using Info class
    info = Info(client.base_url, skip_ws=True)
    user_state = info.user_state(public_address)
    balance = float(user_state['marginSummary']['accountValue'])
    positions = user_state.get('assetPositions', [])
    position = next((p for p in positions if p['position']['coin'] == asset), None)
    
    # Get current market price using all_mids
    response = info.all_mids()
    current_price = float(response[asset])
    
    if position and float(position['position']['szi']) != 0:  # szi is the position size
        # We have an open position
        print("\nOpen position:")
        print(f"Asset: {position['position']['coin']}")
        print(f"Size: {position['position']['szi']}")
        print(f"Entry: {position['position']['entryPx']}")
        print(f"Current market price: {current_price}")
        
        # Calculate PnL manually
        entry_price = float(position['position']['entryPx'])
        position_size = float(position['position']['szi'])
        unrealized_pnl = position_size * (current_price - entry_price)
        pnl_percent = (unrealized_pnl / balance) * 100
        print(f"Estimated PnL: ${unrealized_pnl:.2f} ({pnl_percent:.2f}%)")
        
        if position['position'].get('liquidationPx'):
            print(f"Liquidation: {position['position']['liquidationPx']}")
        time.sleep(REQUEST_INTERVAL_SECONDS)
        return

    # Our strategy only works when we have enough prices in our queue
    if len(last_prices) < DON_MAX_PERIOD:
        print(f"\nCollecting prices ({len(last_prices) + 1}/{DON_MAX_PERIOD}):")
        print(f"Time: {time.time():.0f}")
        print(f"Price: {current_price}")
        print(f"Prices collected: {list(last_prices)}")
    else:
        min_price = min(last_prices)
        max_price = max(last_prices)
        print(f"\nAnalyzing prices:")
        print(f"Time: {time.time():.0f}")
        print(f"Current: {current_price}")
        print(f"Min: {min_price}")
        print(f"Max: {max_price}")
        print(f"Range: {max_price - min_price}")
        print(f"All prices: {list(last_prices)}")
        
        position_size = calculate_position_size(balance, current_price)
        if position_size is None:
            print("Skipping trade due to risk/leverage limits")
            return

        if current_price > max_price:
            print(f"\nSignal: LONG")
            print(f"Price {current_price} > Max {max_price}")
            buy(client, asset, position_size)
            last_prices.clear()
            print("Price history cleared")
        elif current_price < min_price:
            print(f"\nSignal: SHORT")
            print(f"Price {current_price} < Min {min_price}")
            sell(client, asset, position_size)
            last_prices.clear()
            print("Price history cleared")
    
    last_prices.append(current_price)

def signal_handler(signum, frame):
    print("\nStopping bot gracefully...")
    # Could add cleanup code here if needed
    exit(0)

if __name__ == "__main__":
    try:        
        signal.signal(signal.SIGINT, signal_handler)
        print("Press Ctrl+C to stop the bot")
        
        # Validate environment variables
        private_key = os.getenv('HYPERLIQUID_PRIVATE_KEY')
        public_address = os.getenv('HYPERLIQUID_PUBLIC_ADDRESS')
        if not private_key or not public_address:
            raise ValueError("Missing required environment variables")
            
        env = os.getenv('HYPERLIQUID_ENV', 'mainnet')
        api_url = constants.MAINNET_API_URL if env == 'mainnet' else constants.TESTNET_API_URL
        
        print(f"\nInitializing bot:")
        print(f"Environment: {env}")
        print(f"Public address: {public_address}")
        
        # Initialize client
        account = eth_account.Account.from_key(private_key)
        client = Exchange(account, api_url)
        info = Info(api_url, skip_ws=True)
        
        # Initial balance check
        user_state = info.user_state(public_address)
        balance = float(user_state['marginSummary']['accountValue'])
        print(f"\nInitial balance: ${balance:.2f}")
        
        # Trading parameters
        asset = "BTC"
        margin_per_position = balance * RISK_PER_TRADE_PERCENT / 100
        print(f"Risk per trade: ${margin_per_position:.2f}")
        
        # Initialize price queue
        last_prices = collections.deque(maxlen=DON_MAX_PERIOD)
        
        print("\nStarting trading loop...")
        while True:
            try:
                run_trading_strategy(client, asset, margin_per_position, last_prices, public_address)
            except Exception as e:
                print(f"Error in trading loop: {str(e)}")
                print("Continuing after error...")
            time.sleep(REQUEST_INTERVAL_SECONDS)
            
    except Exception as e:
        print(f"Fatal error: {str(e)}")
        exit(1)