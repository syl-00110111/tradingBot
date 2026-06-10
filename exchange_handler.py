# Binance Trading Bot - Exchange Interface
# Copyleft © 2026 Jules, Ecosia, Sylvain, the World-Wide-Web and you
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import ccxt
import time
import logging

class ExchangeInterface:
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        raise NotImplementedError
    def create_order(self, symbol, side, amount, price=None):
        raise NotImplementedError
    def fetch_balance(self):
        raise NotImplementedError
    def fetch_ticker(self, symbol):
        raise NotImplementedError
    def fetch_trading_fee(self, symbol):
        raise NotImplementedError

class BinanceExchange(ExchangeInterface):
    def __init__(self, api_key, api_secret):
        self.exchange = ccxt.binance({
            'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True,
        })

    def load_markets(self):
        try: return self.exchange.load_markets()
        except Exception as e: logging.error(f"Failed to load markets: {e}"); return {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        try: return self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e: logging.error(f"Error fetching OHLCV for {symbol}: {e}"); return None

    def fetch_ticker(self, symbol):
        try: return self.exchange.fetch_ticker(symbol)
        except Exception as e: logging.error(f"Error fetching ticker for {symbol}: {e}"); return None

    def fetch_balance(self):
        try: return self.exchange.fetch_balance()
        except Exception as e: logging.error(f"Error fetching balance: {e}"); return None

    def fetch_my_trades(self, symbol, limit=10):
        try: return self.exchange.fetch_my_trades(symbol, limit=limit)
        except Exception as e: logging.error(f"Error fetching trades for {symbol}: {e}"); return []

    def fetch_trading_fee(self, symbol):
        try:
            fees = self.exchange.fetch_trading_fee(symbol)
            return fees.get('taker', 0.001)
        except Exception as e:
            logging.warning(f"Error fetching trading fee for {symbol}: {e}. Falling back to 0.1%")
            return 0.001

    def create_order(self, symbol, side, amount, price=None):
        try:
            if not self.exchange.markets: self.exchange.load_markets()

            # Ensure precision is handled
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            amount = float(amount_str)

            if side == 'sell':
                base, _ = symbol.split('/')
                balance = self.fetch_balance()
                # Use a small buffer or just use the precision-adjusted amount
                free_balance = balance.get(base, {}).get('free', 0)
                if free_balance < amount:
                    # If we are very close (due to precision), use the free balance instead
                    if free_balance > 0 and (amount - free_balance) / amount < 0.01:
                        amount = float(self.exchange.amount_to_precision(symbol, free_balance))
                    else:
                        logging.warning(f"Aborting sell of {symbol}: Insufficient {base} balance ({free_balance} < {amount})")
                        return None

            if side == 'buy':
                order = self.exchange.create_market_buy_order(symbol, amount)
            else:
                order = self.exchange.create_market_sell_order(symbol, amount)

            if order and 'fee' in order and order['fee']:
                order['calculated_fee'] = order['fee'].get('cost', 0)
            else:
                 ticker = self.fetch_ticker(symbol)
                 fee_rate = self.fetch_trading_fee(symbol)
                 order['calculated_fee'] = amount * ticker['last'] * fee_rate
            return order
        except Exception as e: logging.error(f"Error during {side} order on {symbol}: {e}"); return None

class MockExchange(ExchangeInterface):
    def __init__(self, api_key=None, api_secret=None):
        self.balance = {'EUR': 1000.0}; self.ohlcv_data = {}; self.real_exchange = None; self.fee_rate = 0.001
        self.markets = {}
        if api_key and api_secret and api_key != "YOUR_API_KEY":
            try:
                self.real_exchange = ccxt.binance({'apiKey': api_key, 'secret': api_secret, 'enableRateLimit': True})
                logging.info("Mock initialized with real API balance fetching (markets deferred)")
            except Exception as e: logging.error(f"Failed to initialize real exchange for Mock: {e}")

    def load_markets(self):
        if self.real_exchange:
            try:
                self.markets = self.real_exchange.load_markets()
                return self.markets
            except Exception as e: logging.error(f"Mock failed to load markets: {e}")
        return {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        if self.real_exchange:
             try: return self.real_exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
             except Exception: pass

        # Fallback to public fetch if no mock data
        if symbol not in self.ohlcv_data:
            try:
                public_ex = ccxt.binance()
                return public_ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            except Exception:
                return []

        return self.ohlcv_data.get(symbol, [])[:limit]

    def fetch_ticker(self, symbol):
        if self.real_exchange:
             try: return self.real_exchange.fetch_ticker(symbol)
             except Exception: pass
        # Fallback for mock if no real exchange or call fails
        data = self.ohlcv_data.get(symbol, [])
        if data:
            return {'last': data[-1][4]}

        # If no internal mock data, try a public CCXT call (no API keys needed)
        try:
            import ccxt
            public_ex = ccxt.binance()
            return public_ex.fetch_ticker(symbol)
        except Exception:
            return {'last': 0.0}

    def fetch_balance(self):
        if self.real_exchange:
            try: return self.real_exchange.fetch_balance()
            except Exception: pass
        return {'total': self.balance, 'free': self.balance}

    def fetch_my_trades(self, symbol, limit=10):
        if self.real_exchange:
            try: return self.real_exchange.fetch_my_trades(symbol, limit=limit)
            except Exception: pass
        return []

    def fetch_trading_fee(self, symbol):
        if self.real_exchange:
            try:
                fees = self.real_exchange.fetch_trading_fee(symbol)
                return fees.get('taker', 0.001)
            except Exception: pass
        return self.fee_rate

    def create_order(self, symbol, side, amount, price=None):
        ticker = self.fetch_ticker(symbol); price = ticker['last']; cost = amount * price;
        fee_rate = self.fetch_trading_fee(symbol)
        fee = cost * fee_rate
        base, quote = symbol.split('/'); current_balance = self.fetch_balance()
        if 'free' in current_balance:
             free_quote = current_balance['free'].get(quote, self.balance.get(quote, 0))
             free_base = current_balance['free'].get(base, self.balance.get(base, 0))
        else:
             free_quote = self.balance.get(quote, 0); free_base = self.balance.get(base, 0)
        if side == 'buy':
            if free_quote >= (cost + fee):
                self.balance[quote] = free_quote - (cost + fee); self.balance[base] = free_base + amount
                return {'id': 'mock_buy_' + str(time.time()), 'status': 'closed', 'price': price, 'amount': amount, 'calculated_fee': fee}
        else:
            # For sell, we allow it if real API keys are present even if real balance is 0
            # because simulation should be isolated from real wallet
            if self.real_exchange or free_base >= amount:
                self.balance[base] = free_base - amount; self.balance[quote] = free_quote + cost - fee
                return {'id': 'mock_sell_' + str(time.time()), 'status': 'closed', 'price': price, 'amount': amount, 'calculated_fee': fee}
        return None
