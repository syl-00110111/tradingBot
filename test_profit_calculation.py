import unittest
from trading_engine import TradingEngine

class TestProfitCalculation(unittest.TestCase):
    def setUp(self):
        self.config = {}
        self.engine = TradingEngine(self.config)

    def test_is_profitable_true(self):
        # entry = 100, fee = 0.001 (0.1%)
        # min_exit = 100 * (1 + 0.001 * 2) = 100 * 1.002 = 100.2
        # current = 100.3 -> True
        self.assertTrue(self.engine.is_profitable(100.3, 100.0, 0.001))

    def test_is_profitable_false_due_to_fees(self):
        # entry = 100, min_exit = 100.2
        # current = 100.1 -> False
        self.assertFalse(self.engine.is_profitable(100.1, 100.0, 0.001))

    def test_is_profitable_false_loss(self):
        # current = 99 -> False
        self.assertFalse(self.engine.is_profitable(99.0, 100.0, 0.001))

    def test_check_profitability_wrapper(self):
        # Should behave same as is_profitable
        self.assertTrue(self.engine.check_profitability(100.3, 100.0, "BTC/USDT", 0.001))

if __name__ == '__main__':
    unittest.main()
