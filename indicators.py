# Binance Trading Bot - Technical Strategies
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

import pandas as pd
import pandas_ta as ta
import numpy as np
import torch
from monte_carlo import MonteCarloEngine

@torch.jit.script
def torch_ema_kernel(series: torch.Tensor, alpha: float):
    n = series.size(0)
    ema = torch.empty_like(series)
    if n == 0: return ema
    ema[0] = series[0]
    one_minus_alpha = 1.0 - alpha
    for i in range(1, n):
        ema[i] = series[i] * alpha + ema[i-1] * one_minus_alpha
    return ema

def torch_ema(series, length):
    """High-performance EMA implementation in PyTorch using JIT compilation."""
    alpha = 2.0 / (length + 1)
    return torch_ema_kernel(series, float(alpha))

def torch_rsi(series, length):
    """Vectorized RSI implementation in PyTorch."""
    if series.size(0) <= length:
        return torch.full_like(series, 50.0)
    delta = series[1:] - series[:-1]
    gain = torch.clamp(delta, min=0)
    loss = torch.clamp(-delta, min=0)
    gain = torch.cat([torch.tensor([0.0], device=series.device), gain])
    loss = torch.cat([torch.tensor([0.0], device=series.device), loss])
    alpha_wilder = 1.0 / length
    avg_gain = torch_ema_kernel(gain, float(alpha_wilder))
    avg_loss = torch_ema_kernel(loss, float(alpha_wilder))
    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def torch_macd(series, fast=12, slow=26, signal=9):
    ema_f = torch_ema(series, fast)
    ema_s = torch_ema(series, slow)
    macd = ema_f - ema_s
    signal_line = torch_ema(macd, signal)
    hist = macd - signal_line
    return macd, signal_line, hist

def torch_adx(high, low, close, length=14):
    """High-performance ADX implementation in PyTorch."""
    if close.size(0) <= length:
        return torch.zeros_like(close)
    up = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    up = torch.cat([torch.tensor([0.0], device=high.device), up])
    down = torch.cat([torch.tensor([0.0], device=low.device), down])
    plus_dm = torch.where((up > down) & (up > 0), up, torch.tensor(0.0, device=high.device))
    minus_dm = torch.where((down > up) & (down > 0), down, torch.tensor(0.0, device=high.device))
    tr1 = high[1:] - low[1:]
    tr2 = torch.abs(high[1:] - close[:-1])
    tr3 = torch.abs(low[1:] - close[:-1])
    tr = torch.maximum(torch.maximum(tr1, tr2), tr3)
    tr = torch.cat([torch.tensor([0.0], device=high.device), tr])
    atr = torch_ema(tr, 2 * length - 1)
    plus_di = 100 * torch_ema(plus_dm, 2 * length - 1) / (atr + 1e-10)
    minus_di = 100 * torch_ema(minus_dm, 2 * length - 1) / (atr + 1e-10)
    dx = 100 * torch.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = torch_ema(dx, 2 * length - 1)
    return adx
STRATEGIES = [
    'simple_ema', 'simple_sma',
    'moving_averages', 'ichimoku_cloud', 'parabolic_sar', 'rsi_support_resistance',
    'bollinger_bands', 'macd_range', 'breakout_volume', 'donchian_channels',
    'atr_breakout', 'stochastic_rsi', 'williams_r', 'vwap_momentum',
    'order_flow_proxy', 'renko_proxy', 'tick_proxy', 'ema_rsi_volume',
    'macd_bollinger_bands', 'double_ema', 'double_ema_macd_rsi',
    'mc_mean_reversion', 'mc_momentum', 'mc_dynamic_allocation', 'mc_market_making',
    'mc_stop_loss_eval', 'mc_options_pricing',
    'whale_detection_proxy', 'pump_dump_proxy', 'market_regime_proxy', 'scientific_ensemble',
    'sentiment_momentum_proxy', 'liquidation_cascade_proxy', 'mvrv_proxy', 'adx_trend_strength',
    'pairs_trading_proxy', 'halving_cycle_proxy', 'listing_surge_proxy'
]

# Global MC engine for reuse
_mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=20)

def get_signals(df, mode_config, is_backtest=False):
    """
    Dispatcher for multiple trading strategies.
    Selected strategy is defined in mode_config['strategy'].
    """
    strategy = mode_config.get('strategy', 'simple_ema')
    device = mode_config.get('device', torch.device('cpu'))
    _mc_engine.set_device(device)

    # Common indicators for tendency and background analysis (Expert Mode)
    if df.empty: return finalize_signals(df)

    # Standardize hardware acceleration: enable MKLDNN if on CPU and supported
    if device.type == 'cpu' and torch.backends.mkldnn.is_available():
        torch.backends.mkldnn.enabled = True

    use_acceleration = (device.type != 'cpu') or (device.type == 'cpu' and torch.backends.mkldnn.enabled)

    if use_acceleration:
        close_t = torch.tensor(df['close'].values, device=device, dtype=torch.float64)
        high_t = torch.tensor(df['high'].values, device=device, dtype=torch.float64)
        low_t = torch.tensor(df['low'].values, device=device, dtype=torch.float64)
        df['ema_f'] = torch_ema(close_t, 9).to('cpu').numpy()
        df['ema_s'] = torch_ema(close_t, 21).to('cpu').numpy()
        m_val, m_sig, m_hist = torch_macd(close_t)
        df['macd_val'] = m_val.to('cpu').numpy()
        df['macd_sig'] = m_sig.to('cpu').numpy()
        df['macd_hist'] = m_hist.to('cpu').numpy()
        df['rsi'] = torch_rsi(close_t, 14).to('cpu').numpy()
        df['adx'] = torch_adx(high_t, low_t, close_t, 14).to('cpu').numpy()
    else:
        ema_f = ta.ema(df['close'], length=9)
        df['ema_f'] = ema_f.fillna(df['close']) if ema_f is not None else df['close']
        ema_s = ta.ema(df['close'], length=21)
        df['ema_s'] = ema_s.fillna(df['close']) if ema_s is not None else df['close']
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        if macd is not None:
            df['macd_val'] = macd.iloc[:, 0].fillna(0); df['macd_sig'] = macd.iloc[:, 1].fillna(0); df['macd_hist'] = macd.iloc[:, 2].fillna(0)
        else:
            df['macd_val'] = df['macd_sig'] = df['macd_hist'] = 0
        rsi = ta.rsi(df['close'], length=14)
        df['rsi'] = rsi.fillna(50) if rsi is not None else 50
        adx_df = ta.adx(df['high'], df['low'], df['close'])
        df['adx'] = adx_df.iloc[:, 0].fillna(0) if adx_df is not None else 0

    df['returns'] = np.log(df['close'] / df['close'].shift(1))
    df['volatility'] = df['returns'].rolling(window=20).std().fillna(0)

    # Whale Detection Proxy (Common)
    df['vol_ma_whale'] = ta.sma(df['volume'], length=20)
    df['vol_std_whale'] = df['volume'].rolling(window=20).std()
    df['whale_active'] = (df['volume'] > (df['vol_ma_whale'] + 3 * df['vol_std_whale'])).astype(int)

    # Market Regime Proxy (Common)
    df['vol_ma_regime'] = df['volatility'].rolling(window=50).mean()
    df['is_mean_rev'] = (df['volatility'] > df['vol_ma_regime']).astype(int)

    # Calculate tendency (Vectorized for performance)
    ema_diff = df['ema_f'] - df['ema_s']
    price = df['close']
    macd_hist = df['macd_hist']

    conditions = [
        (price == 0) | ema_diff.isna() | macd_hist.isna(),
        (ema_diff.abs() / price.replace(0, 1)) < 0.001,
        (ema_diff > 0) & (macd_hist > 0),
        (ema_diff < 0) & (macd_hist < 0)
    ]
    choices = ["Neutral", "Range", "Bullish", "Bearish"]
    df['tendency'] = np.select(conditions, choices, default="Neutral")

    # Strategy Selection
    if df.empty:
        return finalize_signals(df)
    if strategy == 'simple_ema':
        df = strategy_simple_ema(df, mode_config)
    elif strategy == 'simple_sma':
        df = strategy_simple_sma(df, mode_config)
    elif strategy == 'double_ema':
        df = strategy_double_ema(df, mode_config)
    elif strategy == 'double_ema_macd_rsi':
        df = strategy_double_ema_macd_rsi(df, mode_config)
    elif strategy == 'moving_averages':
        df = strategy_moving_averages(df, mode_config)
    elif strategy == 'ichimoku_cloud':
        df = strategy_ichimoku(df, mode_config)
    elif strategy == 'parabolic_sar':
        df = strategy_psar(df, mode_config)
    elif strategy == 'rsi_support_resistance':
        df = strategy_rsi_sr(df, mode_config)
    elif strategy == 'bollinger_bands':
        df = strategy_bollinger(df, mode_config)
    elif strategy == 'macd_range':
        df = strategy_macd_range(df, mode_config)
    elif strategy == 'breakout_volume':
        df = strategy_breakout_volume(df, mode_config)
    elif strategy == 'donchian_channels':
        df = strategy_donchian(df, mode_config)
    elif strategy == 'atr_breakout':
        df = strategy_atr_breakout(df, mode_config)
    elif strategy == 'stochastic_rsi':
        df = strategy_stoch_rsi(df, mode_config)
    elif strategy == 'williams_r':
        df = strategy_williams_r(df, mode_config)
    elif strategy == 'vwap_momentum':
        df = strategy_vwap_momentum(df, mode_config)
    elif strategy == 'order_flow_proxy':
        df = strategy_order_flow_proxy(df, mode_config)
    elif strategy == 'renko_proxy':
        df = strategy_renko_proxy(df, mode_config)
    elif strategy == 'tick_proxy':
        df = strategy_tick_proxy(df, mode_config)
    elif strategy == 'ema_rsi_volume':
        df = strategy_ema_rsi_volume(df, mode_config)
    elif strategy == 'macd_bollinger_bands':
        df = strategy_macd_bollinger(df, mode_config)
    elif strategy.startswith('mc_'):
        df = handle_mc_strategies(df, strategy, mode_config, is_backtest)
    elif strategy == 'whale_detection_proxy':
        df = strategy_whale_detection(df, mode_config)
    elif strategy == 'pump_dump_proxy':
        df = strategy_pump_dump(df, mode_config)
    elif strategy == 'market_regime_proxy':
        df = strategy_market_regime(df, mode_config)
    elif strategy == 'scientific_ensemble':
        df = strategy_scientific_ensemble(df, mode_config)
    elif strategy == 'sentiment_momentum_proxy':
        df = strategy_sentiment_momentum(df, mode_config)
    elif strategy == 'liquidation_cascade_proxy':
        df = strategy_liquidation_cascade(df, mode_config)
    elif strategy == 'mvrv_proxy':
        df = strategy_mvrv_proxy(df, mode_config)
    elif strategy == 'adx_trend_strength':
        df = strategy_adx_trend(df, mode_config)
    elif strategy == 'pairs_trading_proxy':
        df = strategy_pairs_trading(df, mode_config)
    elif strategy == 'halving_cycle_proxy':
        df = strategy_halving_cycle(df, mode_config)
    elif strategy == 'listing_surge_proxy':
        df = strategy_listing_surge(df, mode_config)
    else:
        df = strategy_simple_ema(df, mode_config)

    return finalize_signals(df)


def finalize_signals(df):
    """Signals are used directly without any confirmation window."""
    df['buy_signal'] = df['buy_candidate']
    df['sell_signal'] = df['sell_candidate']
    return df


def normalize_series(series):
    """Min-max normalization to [0, 1]."""
    if series.empty or series.max() == series.min():
        return series * 0
    return (series - series.min()) / (series.max() - series.min())

def calculate_similarity(buffer_df, pattern, device=torch.device('cpu')):
    """
    Calculates similarity between current buffer and a success pattern.
    Combines shape correlation (price) and technical state distance.
    Uses GPU acceleration via PyTorch if available.
    """
    if len(buffer_df) != len(pattern['prices']):
        return 0.0

    # GPU-accelerated Shape Correlation
    try:
        # Convert to tensors for fast computation
        c_vals = torch.tensor(buffer_df['close'].values, device=device, dtype=torch.float64)
        p_vals = torch.tensor(pattern['prices'], device=device, dtype=torch.float64)

        # Min-max normalization on GPU
        c_min, c_max = c_vals.min(), c_vals.max()
        if c_max > c_min:
            c_norm = (c_vals - c_min) / (c_max - c_min)
        else:
            c_norm = c_vals * 0

        p_min, p_max = p_vals.min(), p_vals.max()
        if p_max > p_min:
            p_norm = (p_vals - p_min) / (p_max - p_min)
        else:
            p_norm = p_vals * 0

        # Pearson Correlation using torch.corrcoef
        stacked = torch.stack([c_norm, p_norm])
        corr_mat = torch.corrcoef(stacked)
        shape_corr = float(corr_mat[0, 1].item())
        if np.isnan(shape_corr): shape_corr = 0.0
    except Exception:
        # Fallback to CPU/Pandas if torch fails
        current_prices = normalize_series(buffer_df['close'])
        pattern_prices = pd.Series(pattern['prices'])
        shape_corr = current_prices.corr(pattern_prices)
        if np.isnan(shape_corr): shape_corr = 0.0

    # 2. Technical State Distance (Euclidean)
    # We compare RSI and ADX states at the end of the window
    curr_rsi = buffer_df['rsi'].iloc[-1]
    curr_adx = buffer_df['adx'].iloc[-1]

    dist_rsi = abs(curr_rsi - pattern['tech_state']['rsi']) / 100.0
    dist_adx = abs(curr_adx - pattern['tech_state']['adx']) / 100.0
    tech_sim = 1.0 - (dist_rsi + dist_adx) / 2.0

    # Combined Score (Weight: 70% Shape, 30% Tech)
    combined = (0.7 * max(0, shape_corr)) + (0.3 * max(0, tech_sim))
    return combined

def handle_mc_strategies(df, strategy, config, is_backtest):
    """Helper to run MC strategies only on necessary rows."""
    df['buy_candidate'] = False
    df['sell_candidate'] = False

    # Range of indices to calculate (all if backtest, only last if not)
    # Actually backtest might need a window.
    start_idx = 0 if is_backtest else len(df) - 1

    if strategy == 'mc_mean_reversion':
        df['sma_20'] = ta.sma(df['close'], length=20)
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std()

        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            if np.isnan(row['volatility']) or row['volatility'] == 0: continue
            prob = _mc_engine.estimate_hit_probability(row['close'], row['sma_20'], row['volatility'], mode='above' if row['close'] < row['sma_20'] else 'below')
            df.at[df.index[i], 'buy_candidate'] = (row['close'] < row['sma_20']) and (prob > 0.7)
            df.at[df.index[i], 'sell_candidate'] = (row['close'] > row['sma_20']) and (prob > 0.7)

    elif strategy == 'mc_momentum':
        df['sma_20'] = ta.sma(df['close'], length=20)
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std()
        df['drift'] = df['returns'].rolling(window=20).mean()

        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            if np.isnan(row['volatility']) or row['volatility'] == 0: continue
            prob_up = _mc_engine.estimate_hit_probability(row['close'], row['close'] * 1.02, row['volatility'], drift=row['drift'], mode='above')
            prob_down = _mc_engine.estimate_hit_probability(row['close'], row['close'] * 0.98, row['volatility'], drift=row['drift'], mode='below')
            df.at[df.index[i], 'buy_candidate'] = (row['close'] > row['sma_20']) and (prob_up > 0.6)
            df.at[df.index[i], 'sell_candidate'] = (row['close'] < row['sma_20']) and (prob_down > 0.6)

    elif strategy == 'mc_dynamic_allocation':
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std()
        threshold = 0.05 / np.sqrt(365)
        df['buy_candidate'] = (df['volatility'] < threshold) & (df['volatility'].shift(1) >= threshold)
        df['sell_candidate'] = (df['volatility'] > threshold) & (df['volatility'].shift(1) <= threshold)

    elif strategy == 'mc_market_making':
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=10).std()
        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            if np.isnan(row['volatility']) or row['volatility'] == 0: continue
            prob_up = _mc_engine.estimate_hit_probability(row['close'], row['close'] * 1.001, row['volatility'], mode='above')
            prob_down = _mc_engine.estimate_hit_probability(row['close'], row['close'] * 0.999, row['volatility'], mode='below')
            df.at[df.index[i], 'buy_candidate'] = prob_up > 0.8
            df.at[df.index[i], 'sell_candidate'] = prob_down > 0.8

    elif strategy == 'mc_stop_loss_eval':
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std()
        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            if np.isnan(row['volatility']) or row['volatility'] == 0: continue
            prob_sl = _mc_engine.estimate_hit_probability(row['close'], row['close'] * 0.95, row['volatility'], mode='below')
            df.at[df.index[i], 'sell_candidate'] = prob_sl > 0.4

    elif strategy == 'mc_options_pricing':
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std()
        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            if np.isnan(row['volatility']) or row['volatility'] == 0: continue
            call_p = _mc_engine.price_option(row['close'], row['close'] * 1.05, row['volatility'], option_type='call')
            put_p = _mc_engine.price_option(row['close'], row['close'] * 0.95, row['volatility'], option_type='put')
            df.at[df.index[i], 'buy_candidate'] = call_p > put_p * 1.5
            df.at[df.index[i], 'sell_candidate'] = put_p > call_p * 1.5

    return finalize_signals(df)

# --- 1. TREND FOLLOWING ---

def strategy_moving_averages(df, config):
    df['ma_9'] = ta.ema(df['close'], length=9)
    df['ma_21'] = ta.ema(df['close'], length=21)
    df['ma_50'] = ta.ema(df['close'], length=50)
    df['ma_200'] = ta.ema(df['close'], length=200)

    # Fill NaN to avoid comparison errors
    df['ma_9'] = df['ma_9'].fillna(0)
    df['ma_21'] = df['ma_21'].fillna(0)
    df['ma_50'] = df['ma_50'].fillna(0)
    df['ma_200'] = df['ma_200'].fillna(0)

    df['buy_candidate'] = (df['ma_9'] > df['ma_21']) & (df['ma_9'].shift(1) <= df['ma_21'].shift(1)) & (df['close'] > df['ma_200'])
    df['sell_candidate'] = (df['close'] < df['ma_50']) & (df['close'].shift(1) >= df['ma_50'].shift(1))

    return finalize_signals(df)

def strategy_ichimoku(df, config):
    ichi_result = ta.ichimoku(df['high'], df['low'], df['close'])
    if ichi_result is not None and len(ichi_result) > 0:
        ichimoku = ichi_result[0]
        df['tenkan'] = ichimoku.iloc[:, 0].fillna(df['close'])
        df['kijun'] = ichimoku.iloc[:, 1].fillna(df['close'])
        df['span_a'] = ichimoku.iloc[:, 2].fillna(df['close'])
        df['span_b'] = ichimoku.iloc[:, 3].fillna(df['close'])
    else:
        df['tenkan'] = df['kijun'] = df['span_a'] = df['span_b'] = df['close']

    df['buy_candidate'] = (df['tenkan'] > df['kijun']) & (df['close'] > df['span_a']) & (df['close'] > df['span_b'])
    df['sell_candidate'] = (df['tenkan'] < df['kijun'])

    return finalize_signals(df)

def strategy_psar(df, config):
    psar = ta.psar(df['high'], df['low'], df['close'])
    if psar is not None and not psar.empty:
        l_col = [c for c in psar.columns if 'PSARl' in c]
        s_col = [c for c in psar.columns if 'PSARs' in c]
        df['psar_long'] = psar[l_col[0]] if l_col else np.nan
        df['psar_short'] = psar[s_col[0]] if s_col else np.nan
    else:
        df['psar_long'] = df['psar_short'] = np.nan

    df['buy_candidate'] = df['psar_long'].notna() & df['psar_long'].shift(1).isna()
    df['sell_candidate'] = df['psar_short'].notna() & df['psar_short'].shift(1).isna()

    return finalize_signals(df)

# --- 2. RANGE ---

def strategy_rsi_sr(df, config):
    df['rsi'] = ta.rsi(df['close'], length=14)
    df['support'] = df['low'].rolling(window=50).min()
    df['resistance'] = df['high'].rolling(window=50).max()

    # Fill defaults
    df['rsi'] = df['rsi'].fillna(50)
    df['support'] = df['support'].fillna(df['low'])
    df['resistance'] = df['resistance'].fillna(df['high'])

    df['buy_candidate'] = (df['rsi'] < 30) & (df['close'] <= df['support'] * 1.01)
    df['sell_candidate'] = (df['rsi'] > 70) & (df['close'] >= df['resistance'] * 0.99)

    return finalize_signals(df)

def strategy_bollinger(df, config):
    bb = ta.bbands(df['close'], length=20, std=2)
    if bb is not None and not bb.empty:
        df['bb_low'] = bb.iloc[:, 0].fillna(df['close'])
        df['bb_mid'] = bb.iloc[:, 1].fillna(df['close'])
        df['bb_high'] = bb.iloc[:, 2].fillna(df['close'])
    else:
        df['bb_low'] = df['bb_mid'] = df['bb_high'] = df['close']

    df['rsi'] = ta.rsi(df['close'], length=14)
    df['rsi'] = df['rsi'].fillna(50)

    df['buy_candidate'] = (df['close'] <= df['bb_low']) & (df['rsi'] < 35)
    df['sell_candidate'] = (df['close'] >= df['bb_mid'])

    return finalize_signals(df)

def strategy_macd_range(df, config):
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    if macd is not None:
        df['macd_val'] = macd.iloc[:, 0]
        df['macd_sig'] = macd.iloc[:, 1]
    else:
        df['macd_val'] = df['macd_sig'] = 0

    df['buy_candidate'] = (df['tendency'] == "Range") & (df['macd_val'] > df['macd_sig']) & (df['macd_val'].shift(1) <= df['macd_sig'].shift(1))
    df['sell_candidate'] = (df['macd_val'] < df['macd_sig']) & (df['macd_val'].shift(1) >= df['macd_sig'].shift(1))

    return finalize_signals(df)

# --- 3. BREAKOUT ---

def strategy_breakout_volume(df, config):
    df['resistance'] = df['high'].rolling(window=20).max().shift(1)
    df['vol_ma'] = ta.sma(df['volume'], length=20)

    df['buy_candidate'] = (df['close'] > df['resistance']) & (df['volume'] > df['vol_ma'] * 2)
    df['ma_20'] = ta.sma(df['close'], length=20)
    df['sell_candidate'] = (df['close'] < df['ma_20'])

    return finalize_signals(df)

def strategy_donchian(df, config):
    dc = ta.donchian(df['high'], df['low'], length=20)
    if dc is not None:
        df['dc_upper'] = dc.iloc[:, 0]
        df['dc_lower'] = dc.iloc[:, 2]
    else:
        df['dc_upper'] = df['high']
        df['dc_lower'] = df['low']

    df['buy_candidate'] = (df['close'] >= df['dc_upper'])
    df['sell_candidate'] = (df['close'] <= df['dc_lower'])

    return finalize_signals(df)

def strategy_atr_breakout(df, config):
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['resistance'] = df['high'].rolling(window=30).max().shift(1)

    df['buy_candidate'] = (df['close'] > df['resistance']) & (df['atr'] > df['atr'].shift(1))
    df['sell_candidate'] = (df['close'] < (df['close'].shift(1) - 2 * df['atr']))

    return finalize_signals(df)

# --- 4. MOMENTUM ---

def strategy_stoch_rsi(df, config):
    stoch = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
    if stoch is not None:
        df['stoch_k'] = stoch.iloc[:, 0]
    else:
        df['stoch_k'] = 50

    df['buy_candidate'] = (df['stoch_k'] < 20) & (df['stoch_k'] > df['stoch_k'].shift(1))
    df['sell_candidate'] = (df['stoch_k'] > 80) & (df['stoch_k'] < df['stoch_k'].shift(1))

    return finalize_signals(df)

def strategy_williams_r(df, config):
    df['willr'] = ta.willr(df['high'], df['low'], df['close'], length=14)

    df['buy_candidate'] = (df['willr'] < -80) & (df['willr'] > df['willr'].shift(1))
    df['sell_candidate'] = (df['willr'] > -20) & (df['willr'] < df['willr'].shift(1))

    return finalize_signals(df)

def strategy_vwap_momentum(df, config):
    df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

    df['buy_candidate'] = (df['close'] > df['vwap']) & (df['volume'] > df['volume'].shift(1))
    df['sell_candidate'] = (df['close'] < df['vwap'])

    return finalize_signals(df)

# --- 5. SCALPING (Proxies) ---

def strategy_order_flow_proxy(df, config):
    df['vol_delta'] = df['volume'] * (df['close'] - df['open']) / (df['high'] - df['low'] + 0.000001)
    df['vol_delta_ma'] = df['vol_delta'].rolling(window=10).mean()

    df['buy_candidate'] = (df['vol_delta'] > df['vol_delta_ma'] * 1.5) & (df['close'] > df['open'])
    df['sell_candidate'] = (df['vol_delta'] < 0)

    return finalize_signals(df)

def strategy_renko_proxy(df, config):
    df['body'] = (df['close'] - df['open']).abs()
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    df['buy_candidate'] = (df['body'] > df['atr']) & (df['close'] > df['open'])
    df['sell_candidate'] = (df['body'] > df['atr']) & (df['close'] < df['open'])

    return finalize_signals(df)

def strategy_tick_proxy(df, config):
    df['velocity'] = (df['close'] - df['close'].shift(5)) / 5

    df['buy_candidate'] = (df['velocity'] > df['velocity'].rolling(window=20).std() * 2)
    df['sell_candidate'] = (df['close'] < df['close'].shift(1))

    return finalize_signals(df)

# --- 6. HYBRIDS ---

def strategy_ema_rsi_volume(df, config):
    df['ema_9'] = ta.ema(df['close'], length=9).fillna(df['close'])
    df['ema_21'] = ta.ema(df['close'], length=21).fillna(df['close'])
    df['rsi'] = ta.rsi(df['close'], length=14).fillna(50)
    df['vol_ma'] = ta.sma(df['volume'], length=20).fillna(df['volume'])

    df['buy_candidate'] = (df['ema_9'] > df['ema_21']) & (df['rsi'] > 50) & (df['volume'] > df['vol_ma'])
    df['sell_candidate'] = (df['ema_9'] < df['ema_21'])

    return finalize_signals(df)

def strategy_macd_bollinger(df, config):
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    if macd is not None:
        df['macd_val'] = macd.iloc[:, 0]
        df['macd_sig'] = macd.iloc[:, 1]
    else:
        df['macd_val'] = df['macd_sig'] = 0

    bb = ta.bbands(df['close'], length=20, std=2)
    if bb is not None:
        df['bb_low'] = bb.iloc[:, 0]
    else:
        df['bb_low'] = df['close']

    df['buy_candidate'] = (df['macd_val'] > df['macd_sig']) & (df['close'] <= df['bb_low'] * 1.01)
    df['sell_candidate'] = (df['macd_val'] < df['macd_sig'])

    return finalize_signals(df)

# --- SCIENTIFIC PROXIES ---

def strategy_whale_detection(df, config):
    """
    Proxy for On-Chain metrics (Bartoletti et al., 2017).
    Detects unusual volume spikes accompanied by price stability/movement
    to infer big player activity.
    """
    df['vol_ma'] = ta.sma(df['volume'], length=20)
    df['vol_std'] = df['volume'].rolling(window=20).std()

    # Significant volume spike: volume > 3 standard deviations above mean
    df['whale_spike'] = df['volume'] > (df['vol_ma'] + 3 * df['vol_std'])

    # Buy if volume spike and price moves up
    df['buy_candidate'] = df['whale_spike'] & (df['close'] > df['close'].shift(1))
    df['sell_candidate'] = df['whale_spike'] & (df['close'] < df['close'].shift(1))

    return finalize_signals(df)

def strategy_pump_dump(df, config):
    """
    Proxy for Pump and Dump detection (Kamps et Kleinberg, 2018).
    Detects extreme price-volume divergence.
    """
    df['vol_change'] = df['volume'].pct_change()
    df['price_change'] = df['close'].pct_change()

    # Pump: Price and Volume both surge suddenly
    df['pump_detected'] = (df['vol_change'] > 5.0) & (df['price_change'] > 0.05)

    # Dump: After a pump, price stops growing but volume remains high or drops
    df['buy_candidate'] = False # Don't buy pumps
    df['sell_candidate'] = df['pump_detected'].shift(1) & (df['close'] < df['close'].shift(1))

    return df

def strategy_market_regime(df, config):
    """
    Mean-reversion vs Trend detection based on volatility (Baur et Dimpfl, 2021).
    """
    df['returns'] = np.log(df['close'] / df['close'].shift(1))
    df['volatility'] = df['returns'].rolling(window=20).std()
    df['vol_ma'] = df['volatility'].rolling(window=50).mean()

    # High volatility regime -> Mean Reversion (Bollinger Bands)
    bb = ta.bbands(df['close'], length=20, std=2)
    df['bb_low'] = bb.iloc[:, 0] if bb is not None else df['close']
    df['bb_high'] = bb.iloc[:, 2] if bb is not None else df['close']

    # Low volatility regime -> Trend Following (EMA)
    df['ema_9'] = ta.ema(df['close'], length=9).fillna(df['close'])
    df['ema_21'] = ta.ema(df['close'], length=21).fillna(df['close'])

    # Vectorized market regime switching
    df['buy_candidate'] = np.where(df['volatility'] > df['vol_ma'],
                                   df['close'] < df['bb_low'],
                                   df['ema_9'] > df['ema_21'])
    df['sell_candidate'] = np.where(df['volatility'] > df['vol_ma'],
                                    df['close'] > df['bb_high'],
                                    df['ema_9'] < df['ema_21'])

    return finalize_signals(df)

def strategy_scientific_ensemble(df, config):
    """
    LSTM/Machine Learning Ensemble Proxy (Makarov et al., 2019; Zhang et al., 2020).
    Weights MACD, RSI, and Bollinger.
    """
    # Use existing macd/rsi from get_signals
    bb = ta.bbands(df['close'], length=20, std=2)
    df['bb_low'] = bb.iloc[:, 0] if bb is not None else df['close']
    df['bb_high'] = bb.iloc[:, 2] if bb is not None else df['close']

    # Score-based approach
    df['score'] = 0
    df.loc[df['rsi'] < 35, 'score'] += 1
    df.loc[df['rsi'] > 65, 'score'] -= 1
    df.loc[df['macd_val'] > df['macd_sig'], 'score'] += 1
    df.loc[df['macd_val'] < df['macd_sig'], 'score'] -= 1
    df.loc[df['close'] < df['bb_low'], 'score'] += 1
    df.loc[df['close'] > df['bb_high'], 'score'] -= 1

    df['buy_candidate'] = df['score'] >= 1
    df['sell_candidate'] = df['score'] <= -1

    return finalize_signals(df)

def strategy_sentiment_momentum(df, config):
    """
    Social Media Sentiment Proxy (Abraham et al., 2018).
    Uses price acceleration and RSI divergence as a proxy for "FOMO" or "Fear".
    """
    df['rsi'] = ta.rsi(df['close'], length=14).fillna(50)
    df['roc'] = ta.roc(df['close'], length=10).fillna(0)
    df['acceleration'] = df['roc'].diff().fillna(0)

    # Positive sentiment: Price accelerating upwards + RSI not yet overbought
    df['buy_candidate'] = (df['acceleration'] > 0) & (df['roc'] > 0) & (df['rsi'] < 60)
    # Negative sentiment: Price decelerating or dropping fast + RSI oversold (panic)
    df['sell_candidate'] = (df['acceleration'] < 0) & (df['roc'] < 0) & (df['rsi'] > 40)

    return finalize_signals(df)

def strategy_liquidation_cascade(df, config):
    """
    Liquidation Cascade Proxy (Makarov et Schoar, 2020).
    Detects high-volume sharp price drops (long liquidations) as buying opportunities,
    or sharp rises as selling opportunities.
    """
    df['pct_change'] = df['close'].pct_change().fillna(0)
    df['vol_ma'] = ta.sma(df['volume'], length=20).fillna(df['volume'])

    # Cascade: Price drops > 2% in one candle + Volume > 2x average
    df['long_liquidation'] = (df['pct_change'] < -0.02) & (df['volume'] > df['vol_ma'] * 2)
    df['short_liquidation'] = (df['pct_change'] > 0.02) & (df['volume'] > df['vol_ma'] * 2)

    # Buy the blood (after cascade)
    df['buy_candidate'] = df['long_liquidation'].shift(1) & (df['close'] > df['close'].shift(1))
    # Sell the squeeze
    df['sell_candidate'] = df['short_liquidation'].shift(1) & (df['close'] < df['close'].shift(1))

    return df

def strategy_mvrv_proxy(df, config):
    """
    MVRV Ratio Proxy (Ciaian et al., 2018).
    Proxy: Price / 200-day Moving Average (Market Value to 'Realized' Value proxy).
    """
    df['realized_proxy'] = ta.sma(df['close'], length=200).fillna(df['close'])
    df['mvrv_proxy'] = df['close'] / df['realized_proxy']

    # Buy when undervalued (MVRV < 0.8), sell when overvalued (MVRV > 2.0)
    df['buy_candidate'] = df['mvrv_proxy'] < 0.95
    df['sell_candidate'] = df['mvrv_proxy'] > 1.05

    return finalize_signals(df)

def strategy_adx_trend(df, config):
    """
    ADX Trend Strength (Zhang et al., 2020).
    Only trade when trend is strong (ADX > 25).
    """
    adx = ta.adx(df['high'], df['low'], df['close'])
    if adx is not None:
        df['adx'] = adx.iloc[:, 0].fillna(0)
        df['dmp'] = adx.iloc[:, 1].fillna(0)
        df['dmn'] = adx.iloc[:, 2].fillna(0)
    else:
        df['adx'] = df['dmp'] = df['dmn'] = 0

    df['buy_candidate'] = (df['adx'] > 25) & (df['dmp'] > df['dmn'])
    df['sell_candidate'] = (df['adx'] > 25) & (df['dmn'] > df['dmp'])

    return finalize_signals(df)

def strategy_pairs_trading(df, config):
    """
    Statistical Arbitrage Proxy (Grobys et al., 2020).
    Proxy: Asset vs moving average of its own price (Self-pairs trading/Mean reversion).
    """
    df['ma_50'] = ta.sma(df['close'], length=50).fillna(df['close'])
    df['z_score'] = (df['close'] - df['ma_50']) / df['close'].rolling(window=50).std()

    df['buy_candidate'] = df['z_score'] < -2.0
    df['sell_candidate'] = df['z_score'] > 2.0

    return df

def strategy_halving_cycle(df, config):
    """
    Bitcoin Halving Cycle Proxy (Bouoiyour & Selmi, 2020).
    Uses very long term EMA (200) to ensure alignment with major market cycles.
    """
    df['ema_200'] = ta.ema(df['close'], length=200).fillna(df['close'])
    df['ema_50'] = ta.ema(df['close'], length=50).fillna(df['close'])

    # Buy only when above 200 EMA (Bull market cycle)
    df['buy_candidate'] = (df['close'] > df['ema_200']) & (df['close'] > df['ema_50']) & (df['close'].shift(1) <= df['ema_50'].shift(1))
    df['sell_candidate'] = (df['close'] < df['ema_50'])

    return finalize_signals(df)

def strategy_listing_surge(df, config):
    """
    Exchange Listing Surge Proxy (Hau et al., 2021).
    Detects extreme volume increase on relatively "flat" price history.
    """
    df['vol_ma'] = ta.sma(df['volume'], length=50).fillna(df['volume'])
    df['price_std'] = df['close'].rolling(window=50).std().fillna(0)

    # Surge: Volume > 10x average + Price breakout
    df['surge'] = (df['volume'] > df['vol_ma'] * 5) & (df['close'] > df['close'].shift(1) + 2 * df['price_std'])

    df['buy_candidate'] = df['surge']
    df['sell_candidate'] = df['close'] < df['close'].shift(3) # Exit fast after surge

    return df

# --- LEGACY / ORIGINAL ---

def strategy_simple_ema(df, config):
    ema_fast = config.get('ema_fast', 9)
    ema_slow = config.get('ema_slow', 21)
    df['ema_f_strat'] = ta.ema(df['close'], length=ema_fast)
    df['ema_s_strat'] = ta.ema(df['close'], length=ema_slow)
    df['buy_candidate'] = (df['ema_f_strat'] > df['ema_s_strat']) & (df['ema_f_strat'].shift(1) <= df['ema_s_strat'].shift(1))
    df['sell_candidate'] = (df['ema_f_strat'] < df['ema_s_strat']) & (df['ema_f_strat'].shift(1) >= df['ema_s_strat'].shift(1))
    return finalize_signals(df)

def strategy_simple_sma(df, config):
    sma_fast = config.get('ema_fast', 9)
    sma_slow = config.get('ema_slow', 21)
    df['sma_f_strat'] = ta.sma(df['close'], length=sma_fast)
    df['sma_s_strat'] = ta.sma(df['close'], length=sma_slow)
    df['buy_candidate'] = (df['sma_f_strat'] > df['sma_s_strat']) & (df['sma_f_strat'].shift(1) <= df['sma_s_strat'].shift(1))
    df['sell_candidate'] = (df['sma_f_strat'] < df['sma_s_strat']) & (df['sma_f_strat'].shift(1) >= df['sma_s_strat'].shift(1))
    return finalize_signals(df)

def strategy_double_ema(df, config):
    ema_fast = config.get('ema_fast', 8)
    ema_slow = config.get('ema_slow', 18)
    device = config.get('device', torch.device('cpu'))
    if (device.type != 'cpu') or torch.backends.mkldnn.enabled:
        close_t = torch.tensor(df['close'].values, device=device, dtype=torch.float64)
        df['ema_f'] = torch_ema(close_t, ema_fast).to('cpu').numpy()
        df['ema_s'] = torch_ema(close_t, ema_slow).to('cpu').numpy()
    else:
        df['ema_f'] = ta.ema(df['close'], length=ema_fast)
        df['ema_s'] = ta.ema(df['close'], length=ema_slow)
    df['buy_candidate'] = (df['ema_f'] > df['ema_s']) & (df['ema_f'].shift(1) <= df['ema_s'].shift(1))
    df['sell_candidate'] = (df['ema_f'] < df['ema_s']) & (df['ema_f'].shift(1) >= df['ema_s'].shift(1))
    return finalize_signals(df)

def strategy_double_ema_macd_rsi(df, config):
    ema_fast = config.get('ema_fast', 8)
    ema_slow = config.get('ema_slow', 18)
    macd_f = config.get('macd_fast', 12)
    macd_s = config.get('macd_slow', 26)
    macd_sig = config.get('macd_signal', 9)
    rsi_p = config.get('rsi_period', 14)
    device = config.get('device', torch.device('cpu'))

    if (device.type != 'cpu') or torch.backends.mkldnn.enabled:
        close_t = torch.tensor(df['close'].values, device=device, dtype=torch.float64)
        df['ema_f_strat'] = torch_ema(close_t, ema_fast).to('cpu').numpy()
        df['ema_s_strat'] = torch_ema(close_t, ema_slow).to('cpu').numpy()
        m_val, m_sig, _ = torch_macd(close_t, fast=macd_f, slow=macd_s, signal=macd_sig)
        df['macd_val_strat'] = m_val.to('cpu').numpy()
        df['macd_sig_strat'] = m_sig.to('cpu').numpy()
        df['rsi_strat'] = torch_rsi(close_t, rsi_p).to('cpu').numpy()
    else:
        df['ema_f_strat'] = ta.ema(df['close'], length=ema_fast)
        df['ema_s_strat'] = ta.ema(df['close'], length=ema_slow)
        macd = ta.macd(df['close'], fast=macd_f, slow=macd_s, signal=macd_sig)
        if macd is not None:
            df['macd_val_strat'] = macd.iloc[:, 0]
            df['macd_sig_strat'] = macd.iloc[:, 1]
        else:
            df['macd_val_strat'] = df['macd_sig_strat'] = 0
        df['rsi_strat'] = ta.rsi(df['close'], length=rsi_p)

    df['ema_up'] = (df['ema_f_strat'] > df['ema_s_strat']) & (df['ema_f_strat'].shift(1) <= df['ema_s_strat'].shift(1))
    df['ema_down'] = (df['ema_f_strat'] < df['ema_s_strat']) & (df['ema_f_strat'].shift(1) >= df['ema_s_strat'].shift(1))
    df['ema_up'] = df['ema_up'].fillna(False); df['ema_down'] = df['ema_down'].fillna(False)

    df['macd_up'] = (df['macd_val_strat'] > df['macd_sig_strat']) & (df['macd_val_strat'].shift(1) <= df['macd_sig_strat'].shift(1))
    df['macd_down'] = (df['macd_val_strat'] < df['macd_sig_strat']) & (df['macd_val_strat'].shift(1) >= df['macd_sig_strat'].shift(1))
    df['macd_up'] = df['macd_up'].fillna(False); df['macd_down'] = df['macd_down'].fillna(False)

    rsi_b = config.get('rsi_buy', 30)
    rsi_s = config.get('rsi_sell', 70)
    df['rsi_up'] = (df['rsi_strat'] < rsi_b) & (df['rsi_strat'] > df['rsi_strat'].shift(1))
    df['rsi_down'] = (df['rsi_strat'] > rsi_s) & (df['rsi_strat'] < df['rsi_strat'].shift(1))
    df['rsi_up'] = df['rsi_up'].fillna(False); df['rsi_down'] = df['rsi_down'].fillna(False)

    df['buy_candidate'] = df['ema_up'] & df['macd_up'] & df['rsi_up']
    df['sell_candidate'] = df['ema_down'] & df['macd_down'] & df['rsi_down']
    return df
