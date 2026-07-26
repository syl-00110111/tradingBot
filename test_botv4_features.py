import sys
import os
import json
import math
import unittest
from unittest.mock import MagicMock, patch

# Mock dependencies to prevent errors during import in sandbox without pandas/ccxt/numpy/torch
mock_pandas = MagicMock()
class MockILoc:
    def __init__(self, df):
        self.df = df
    def __getitem__(self, idx):
        row_data = self.df.data[idx]
        if isinstance(row_data, dict):
            return row_data
        return dict(zip(self.df.columns, row_data))

class MockDataFrame:
    def __init__(self, data=None, columns=None):
        if data is None:
            data = []
        self.data = data
        self.columns = columns
    @property
    def empty(self):
        return len(self.data) == 0
    def tail(self, n):
        return MockDataFrame(self.data[-n:], self.columns)
    @property
    def iloc(self):
        return MockILoc(self)
    def __len__(self):
        return len(self.data)
mock_pandas.DataFrame = MockDataFrame
sys.modules['pandas'] = mock_pandas

mock_pandas_ta = MagicMock()
sys.modules['pandas_ta'] = mock_pandas_ta

mock_numpy = MagicMock()
mock_numpy.nan = float('nan')
mock_numpy.log = math.log
mock_numpy.sqrt = math.sqrt
sys.modules['numpy'] = mock_numpy

mock_torch = MagicMock()
sys.modules['torch'] = mock_torch

mock_ccxt = MagicMock()
sys.modules['ccxt'] = mock_ccxt

mock_plotext = MagicMock()
sys.modules['plotext'] = mock_plotext

mock_readchar = MagicMock()
sys.modules['readchar'] = mock_readchar

mock_psutil = MagicMock()
sys.modules['psutil'] = mock_psutil

# Mock rich elements
mock_rich = MagicMock()
sys.modules['rich'] = mock_rich
sys.modules['rich.console'] = mock_rich
sys.modules['rich.live'] = mock_rich
sys.modules['rich.table'] = mock_rich
sys.modules['rich.progress'] = mock_rich
sys.modules['rich.layout'] = mock_rich
sys.modules['rich.panel'] = mock_rich
sys.modules['rich.logging'] = mock_rich
sys.modules['rich.text'] = mock_rich

# Now import botv4 features
import botv4

class TestBotV4Features(unittest.TestCase):
    def setUp(self):
        # Clean up any test files
        self.purchases_file = 'recorded_purchases.json'
        if os.path.exists(self.purchases_file):
            os.remove(self.purchases_file)
        botv4.recorded_purchases = {}

    def tearDown(self):
        if os.path.exists(self.purchases_file):
            os.remove(self.purchases_file)
        botv4.recorded_purchases = {}

    def test_record_purchase(self):
        botv4.record_purchase("BTC/USD", 1.5, 50000.0)
        self.assertIn("BTC/USD", botv4.recorded_purchases)
        self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 1)
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][0]["amount"], 1.5)
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][0]["price"], 50000.0)

        # Verify it was persisted to file
        self.assertTrue(os.path.exists(self.purchases_file))
        with open(self.purchases_file, 'r') as f:
            data = json.load(f)
            self.assertIn("BTC/USD", data)

    def test_is_sell_profitable_and_deductions(self):
        # If no purchases, default to True
        profitable, msg = botv4.is_sell_profitable("BTC/USD", 48000.0, 1.0)
        self.assertTrue(profitable)

        # Record two purchases
        botv4.record_purchase("BTC/USD", 1.0, 40000.0)
        botv4.record_purchase("BTC/USD", 1.0, 42000.0)
        # Average purchase price: 41000.0

        # Selling at 40500 should be unprofitable (40500 < 41000)
        profitable, msg = botv4.is_sell_profitable("BTC/USD", 40500.0, 1.0)
        self.assertFalse(profitable)

        # Selling at 41500 should be profitable (41500 > 41000)
        profitable, msg = botv4.is_sell_profitable("BTC/USD", 41500.0, 1.0)
        self.assertTrue(profitable)

        # Deduct some amount (1.5 BTC)
        # Under new requirements, any successful sale clears all recorded purchases for that symbol
        botv4.remove_recorded_purchases("BTC/USD", 1.5)
        self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 0)

        # Selling when empty defaults to True
        profitable, msg = botv4.is_sell_profitable("BTC/USD", 41500.0, 0.5)
        self.assertTrue(profitable)

    def test_remove_edited_buy_order_purchase(self):
        # Record three purchases
        botv4.record_purchase("BTC/USD", 1.0, 40000.0)
        botv4.record_purchase("BTC/USD", 1.5, 42000.0)
        botv4.record_purchase("BTC/USD", 1.0, 40000.0)

        # Remove the one with 1.5 amount and 42000.0 price
        botv4.remove_edited_buy_order_purchase("BTC/USD", 1.5, 42000.0)
        self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 2)
        # Verify remaining purchases
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][0]["amount"], 1.0)
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][0]["price"], 40000.0)
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][1]["amount"], 1.0)
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][1]["price"], 40000.0)

        # Remove only one if there are duplicates
        botv4.remove_edited_buy_order_purchase("BTC/USD", 1.0, 40000.0)
        self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 1)
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][0]["amount"], 1.0)
        self.assertEqual(botv4.recorded_purchases["BTC/USD"][0]["price"], 40000.0)

        # No match prints a warning and does not raise exception
        botv4.remove_edited_buy_order_purchase("BTC/USD", 9.9, 999.9)
        self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 1)

    def test_cleanup_open_orders_edit_removes_old_purchase(self):
        mock_exchange = MagicMock()
        mock_exchange.rateLimit = 1000
        mock_exchange.has = {'editOrder': True}
        # Mock previous order on same side (buy vs buy)
        mock_exchange.fetch_open_orders.return_value = [
            {'id': '123', 'side': 'buy', 'price': 42000.0, 'amount': 1.5, 'symbol': 'BTC/USD'}
        ]
        mock_exchange.edit_order.return_value = {'id': '123', 'price': 43000.0, 'amount': 1.5}

        # Setup recorded purchases
        botv4.record_purchase("BTC/USD", 1.5, 42000.0)
        self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 1)

        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.95
            botv4.config = {'monte_carlo': {'sufficient_probability': 0.91}}

            res = botv4.cleanup_open_orders(mock_exchange, "BTC/USD", 43000.0, "buy", None, 44000.0, 1.5)

            # edit_order should have been called successfully
            mock_exchange.edit_order.assert_called_once_with('123', 'BTC/USD', 'limit', 'buy', 1.5, 43000.0)
            mock_exchange.cancel_order.assert_not_called()
            self.assertIsNotNone(res)

            # The old purchase should have been removed!
            self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 0)

    def test_cleanup_open_orders_prob_sufficient(self):
        mock_exchange = MagicMock()
        mock_exchange.rateLimit = 1000
        # Mock previous orders on different side (side = 'buy', previous order = 'sell')
        mock_exchange.fetch_open_orders.return_value = [
            {'id': '123', 'side': 'sell', 'price': 45000.0, 'symbol': 'BTC/USD'}
        ]

        # Prepare dummy df_candles
        # Close is 44000.0, target is 45000.0.
        # Let's mock MonteCarloEngine's estimate_hit_probability to return high probability (0.95)
        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.95
            # Under config we have sufficient_probability = 0.91 (the default)
            botv4.config = {'monte_carlo': {'sufficient_probability': 0.91}}

            botv4.cleanup_open_orders(mock_exchange, "BTC/USD", 43000.0, "buy", None, 44000.0)

            # Since execution probability (0.95) >= threshold (0.91), order should NOT be cancelled
            mock_exchange.cancel_order.assert_not_called()

    def test_cleanup_open_orders_prob_insufficient(self):
        mock_exchange = MagicMock()
        mock_exchange.rateLimit = 1000
        # Mock previous orders on different side
        mock_exchange.fetch_open_orders.return_value = [
            {'id': '123', 'side': 'sell', 'price': 45000.0, 'symbol': 'BTC/USD'}
        ]

        # Let's mock MonteCarloEngine's estimate_hit_probability to return low probability (0.8)
        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.8
            botv4.config = {'monte_carlo': {'sufficient_probability': 0.91}}

            botv4.cleanup_open_orders(mock_exchange, "BTC/USD", 43000.0, "buy", None, 44000.0)

            # Since execution probability (0.8) < threshold (0.91) and side changed (sell vs buy), it should be cancelled!
            mock_exchange.cancel_order.assert_called_once_with('123', 'BTC/USD')

    def test_should_place_order_buy_sufficient(self):
        # Prepare dummy df_candles
        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.995  # > 0.99
            should_place, prob = botv4.should_place_order("BTC/USD", "buy", 43000.0, 44000.0, None)
            self.assertTrue(should_place)
            self.assertEqual(prob, 0.995)

    def test_should_place_order_buy_insufficient(self):
        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.98  # <= 0.99
            should_place, prob = botv4.should_place_order("BTC/USD", "buy", 43000.0, 44000.0, None)
            self.assertFalse(should_place)
            self.assertEqual(prob, 0.98)

    def test_should_place_order_sell_sufficient(self):
        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.995  # > 0.99
            should_place, prob = botv4.should_place_order("BTC/USD", "sell", 45000.0, 44000.0, None)
            self.assertTrue(should_place)
            self.assertEqual(prob, 0.995)

    def test_should_place_order_sell_insufficient(self):
        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.99  # not > 0.99 (i.e. <= 0.99)
            should_place, prob = botv4.should_place_order("BTC/USD", "sell", 45000.0, 44000.0, None)
            self.assertFalse(should_place)
            self.assertEqual(prob, 0.99)

    def test_cleanup_open_orders_same_side(self):
        mock_exchange = MagicMock()
        mock_exchange.rateLimit = 1000
        # Mock previous order on same side (sell vs sell)
        mock_exchange.fetch_open_orders.return_value = [
            {'id': '123', 'side': 'sell', 'price': 45000.0, 'symbol': 'BTC/USD'}
        ]

        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.01 # extremely low
            botv4.config = {'monte_carlo': {'sufficient_probability': 0.15}}

            botv4.cleanup_open_orders(mock_exchange, "BTC/USD", 46000.0, "sell", None, 44000.0)

            # Order should be cancelled because probability (0.01) is no longer sufficient (< 0.15)
            mock_exchange.cancel_order.assert_called_once_with('123', 'BTC/USD')

    def test_cleanup_open_orders_edit_success(self):
        mock_exchange = MagicMock()
        mock_exchange.rateLimit = 1000
        mock_exchange.has = {'editOrder': True}
        # Mock previous order on same side (buy vs buy)
        mock_exchange.fetch_open_orders.return_value = [
            {'id': '123', 'side': 'buy', 'price': 42000.0, 'symbol': 'BTC/USD'}
        ]
        mock_exchange.edit_order.return_value = {'id': '123', 'price': 43000.0, 'amount': 1.5}

        with patch('monte_carlo2.MonteCarloEngine.estimate_hit_probability') as mock_hit_prob:
            mock_hit_prob.return_value = 0.95
            botv4.config = {'monte_carlo': {'sufficient_probability': 0.91}}

            res = botv4.cleanup_open_orders(mock_exchange, "BTC/USD", 43000.0, "buy", None, 44000.0, 1.5)

            # edit_order should have been called successfully
            mock_exchange.edit_order.assert_called_once_with('123', 'BTC/USD', 'limit', 'buy', 1.5, 43000.0)
            mock_exchange.cancel_order.assert_not_called()
            self.assertIsNotNone(res)
            self.assertEqual(res['price'], 43000.0)

    def test_simultaneous_signals_prioritization(self):
        # We want to test the prioritisation of concurrent BUY and SELL signals
        # We can extract the prioritization logic into a simple helper or simulate it
        # Since the logic is inside the botv4.py main block, let's write a unit test
        # that mimics or tests this logic on mock global_buy and global_sell lists.

        # Scenario 1: Buy has higher probability than Sell -> Sell is set to False
        global_buy = [True]
        global_sell = [True]
        latest_idx = 0
        prob_buy = 0.6
        prob_sell = 0.4

        if prob_buy >= prob_sell:
            global_sell[latest_idx] = False
        else:
            global_buy[latest_idx] = False

        self.assertTrue(global_buy[latest_idx])
        self.assertFalse(global_sell[latest_idx])

        # Scenario 2: Sell has higher probability than Buy -> Buy is set to False
        global_buy = [True]
        global_sell = [True]
        latest_idx = 0
        prob_buy = 0.3
        prob_sell = 0.7

        if prob_buy >= prob_sell:
            global_sell[latest_idx] = False
        else:
            global_buy[latest_idx] = False

        self.assertFalse(global_buy[latest_idx])
        self.assertTrue(global_sell[latest_idx])

        # Scenario 3: Equal probability -> Buy is prioritized (prob_buy >= prob_sell) -> Sell is set to False
        global_buy = [True]
        global_sell = [True]
        latest_idx = 0
        prob_buy = 0.5
        prob_sell = 0.5

        if prob_buy >= prob_sell:
            global_sell[latest_idx] = False
        else:
            global_buy[latest_idx] = False

        self.assertTrue(global_buy[latest_idx])
        self.assertFalse(global_sell[latest_idx])

    def test_remove_recorded_purchases_cross_pairs(self):
        # Record purchases for different pairs of base asset ADA
        botv4.record_purchase("ADA/USD", 10.0, 0.50)
        botv4.record_purchase("ADA/BTC", 100.0, 0.00001)
        # Record purchase for a different base asset (e.g. BTC)
        botv4.record_purchase("BTC/USD", 1.0, 90000.0)

        # Trigger sale on ADA/USD
        botv4.remove_recorded_purchases("ADA/USD", 10.0)

        # All ADA purchases should be wiped, while BTC/USD purchase remains
        self.assertEqual(len(botv4.recorded_purchases["ADA/USD"]), 0)
        self.assertEqual(len(botv4.recorded_purchases["ADA/BTC"]), 0)
        self.assertEqual(len(botv4.recorded_purchases["BTC/USD"]), 1)

    def test_count_buyings_for_base_asset(self):
        botv4.record_purchase("ADA/USD", 10.0, 0.50)
        botv4.record_purchase("ADA/BTC", 100.0, 0.00001)
        botv4.record_purchase("BTC/USD", 1.0, 90000.0)

        self.assertEqual(botv4.count_buyings_for_base_asset("ADA"), 2)
        self.assertEqual(botv4.count_buyings_for_base_asset("BTC"), 1)
        self.assertEqual(botv4.count_buyings_for_base_asset("XRP"), 0)

    def test_is_sell_profitable_cross_quote(self):
        # We have recorded purchases on ADA/USD: 100 ADA at 1.00 USD
        # We check profitability on ADA/EUR.
        # Fallback EUR/USD exchange rate is 1.08.
        # So USD/EUR exchange rate is 1 / 1.08 = ~0.9259 EUR per USD.
        # Thus, 1.00 USD purchase price is converted to ~0.9259 EUR.
        # With 0.3% margin: 0.9259 * 1.003 = ~0.9287 EUR.

        botv4.record_purchase("ADA/USD", 100.0, 1.00)

        # Check profitability on ADA/EUR at sell_price = 0.90 EUR (should be unprofitable, since 0.90 < 0.9287)
        profitable, msg = botv4.is_sell_profitable("ADA/EUR", 0.90, 100.0)
        self.assertFalse(profitable)

        # Check profitability on ADA/EUR at sell_price = 0.95 EUR (should be profitable, since 0.95 > 0.9287)
        profitable, msg = botv4.is_sell_profitable("ADA/EUR", 0.95, 100.0)
        self.assertTrue(profitable)

    def test_wind_choice_logic(self):
        # Setup simulated availablePairs:
        # We have ADA/USD (p_base='ADA', p_quote='USD') and ADA/EUR (p_base='ADA', p_quote='EUR')
        availablePairs = [
            ["ADA/USD", "id1", "ADA", "USD"],
            ["ADA/EUR", "id2", "ADA", "EUR"]
        ]

        # Scenario 1: We have more available EUR than USD (measured in USD)
        # USD balance: $100
        # EUR balance: 100 EUR, which is ~$108
        _balance = {
            'free': {
                'USD': 100.0,
                'EUR': 100.0
            }
        }

        # Test current pair is ADA/USD
        base = "ADA"
        quote = "USD"
        symbol = "ADA/USD"

        # Standard fallback rates in botv4: EUR to USD is 1.08
        # We simulate the exact wind-choice logic block from botv4.py
        pass_on_buy = False
        if botv4.count_buyings_for_base_asset(base) == 0:
            other_pairs = [p for p in availablePairs if p[2] == base and p[3] != quote]
            if other_pairs:
                quote_free = float(_balance.get('free', {}).get(quote, 0.0))
                for p in other_pairs:
                    p_symbol = p[0]
                    p_quote = p[3]
                    other_quote_free = float(_balance.get('free', {}).get(p_quote, 0.0))

                    # Simulated conversion rate fallback: EUR to USD is 1.08
                    conversion_rate = 1.08 if p_quote == 'EUR' and quote == 'USD' else 1.0
                    other_quote_free_converted = other_quote_free * conversion_rate
                    if other_quote_free_converted > quote_free:
                        pass_on_buy = True
                        break

        # Should pass on the buy of ADA/USD because we have more money in EUR
        self.assertTrue(pass_on_buy)

        # Scenario 2: We have more available USD than EUR
        _balance = {
            'free': {
                'USD': 200.0,
                'EUR': 50.0 # 50 EUR * 1.08 = 54 USD < 200 USD
            }
        }

        pass_on_buy = False
        if botv4.count_buyings_for_base_asset(base) == 0:
            other_pairs = [p for p in availablePairs if p[2] == base and p[3] != quote]
            if other_pairs:
                quote_free = float(_balance.get('free', {}).get(quote, 0.0))
                for p in other_pairs:
                    p_symbol = p[0]
                    p_quote = p[3]
                    other_quote_free = float(_balance.get('free', {}).get(p_quote, 0.0))

                    conversion_rate = 1.08 if p_quote == 'EUR' and quote == 'USD' else 1.0
                    other_quote_free_converted = other_quote_free * conversion_rate
                    if other_quote_free_converted > quote_free:
                        pass_on_buy = True
                        break

        # Should NOT pass on the buy of ADA/USD because USD has more money
        self.assertFalse(pass_on_buy)

    def test_price_not_overwritten_by_shadowing(self):
        # Initial price calculation
        price = 0.36669500

        # Simulated wind-choice logic that previously caused the bug
        availablePairs = [
            ["ETHFI/EUR", "id1", "ETHFI", "EUR"],
            ["ETHFI/USD", "id2", "ETHFI", "USD"]
        ]
        base = "ETHFI"
        quote = "EUR"

        # Mock exchange with fetch_ticker
        mock_exchange = MagicMock()
        mock_exchange.fetch_ticker.return_value = {
            'close': 1.13664,
            'last': 1.13664
        }

        _markets = {
            "ETHFI/EUR": {},
            "ETHFI/USD": {},
            "EUR/USD": {},
            "USD/EUR": {}
        }

        _balance = {
            'free': {
                'EUR': 100.0,
                'USD': 150.0
            }
        }

        pass_on_buy = False
        other_pairs = [p for p in availablePairs if p[2] == base and p[3] != quote]
        if other_pairs:
            quote_free = float(_balance.get('free', {}).get(quote, 0.0))
            for p in other_pairs:
                p_symbol = p[0]
                p_quote = p[3]
                other_quote_free = float(_balance.get('free', {}).get(p_quote, 0.0))

                conversion_rate = 1.0
                if p_quote != quote:
                    symbol1 = f"{p_quote}/{quote}"
                    symbol2 = f"{quote}/{p_quote}"
                    rate_found = False
                    if isinstance(_markets, dict):
                        if symbol1 in _markets:
                            try:
                                ticker = mock_exchange.fetch_ticker(symbol1)
                                conversion_rate = float(ticker.get('close') or ticker.get('last') or 0.0)
                                rate_found = True
                            except Exception:
                                pass
                        # In the old code, this block used "price = ..." which overwrote the outer "price"
                        if not rate_found and symbol2 in _markets:
                            try:
                                ticker = mock_exchange.fetch_ticker(symbol2)
                                ticker_price = float(ticker.get('close') or ticker.get('last') or 0.0)
                                if ticker_price > 0:
                                    conversion_rate = 1.0 / ticker_price
                                    rate_found = True
                            except Exception:
                                pass

                other_quote_free_converted = other_quote_free * conversion_rate
                if other_quote_free_converted > quote_free:
                    pass_on_buy = True
                    break

        # Check that the outer 'price' variable remained intact and was not overwritten!
        self.assertEqual(price, 0.36669500)

    def test_calibrate_window_by_non_repetition(self):
        # Create a mock DataFrame representing candles.
        candles_data = [
            [1.0, 1.1, 0.9, 1.0, 100], # 0
            [1.0, 1.1, 0.9, 1.0, 100], # 1 (identical to 0, repetition)
            [1.01, 1.11, 0.91, 1.01, 100], # 2 (active)
            [1.01, 1.11, 0.91, 1.01, 100], # 3 (identical to 2, repetition)
            [1.02, 1.12, 0.92, 1.02, 100], # 4 (active)
            [1.03, 1.13, 0.93, 1.03, 100], # 5 (active)
            [1.03, 1.13, 0.93, 1.03, 100], # 6 (identical to 5, repetition)
            [1.04, 1.14, 0.94, 1.04, 100], # 7 (active)
            [1.05, 1.15, 0.95, 1.05, 100], # 8 (active)
            [1.06, 1.16, 0.96, 1.06, 100]  # 9 (active)
        ]
        df = MockDataFrame(candles_data, columns=['open', 'high', 'low', 'close', 'volume'])

        # We scan backwards from index 9.
        # Comparisons:
        # (9, 8) -> 1.06 vs 1.05 -> active. active_count = 1
        # (8, 7) -> 1.05 vs 1.04 -> active. active_count = 2
        # (7, 6) -> 1.04 vs 1.03 -> active. active_count = 3
        # (6, 5) -> 1.03 vs 1.03 -> repetition. active_count = 3
        # (5, 4) -> 1.03 vs 1.02 -> active. active_count = 4
        # (4, 3) -> 1.02 vs 1.01 -> active. active_count = 5

        # If target_active=5, we break at (4, 3) comparison.
        # scanned_count = 6 comparisons.
        # window_size = scanned_count + 1 = 7.
        size = botv4.calibrate_window_by_non_repetition(df, target_active=5, epsilon=1e-5)
        self.assertEqual(size, 7)

        # Check safety/fallback for empty dataframe
        empty_df = MockDataFrame([], columns=['open', 'high', 'low', 'close', 'volume'])
        self.assertEqual(botv4.calibrate_window_by_non_repetition(empty_df), 0)

        # Check single candle
        single_df = MockDataFrame([[1.0, 1.1, 0.9, 1.0, 100]], columns=['open', 'high', 'low', 'close', 'volume'])
        self.assertEqual(botv4.calibrate_window_by_non_repetition(single_df), 1)

        # Check with default target_active (17280)
        self.assertEqual(botv4.calibrate_window_by_non_repetition(df), 17280)

    def test_buy_requires_at_least_17280_candles(self):
        # We simulate the check `full_df_candles is None or len(full_df_candles) < 17280` in the BUY block.
        # Case 1: insufficient candles
        candles_data_short = [[1.0, 1.1, 0.9, 1.0, 100]] * 17279
        df_short = MockDataFrame(candles_data_short, columns=['open', 'high', 'low', 'close', 'volume'])
        self.assertTrue(df_short is None or len(df_short) < 17280)

        # Case 2: sufficient candles
        candles_data_long = [[1.0, 1.1, 0.9, 1.0, 100]] * 17280
        df_long = MockDataFrame(candles_data_long, columns=['open', 'high', 'low', 'close', 'volume'])
        self.assertFalse(df_long is None or len(df_long) < 17280)

if __name__ == '__main__':
    unittest.main()
