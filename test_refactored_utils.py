import sys
from unittest.mock import MagicMock

# Dynamically mock pandas for the sandbox environment where pandas is not installed
mock_pandas = MagicMock()
class MockDataFrame:
    def __init__(self, data=None, columns=None):
        self.data = data
        self.columns = columns
    def empty(self):
        return len(self.data) == 0 if self.data else True
mock_pandas.DataFrame = MockDataFrame
sys.modules['pandas'] = mock_pandas

import unittest
from unittest.mock import patch
import os
import json
import shutil

import market_utils
import symbols_utils

class TestRefactoredUtils(unittest.TestCase):
    def setUp(self):
        # Create temp files/directories for tests if needed
        self.test_balance_file = "balance.json"
        self.test_paused_file = "paused_for_buy_test.json"
        self.test_markets_file = "markets_test.json"
        self.test_volumes_file = "volumes_trades_data_test.json"

        # Cleanup any leftover test files
        for f in [self.test_balance_file, self.test_paused_file, self.test_markets_file, self.test_volumes_file]:
            if os.path.exists(f):
                os.remove(f)

    def tearDown(self):
        # Cleanup
        for f in [self.test_balance_file, self.test_paused_file, self.test_markets_file, self.test_volumes_file]:
            if os.path.exists(f):
                os.remove(f)
        # also cleanup temporary ohlcv files
        for f in os.listdir('.'):
            if f.startswith("ohlcv_data_") and f.endswith(".json"):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def test_fetch_balance(self):
        mock_exchange = MagicMock()
        mock_exchange.fetch_balance.return_value = {
            'free': {'BTC': 1.0, 'USD': 100.0},
            'total': {'BTC': 1.0, 'USD': 100.0}
        }

        balance = market_utils.fetch_balance(mock_exchange)
        self.assertIn('timestamp', balance)
        self.assertEqual(balance['free']['BTC'], 1.0)
        self.assertTrue(os.path.exists("balance.json"))

    def test_check_candles_consistency_no_file(self):
        # When no file exists, should return empty list
        res = market_utils.check_candles_consistency("BTC/USD")
        self.assertEqual(res, [])

    def test_check_candles_consistency_valid(self):
        # Valid candles (interval 60000ms)
        candles = [
            [1600000000000, 10.0, 11.0, 9.0, 10.5, 100],
            [1600000060000, 10.5, 11.5, 10.0, 11.0, 120]
        ]
        symbol = "BTC_USD"
        fpath = f"ohlcv_data_{symbol}_1m.json"
        with open(fpath, "w") as f:
            json.dump(candles, f)

        res = market_utils.check_candles_consistency(symbol)
        self.assertEqual(res, [])

    def test_check_candles_consistency_invalid_gap(self):
        # Gap of more than 1.5 * 60000ms (90000ms)
        candles = [
            [1600000000000, 10.0, 11.0, 9.0, 10.5, 100],
            [1600000200000, 10.5, 11.5, 10.0, 11.0, 120] # 200000ms gap
        ]
        symbol = "BTC_USD"
        fpath = f"ohlcv_data_{symbol}_1m.json"
        with open(fpath, "w") as f:
            json.dump(candles, f)

        res = market_utils.check_candles_consistency(symbol)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][2], "gap 200000ms")

    def test_compute_symbols(self):
        # Create mock markets.json and volumes.json
        markets_data = {
            "BTC/USD": {
                "symbol": "BTC/USD",
                "id": "BTCUSD",
                "base": "BTC",
                "quote": "USD",
                "limits": {"amount": {"min": 0.001}},
                "precision": {"price": 0.01, "amount": 0.0001}
            },
            "ETH/USD": {
                "symbol": "ETH/USD",
                "id": "ETHUSD",
                "base": "ETH",
                "quote": "USD",
                "limits": {"amount": {"min": 0.01}},
                "precision": {"price": 0.01, "amount": 0.001}
            }
        }
        volumes_data = [
            {"symbol": "BTC/USD", "id": "BTCUSD", "trades_count": 1000, "timestamp": 1600000000},
            {"symbol": "ETH/USD", "id": "ETHUSD", "trades_count": 800, "timestamp": 1600000000}
        ]

        with open(self.test_markets_file, "w") as f:
            json.dump(markets_data, f)
        with open(self.test_volumes_file, "w") as f:
            json.dump(volumes_data, f)

        balance = {
            'free': {'BTC': 0.1, 'USD': 100.0}
        }

        symbols = symbols_utils.computeSymbols(
            balance=balance,
            previousPairs=None,
            source_assets=[],
            forbid_assets=['USDT'],
            base_assets=['USD'],
            max_num_pairs=5,
            mini_count=500,
            markets_file=self.test_markets_file,
            volumes_file=self.test_volumes_file
        )

        # Verify both BTC/USD and ETH/USD are in computeSymbols
        self.assertEqual(len(symbols), 2)
        symbols_names = [s[0] for s in symbols]
        self.assertIn("BTC/USD", symbols_names)
        self.assertIn("ETH/USD", symbols_names)


if __name__ == "__main__":
    unittest.main()
