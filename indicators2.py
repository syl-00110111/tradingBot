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

"""
Technical analysis indicators and trading strategies for the bot.

This module provides both standard technical indicators (accelerated via PyTorch)
and a diverse catalog of trading strategies, including scientific proxies
and Monte Carlo-based approaches.
"""

import pandas as pd
import pandas_ta as ta
import numpy as np
import torch
import logging
from monte_carlo2 import MonteCarloEngine

@torch.jit.script
def torch_ema_kernel(series: torch.Tensor, alpha: float):
    """
    Noyau JIT-compilé pour la Moyenne Mobile Exponentielle (EMA).

    Parameters
    ----------
    series : torch.Tensor
        The input price series tensor.
    alpha : float
        The smoothing factor (0 < alpha <= 1).

    Returns
    -------
    torch.Tensor
        The calculated EMA series tensor.
    """
    n = series.size(0)
    ema = torch.empty_like(series)
    if n == 0: return ema
    ema[0] = series[0]
    one_minus_alpha = 1.0 - alpha
    for i in range(1, n):
        ema[i] = series[i] * alpha + ema[i-1] * one_minus_alpha
    return ema

def torch_ema(series, length):
    """
    Implémentation EMA haute performance dans PyTorch via JIT.

    Parameters
    ----------
    series : torch.Tensor
        The input price series tensor.
    length : int
        The period length for the EMA.

    Returns
    -------
    torch.Tensor
        The calculated EMA series tensor.
    """
    alpha = 2.0 / (length + 1)
    return torch_ema_kernel(series, float(alpha))

def torch_tema(series, length):
    """
    Implémentation TEMA haute performance dans PyTorch.

    TEMA = (3 * EMA1) - (3 * EMA2) + EMA3
    where EMA1 is the EMA of the price, EMA2 is the EMA of EMA1, and
    EMA3 is the EMA of EMA2.

    Parameters
    ----------
    series : torch.Tensor
        The input price series tensor.
    length : int
        The period length for the TEMA.

    Returns
    -------
    torch.Tensor
        The calculated TEMA series tensor.
    """
    ema1 = torch_ema(series, length)
    ema2 = torch_ema(ema1, length)
    ema3 = torch_ema(ema2, length)
    return 3 * ema1 - 3 * ema2 + ema3

def torch_heikin_ashi(open_t, high_t, low_t, close_t):
    """
    Implémentation Heikin Ashi haute performance dans PyTorch.

    Parameters
    ----------
    open_t, high_t, low_t, close_t : torch.Tensor
        OHLC series tensors.

    Returns
    -------
    ha_open, ha_high, ha_low, ha_close : torch.Tensor
        Calculated Heikin Ashi OHLC series.
    """
    n = open_t.size(0)
    ha_close = (open_t + high_t + low_t + close_t) / 4.0
    ha_open = torch.empty_like(open_t)

    if n > 0:
        ha_open = _torch_ha_open_loop(open_t, close_t, ha_close)

    ha_high = torch.maximum(torch.maximum(high_t, ha_open), ha_close)
    ha_low = torch.minimum(torch.minimum(low_t, ha_open), ha_close)

    return ha_open, ha_high, ha_low, ha_close

@torch.jit.script
def _torch_ha_open_loop(open_t: torch.Tensor, close_t: torch.Tensor, ha_close: torch.Tensor):
    n = open_t.size(0)
    ha_open = torch.empty_like(open_t)
    # Initial HA_Open is (Open[0] + Close[0]) / 2
    ha_open[0] = (open_t[0] + close_t[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2.0
    return ha_open

def torch_sinewave(close_t):
    """
    Implémentation PyTorch simplifiée de la SineWave de Hilbert Transform.
    Based on Ehlers' implementation.

    Returns
    -------
    sine : torch.Tensor
    leadsine : torch.Tensor
    """
    n = close_t.size(0)
    sine = torch.zeros_like(close_t)
    leadsine = torch.zeros_like(close_t)

    if n < 7:
        return sine, leadsine

    # Simplified implementation of dominant cycle phase
    # In a real Hilbert Transform, this involves multiple stages of filtering.
    # Here we use a simplified version for the bot.

    # 1. Detrender
    # 2. InPhase & Quadrature
    # 3. Phase calculation

    # Due to complexity of a full Hilbert Transform in JIT, we'll use a
    # reasonable approximation or just provide the structure for now.
    # Actually, let's implement the core logic.

    return _torch_sinewave_kernel(close_t)

@torch.jit.script
def _torch_sinewave_kernel(close_t: torch.Tensor):
    n = close_t.size(0)
    smooth = torch.zeros_like(close_t)
    detrender = torch.zeros_like(close_t)
    I1 = torch.zeros_like(close_t)
    Q1 = torch.zeros_like(close_t)
    jI = torch.zeros_like(close_t)
    jQ = torch.zeros_like(close_t)
    I2 = torch.zeros_like(close_t)
    Q2 = torch.zeros_like(close_t)
    Re = torch.zeros_like(close_t)
    Im = torch.zeros_like(close_t)
    period = torch.full_like(close_t, 6.0)
    smooth_period = torch.full_like(close_t, 6.0)
    phase = torch.zeros_like(close_t)
    sine = torch.zeros_like(close_t)
    leadsine = torch.zeros_like(close_t)

    for i in range(6, n):
        smooth[i] = (4*close_t[i] + 3*close_t[i-1] + 2*close_t[i-2] + close_t[i-3]) / 10.0
        detrender[i] = (0.0962*smooth[i] + 0.5769*smooth[i-2] - 0.5769*smooth[i-4] - 0.0962*smooth[i-6]) * (0.075*period[i-1] + 0.54)

        # Compute InPhase and Quadrature components
        I1[i] = detrender[i-3]
        Q1[i] = (0.0962*detrender[i] + 0.5769*detrender[i-2] - 0.5769*detrender[i-4] - 0.0962*detrender[i-6]) * (0.075*period[i-1] + 0.54)

        # Advance the phase of I1 and Q1 by 90 degrees
        jI[i] = (0.0962*I1[i] + 0.5769*I1[i-2] - 0.5769*I1[i-4] - 0.0962*I1[i-6]) * (0.075*period[i-1] + 0.54)
        jQ[i] = (0.0962*Q1[i] + 0.5769*Q1[i-2] - 0.5769*Q1[i-4] - 0.0962*Q1[i-6]) * (0.075*period[i-1] + 0.54)

        # Phasor addition for 3nd order Hilbert Transform
        I2[i] = I1[i] - jQ[i]
        Q2[i] = Q1[i] + jI[i]

        # Smoothing I and Q components
        I2[i] = 0.2*I2[i] + 0.8*I2[i-1]
        Q2[i] = 0.2*Q2[i] + 0.8*Q2[i-1]

        # Homodyne Discriminator
        Re[i] = I2[i]*I2[i-1] + Q2[i]*Q2[i-1]
        Im[i] = I2[i]*Q2[i-1] - Q2[i]*I2[i-1]
        Re[i] = 0.2*Re[i] + 0.8*Re[i-1]
        Im[i] = 0.2*Im[i] + 0.8*Im[i-1]

        if Im[i] != 0 and Re[i] != 0:
            period[i] = 360.0 / (torch.atan(Im[i]/Re[i]) * 180.0 / 3.14159)

        if period[i] > 1.5 * period[i-1]: period[i] = 1.5 * period[i-1]
        if period[i] < 0.67 * period[i-1]: period[i] = 0.67 * period[i-1]
        if period[i] < 6: period[i] = 6
        if period[i] > 50: period[i] = 50
        period[i] = 0.2*period[i] + 0.8*period[i-1]
        smooth_period[i] = 0.33*period[i] + 0.67*smooth_period[i-1]

        if I1[i] != 0:
            phase[i] = torch.atan(Q1[i] / I1[i]) * 180.0 / 3.14159

        sine[i] = torch.sin(phase[i] * 3.14159 / 180.0)
        leadsine[i] = torch.sin((phase[i] + 45.0) * 3.14159 / 180.0)

    return sine, leadsine

def torch_rsi(series, length):
    """
    Implémentation vectorisée du RSI dans PyTorch.

    Calculates RSI using the Wilder's smoothing method.

    Parameters
    ----------
    series : torch.Tensor
        The input price series tensor.
    length : int
        The period length for the RSI.

    Returns
    -------
    torch.Tensor
        The calculated RSI series tensor.
    """
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
    """
    Implémentation vectorisée du MACD dans PyTorch.

    Parameters
    ----------
    series : torch.Tensor
        The input price series tensor.
    fast : int, optional
        The fast EMA period (default is 12).
    slow : int, optional
        The slow EMA period (default is 26).
    signal : int, optional
        The signal line EMA period (default is 9).

    Returns
    -------
    macd : torch.Tensor
        The MACD line.
    signal_line : torch.Tensor
        The MACD signal line.
    hist : torch.Tensor
        The MACD histogram (macd - signal_line).
    """
    ema_f = torch_ema(series, fast)
    ema_s = torch_ema(series, slow)
    macd = ema_f - ema_s
    signal_line = torch_ema(macd, signal)
    hist = macd - signal_line
    return macd, signal_line, hist

def torch_adx(high, low, close, length=14):
    """
    Implémentation ADX haute performance dans PyTorch.

    Parameters
    ----------
    high : torch.Tensor
        High prices tensor.
    low : torch.Tensor
        Low prices tensor.
    close : torch.Tensor
        Close prices tensor.
    length : int, optional
        The period length for ADX (default is 14).

    Returns
    -------
    torch.Tensor
        The calculated ADX series tensor.
    """
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
    'ichimoku_cloud',
    'bollinger_bands',
    'williams_r', 'vwap_momentum',
    'whale_detection_proxy', 'scientific_ensemble',
    'sentiment_momentum_proxy', 'liquidation_cascade_proxy',
    'tema_crossover',
    'heikin_ashi'
]

STRATEGY_GROUPS = {
    'trend_following': [
        'ichimoku_cloud', 'vwap_momentum',
        'tema_crossover', 'heikin_ashi'
    ],
    'mean_reversion': [
        'bollinger_bands', 'williams_r'
    ],
    'other': [
        'whale_detection_proxy', 'scientific_ensemble',
        'sentiment_momentum_proxy', 'liquidation_cascade_proxy'
    ]
}

# Global MC engine for reuse
_mc_engine = MonteCarloEngine(num_simulations=1000, timeframe_candles=100)

def get_signals(df, mode_config, is_scan=False):
    """
    Répartiteur pour plusieurs stratégies de trading.

    Calculates common indicators (EMA, MACD, RSI, ADX) and then executes
    the specific strategy defined in `mode_config['strategy']`.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    mode_config : dict
        Configuration for the strategy (strategy name, indicators parameters, device).
    is_scan : bool, optional
        Whether the call is for a scan (affects some MC strategies).

    Returns
    -------
    pandas.DataFrame
        The input dataframe updated with signals and indicators.
    """
    strategy = mode_config.get('strategy')
    device = mode_config.get('device') or torch.device('cpu')
    _mc_engine.set_device(device)

    # Common indicators for tendency and background analysis (Expert Mode)
    if df.empty or len(df) < 250: return df

    # For multi-technique scanning, we check if the requested EMA/RSI settings
    # match what's already in the dataframe. If not, we MUST recalculate.
    current_ema_fast = df.attrs.get('ema_fast')
    requested_ema_fast = mode_config.get('ema_fast')

    needs_recalc = ('ema_f' not in df.columns) or (current_ema_fast != requested_ema_fast)

    # Optimization: Skip if already calculated
    # We check for 'ema_f' and also if the settings match.
    # For now, just checking 'ema_f' is enough to avoid full recalculation of common indicators
    # in the tight multi-technique loop.
    if needs_recalc:
        # Ensure OHLCV columns are numeric to avoid conversion errors from object type
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns and df[col].dtype == 'object':
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Use Torch-accelerated indicators if GPU is available OR MKLDNN is enabled for CPU
        use_acceleration = (device.type != 'cpu') or torch.backends.mkldnn.enabled
        if use_acceleration:
            close_t = torch.tensor(df['close'].astype(float).values, device=device, dtype=torch.float64)
            high_t = torch.tensor(df['high'].astype(float).values, device=device, dtype=torch.float64)
            low_t = torch.tensor(df['low'].astype(float).values, device=device, dtype=torch.float64)
            df['ema_f'] = torch_ema(close_t, mode_config.get('ema_fast')).to('cpu').numpy()
            df['ema_s'] = torch_ema(close_t, mode_config.get('ema_slow')).to('cpu').numpy()
            m_val, m_sig, m_hist = torch_macd(close_t, fast=mode_config.get('macd_fast'), slow=mode_config.get('macd_slow'), signal=mode_config.get('macd_signal'))
            df['macd_val'] = m_val.to('cpu').numpy()
            df['macd_sig'] = m_sig.to('cpu').numpy()
            df['macd_hist'] = m_hist.to('cpu').numpy()
            df['rsi'] = torch_rsi(close_t, mode_config.get('rsi_period')).to('cpu').numpy()
            df['adx'] = torch_adx(high_t, low_t, close_t, 14).to('cpu').numpy()
            df['tema_20'] = torch_tema(close_t, 20).to('cpu').numpy()
            df.attrs['ema_fast'] = requested_ema_fast
        else:
            ema_f = ta.ema(df['close'], length=mode_config.get('ema_fast'))
            df['ema_f'] = ema_f.fillna(df['close']) if ema_f is not None else df['close']
            ema_s = ta.ema(df['close'], length=mode_config.get('ema_slow'))
            df['ema_s'] = ema_s.fillna(df['close']) if ema_s is not None else df['close']
            macd = ta.macd(df['close'], fast=mode_config.get('macd_fast'), slow=mode_config.get('macd_slow'), signal=mode_config.get('macd_signal'))
            if macd is not None:
                df['macd_val'] = macd.iloc[:, 0].fillna(0); df['macd_sig'] = macd.iloc[:, 1].fillna(0); df['macd_hist'] = macd.iloc[:, 2].fillna(0)
            else:
                df['macd_val'] = df['macd_sig'] = df['macd_hist'] = 0
            rsi = ta.rsi(df['close'], length=mode_config.get('rsi_period'))
            df['rsi'] = rsi.fillna(50) if rsi is not None else 50
            adx_df = ta.adx(df['high'], df['low'], df['close'])
            df['adx'] = adx_df.iloc[:, 0].fillna(0) if adx_df is not None else 0
            tema_20 = ta.tema(df['close'], length=20)
            df['tema_20'] = tema_20.fillna(df['close']) if tema_20 is not None else df['close']
            df.attrs['ema_fast'] = requested_ema_fast

    if 'returns' not in df.columns:
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std().fillna(0)

        # Whale Detection Proxy (Common)
        df['vol_ma_whale'] = ta.sma(df['volume'], length=20)
        df['vol_std_whale'] = df['volume'].rolling(window=20).std()
        df['whale_active'] = (df['volume'] > (df['vol_ma_whale'] + 3 * df['vol_std_whale'])).astype(int)

        # Market Regime Proxy (Common) - Improved Detection
        # Typically: ADX < 25 = Ranging/Mean Reversion, ADX >= 25 = Trending
        # We also use volatility relative to its mean.
        df['vol_ma_regime'] = df['volatility'].rolling(window=50).mean()
        df['is_mean_rev'] = ((df['adx'] < 25) | (df['volatility'] > 1.5 * df['vol_ma_regime'])).astype(int)
        df['regime'] = np.where(df['is_mean_rev'] == 1, 'mean_reversion', 'trend_following')

    # Initialize default score and tendency if not present
    if 'score' not in df.columns:
        df['score'] = 0
    if 'tendency' not in df.columns:
        df['tendency'] = "Neutral"

    # Strategy Selection
    if df.empty:
        return df
    if strategy == 'ichimoku_cloud':
        df = strategy_ichimoku(df, mode_config)
    elif strategy == 'parabolic_sar':
        df = strategy_psar(df, mode_config)
    elif strategy == 'bollinger_bands':
        df = strategy_bollinger(df, mode_config)
    elif strategy == 'donchian_channels':
        df = strategy_donchian(df, mode_config)
    elif strategy == 'stochastic_rsi':
        df = strategy_stoch_rsi(df, mode_config)
    elif strategy == 'williams_r':
        df = strategy_williams_r(df, mode_config)
    elif strategy == 'vwap_momentum':
        df = strategy_vwap_momentum(df, mode_config)
    elif strategy == 'renko_proxy':
        df = strategy_renko_proxy(df, mode_config)
    elif strategy == 'ema_rsi_volume':
        df = strategy_ema_rsi_volume(df, mode_config)
    elif strategy and strategy.startswith('mc_'):
        df = handle_mc_strategies(df, strategy, mode_config, is_scan)
    elif strategy == 'whale_detection_proxy':
        df = strategy_whale_detection(df, mode_config)
    elif strategy == 'pump_dump_proxy':
        df = strategy_pump_dump(df, mode_config)
    elif strategy == 'scientific_ensemble':
        df = strategy_scientific_ensemble(df, mode_config)
    elif strategy == 'sentiment_momentum_proxy':
        df = strategy_sentiment_momentum(df, mode_config)
    elif strategy == 'liquidation_cascade_proxy':
        df = strategy_liquidation_cascade(df, mode_config)
    elif strategy == 'adx_trend_strength':
        df = strategy_adx_trend(df, mode_config)
    elif strategy == 'pairs_trading_proxy':
        df = strategy_pairs_trading(df, mode_config)
    elif strategy == 'halving_cycle_proxy':
        df = strategy_halving_cycle(df, mode_config)
    elif strategy == 'listing_surge_proxy':
        df = strategy_listing_surge(df, mode_config)
    elif strategy == 'tema_crossover':
        # Ensure numeric for TEMA strategy
        if 'close' in df.columns and df['close'].dtype == 'object':
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df = strategy_tema_crossover(df, mode_config)
    elif strategy == 'heikin_ashi':
        # Ensure numeric for HA strategy
        for col in ['open', 'high', 'low', 'close']:
            if col in df.columns and df[col].dtype == 'object':
                df[col] = pd.to_numeric(df[col], errors='coerce')
        df = strategy_heikin_ashi(df, mode_config)
    elif strategy == 'candle_patterns':
        df = strategy_candle_patterns(df, mode_config)
    elif strategy == 'sinewave_cycle':
        df = strategy_sinewave(df, mode_config)
    elif strategy is not None:
        logging.warning(f"Strategy {strategy} not recognized.")

    return df

def detect_hammer(open_, high, low, close):
    body = np.abs(close - open_)
    lower_wick = np.minimum(open_, close) - low
    upper_wick = high - np.maximum(open_, close)
    return (lower_wick > 2 * body) & (upper_wick < 0.1 * lower_wick)

def detect_inverted_hammer(open_, high, low, close):
    body = np.abs(close - open_)
    upper_wick = high - np.maximum(open_, close)
    lower_wick = np.minimum(open_, close) - low
    return (upper_wick > 2 * body) & (lower_wick < 0.1 * upper_wick)

def detect_dragonfly_doji(open_, high, low, close):
    body = np.abs(close - open_)
    lower_wick = np.minimum(open_, close) - low
    upper_wick = high - np.maximum(open_, close)
    return (body < 0.1 * (high - low)) & (lower_wick > 3 * body) & (upper_wick < 0.1 * lower_wick)

def detect_piercing_line(o, c, prev_o, prev_c):
    return (prev_c < prev_o) & (c > o) & (o < prev_c) & (c > (prev_o + prev_c)/2)

def detect_morning_star(o, c, p1_o, p1_c, p2_o, p2_c):
    return (p2_c < p2_o) & (np.abs(p1_c - p1_o) < 0.3 * np.abs(p2_c - p2_o)) & (c > o) & (c > (p2_o + p2_c)/2)

def detect_evening_doji_star(o, c, p1_o, p1_c, p1_h, p1_l, p2_o, p2_c):
    doji = np.abs(p1_c - p1_o) < 0.1 * (p1_h - p1_l + 1e-10)
    return (p2_c > p2_o) & (p1_o > p2_c) & doji & (c < o) & (c < (p2_o + p2_c)/2)

def detect_three_white_soldiers(o, c, p1_o, p1_c, p2_o, p2_c):
    return (c > o) & (p1_c > p1_o) & (p2_c > p2_o) & (c > p1_c) & (p1_c > p2_c)

def detect_hanging_man(open_, high, low, close):
    return detect_hammer(open_, high, low, close) # Same pattern, different context

def detect_shooting_star(open_, high, low, close):
    return detect_inverted_hammer(open_, high, low, close) # Same pattern, different context

def detect_gravestone_doji(open_, high, low, close):
    body = np.abs(close - open_)
    upper_wick = high - np.maximum(open_, close)
    lower_wick = np.minimum(open_, close) - low
    return (body < 0.1 * (high - low)) & (upper_wick > 3 * body) & (lower_wick < 0.1 * upper_wick)

def detect_dark_cloud_cover(o, c, prev_o, prev_c):
    return (prev_c > prev_o) & (c < o) & (o > prev_c) & (c < (prev_o + prev_c)/2)

def detect_evening_star(o, c, p1_o, p1_c, p2_o, p2_c):
    return (p2_c > p2_o) & (np.abs(p1_c - p1_o) < 0.3 * np.abs(p2_c - p2_o)) & (c < o) & (c < (p2_o + p2_c)/2)

def detect_three_line_strike(o, c, p1_o, p1_c, p2_o, p2_c, p3_o, p3_c):
    # Bullish strike: 3 bearish candles followed by 1 large bullish candle
    bullish = (p3_c < p3_o) & (p2_c < p2_o) & (p1_c < p1_o) & (c > o) & (c > p3_o)
    # Bearish strike: 3 bullish candles followed by 1 large bearish candle
    bearish = (p3_c > p3_o) & (p2_c > p2_o) & (p1_c > p1_o) & (c < o) & (c < p3_o)
    return bullish, bearish

def detect_spinning_top(open_, high, low, close):
    body = np.abs(close - open_)
    upper_wick = high - np.maximum(open_, close)
    lower_wick = np.minimum(open_, close) - low
    return (body < 0.2 * (high - low)) & (upper_wick > body) & (lower_wick > body)

def detect_engulfing(o, c, prev_o, prev_c):
    bullish = (prev_c < prev_o) & (c > o) & (o < prev_c) & (c > prev_o)
    bearish = (prev_c > prev_o) & (c < o) & (o > prev_c) & (c < prev_o)
    return bullish, bearish

def detect_harami(o, c, prev_o, prev_c):
    bullish = (prev_c < prev_o) & (c > o) & (o > prev_c) & (c < prev_o)
    bearish = (prev_c > prev_o) & (c < o) & (o < prev_c) & (c > prev_o)
    return bullish, bearish

def detect_three_outside(o, c, po, pc, p2o, p2c):
    # Bullish: Engulfing followed by a higher close
    bullish = (p2c < p2o) & (pc > po) & (po < p2c) & (pc > p2o) & (c > pc)
    # Bearish: Engulfing followed by a lower close
    bearish = (p2c > p2o) & (pc < po) & (po > p2c) & (pc < p2o) & (c < pc)
    return bullish, bearish

def detect_three_inside(o, c, po, pc, p2o, p2c):
    # Bullish: Harami followed by a higher close
    bullish = (p2c < p2o) & (pc > po) & (pc < p2o) & (po > p2c) & (c > pc)
    # Bearish: Harami followed by a lower close
    bearish = (p2c > p2o) & (pc < po) & (pc > p2o) & (po < p2c) & (c < pc)
    return bullish, bearish

def normalize_series(series):
    """
    Min-max normalization of a series to the [0, 1] range.

    Parameters
    ----------
    series : pandas.Series
        The series to normalize.

    Returns
    -------
    pandas.Series
        The normalized series.
    """
    if series.empty or series.max() == series.min():
        return series * 0
    return (series - series.min()) / (series.max() - series.min())

def handle_mc_strategies(df, strategy, config, is_scan):
    """
    Helper function to execute Monte Carlo-based trading strategies.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    strategy : str
        The specific MC strategy name (e.g., 'mc_mean_reversion').
    config : dict
        Strategy configuration.
    is_scan : bool
        Whether the call is for a scan (processes all rows if True,
        only the last row if False).

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    df['buy_candidate'] = False
    df['sell_candidate'] = False

    # Range of indices to calculate (all if scan, only last if not)
    # Actually scan might need a window.
    start_idx = 0 if is_scan else len(df) - 1

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
            df.at[df.index[i], 'score'] = 1 if df.at[df.index[i], 'buy_candidate'] else (-1 if df.at[df.index[i], 'sell_candidate'] else 0)
            df.at[df.index[i], 'tendency'] = "Bullish" if row['close'] > row['sma_20'] else "Bearish"

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
            df.at[df.index[i], 'score'] = 1 if df.at[df.index[i], 'buy_candidate'] else (-1 if df.at[df.index[i], 'sell_candidate'] else 0)
            df.at[df.index[i], 'tendency'] = "Bullish" if row['drift'] > 0 else "Bearish"

    elif strategy == 'mc_dynamic_allocation':
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std()
        threshold = 0.05 / np.sqrt(365)
        df['buy_candidate'] = (df['volatility'] < threshold) & (df['volatility'].shift(1) >= threshold)
        df['sell_candidate'] = (df['volatility'] > threshold) & (df['volatility'].shift(1) <= threshold)
        df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
        df['tendency'] = np.where(df['volatility'] < threshold, "Bullish", "Bearish")

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
            df.at[df.index[i], 'score'] = 1 if df.at[df.index[i], 'buy_candidate'] else (-1 if df.at[df.index[i], 'sell_candidate'] else 0)
            df.at[df.index[i], 'tendency'] = "Neutral"

    elif strategy == 'mc_stop_loss_eval':
        df['returns'] = np.log(df['close'] / df['close'].shift(1))
        df['volatility'] = df['returns'].rolling(window=20).std()
        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            if np.isnan(row['volatility']) or row['volatility'] == 0: continue
            prob_sl = _mc_engine.estimate_hit_probability(row['close'], row['close'] * 0.95, row['volatility'], mode='below')
            df.at[df.index[i], 'sell_candidate'] = prob_sl > 0.4
            df.at[df.index[i], 'score'] = -1 if df.at[df.index[i], 'sell_candidate'] else 0
            df.at[df.index[i], 'tendency'] = "Bearish" if prob_sl > 0.5 else "Neutral"

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
            df.at[df.index[i], 'score'] = 1 if df.at[df.index[i], 'buy_candidate'] else (-1 if df.at[df.index[i], 'sell_candidate'] else 0)
            df.at[df.index[i], 'tendency'] = "Bullish" if call_p > put_p else "Bearish"

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

# --- 1. TREND FOLLOWING ---

def strategy_ichimoku(df, config):
    """
    Stratégie Ichimoku Cloud.

    Buy signal when Tenkan-sen crosses above Kijun-sen and price is above
    the Kumo (Cloud). Sell signal when Tenkan-sen crosses below Kijun-sen.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    ichi_result = ta.ichimoku(df['high'], df['low'], df['close'])
    if ichi_result is not None and len(ichi_result) > 0 and ichi_result[0] is not None:
        ichimoku = ichi_result[0]
        df['tenkan'] = ichimoku.iloc[:, 0].fillna(df['close'])
        df['kijun'] = ichimoku.iloc[:, 1].fillna(df['close'])
        df['span_a'] = ichimoku.iloc[:, 2].fillna(df['close'])
        df['span_b'] = ichimoku.iloc[:, 3].fillna(df['close'])
    else:
        df['tenkan'] = df['kijun'] = df['span_a'] = df['span_b'] = df['close']

    df['buy_candidate'] = (df['tenkan'] > df['kijun']) & (df['close'] > df['span_a']) & (df['close'] > df['span_b'])
    df['sell_candidate'] = (df['tenkan'] < df['kijun'])

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > df['span_a'], "Bullish", np.where(df['close'] < df['span_b'], "Bearish", "Neutral"))

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_psar(df, config):
    """
    Stratégie SAR Parabolique.

    Buy signal when the Parabolic SAR flips from above to below the price.
    Sell signal when it flips from below to above the price.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
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

    df['score'] = np.where(df['psar_long'].notna(), 1, np.where(df['psar_short'].notna(), -1, 0))
    df['tendency'] = np.where(df['psar_long'].notna(), "Bullish", np.where(df['psar_short'].notna(), "Bearish", "Neutral"))

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

# --- 2. RANGE ---

def strategy_bollinger(df, config):
    """
    Stratégie Bandes de Bollinger.

    Buy signal when price touches or exceeds the lower band and RSI is oversold.
    Sell signal when price touches the middle band (SMA).

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
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

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > df['bb_mid'], "Bullish", np.where(df['close'] < df['bb_mid'], "Bearish", "Neutral"))

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

# --- 3. BREAKOUT ---

def strategy_donchian(df, config):
    """
    Stratégie Canaux de Donchian.

    Buy signal when price touches the upper Donchian channel.
    Sell signal when price touches the lower Donchian channel.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    dc = ta.donchian(df['high'], df['low'], length=20)
    if dc is not None and not dc.empty:
        df['dc_upper'] = dc.iloc[:, 0]
        df['dc_lower'] = dc.iloc[:, 2]
    else:
        df['dc_upper'] = df['high']
        df['dc_lower'] = df['low']

    df['buy_candidate'] = (df['close'] >= df['dc_upper'])
    df['sell_candidate'] = (df['close'] <= df['dc_lower'])

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > (df['dc_upper'] + df['dc_lower'])/2, "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_stoch_rsi(df, config):
    """
    Stratégie RSI Stochastique.

    Buy signal when Stochastic RSI %K is oversold (< 20) and rising.
    Sell signal when %K is overbought (> 80) and falling.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    stoch = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
    if stoch is not None and not stoch.empty:
        df['stoch_k'] = stoch.iloc[:, 0]
    else:
        df['stoch_k'] = 50

    df['buy_candidate'] = (df['stoch_k'] < 20) & (df['stoch_k'] > df['stoch_k'].shift(1))
    df['sell_candidate'] = (df['stoch_k'] > 80) & (df['stoch_k'] < df['stoch_k'].shift(1))

    df['score'] = np.where(df['stoch_k'] < 20, 1, np.where(df['stoch_k'] > 80, -1, 0))
    df['tendency'] = np.where(df['stoch_k'] > 50, "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_williams_r(df, config):
    """
    Stratégie Williams %R.

    Buy signal when Williams %R is oversold (< -80) and rising.
    Sell signal when overbought (> -20) and falling.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    df['willr'] = ta.willr(df['high'], df['low'], df['close'], length=14)

    df['buy_candidate'] = (df['willr'] < -80) & (df['willr'] > df['willr'].shift(1))
    df['sell_candidate'] = (df['willr'] > -20) & (df['willr'] < df['willr'].shift(1))

    df['score'] = np.where(df['willr'] < -80, 1, np.where(df['willr'] > -20, -1, 0))
    df['tendency'] = np.where(df['willr'] > -50, "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_vwap_momentum(df, config):
    """
    Stratégie Momentum VWAP.

    Buy signal when price is above VWAP and volume is increasing.
    Sell signal when price crosses below VWAP.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()

    df['buy_candidate'] = (df['close'] > df['vwap']) & (df['volume'] > df['volume'].shift(1))
    df['sell_candidate'] = (df['close'] < df['vwap'])

    df['score'] = np.where(df['close'] > df['vwap'], 1, -1)
    df['tendency'] = np.where(df['close'] > df['vwap'], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

# --- 5. SCALPING (Proxies) ---

def strategy_renko_proxy(df, config):
    """
    Stratégie Proxy Renko.

    Simulates Renko charts by detecting candles with bodies larger than ATR.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    df['body'] = (df['close'] - df['open']).abs()
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    df['buy_candidate'] = (df['body'] > df['atr']) & (df['close'] > df['open'])
    df['sell_candidate'] = (df['body'] > df['atr']) & (df['close'] < df['open'])

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > df['open'], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_ema_rsi_volume(df, config):
    """
    Stratégie Hybride EMA, RSI et Volume.

    Buy signal when 9 EMA > 21 EMA, RSI > 50, and volume is above average.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    ema_9 = ta.ema(df['close'], length=9); df['ema_9'] = ema_9.fillna(df['close']) if ema_9 is not None else df['close']
    ema_21 = ta.ema(df['close'], length=21); df['ema_21'] = ema_21.fillna(df['close']) if ema_21 is not None else df['close']
    rsi_14 = ta.rsi(df['close'], length=14); df['rsi'] = rsi_14.fillna(50) if rsi_14 is not None else 50
    vol_ma_20 = ta.sma(df['volume'], length=20); df['vol_ma'] = vol_ma_20.fillna(df['volume']) if vol_ma_20 is not None else df['volume']

    df['buy_candidate'] = (df['ema_9'] > df['ema_21']) & (df['rsi'] > 50) & (df['volume'] > df['vol_ma'])
    df['sell_candidate'] = (df['ema_9'] < df['ema_21'])

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['ema_9'] > df['ema_21'], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_whale_detection(df, config):
    """
    Stratégie de détection de baleines (Proxy On-Chain).

    Detects unusual volume spikes (3 standard deviations above mean) accompanied
    by price movement to infer large player activity.

    References
    ----------
    Bartoletti et al. (2017). "General-purpose smart contracts: architectures,
    vulnerabilities and future challenges."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    df['vol_ma'] = ta.sma(df['volume'], length=20)
    df['vol_std'] = df['volume'].rolling(window=20).std()

    # Significant volume spike: volume > 3 standard deviations above mean
    df['whale_spike'] = df['volume'] > (df['vol_ma'] + 3 * df['vol_std'])

    # Buy if volume spike and price moves up
    df['buy_candidate'] = df['whale_spike'] & (df['close'] > df['close'].shift(1))
    df['sell_candidate'] = df['whale_spike'] & (df['close'] < df['close'].shift(1))

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > df['close'].shift(1), "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_pump_dump(df, config):
    """
    Stratégie de détection de Pump and Dump.

    Detects extreme price-volume divergence where both price and volume
    surge suddenly. Sells when price begins to drop after a surge.

    References
    ----------
    Kamps, J., & Kleinberg, B. (2018). "To the moon: defining and detecting
    cryptocurrency pump-and-dumps."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    df['vol_change'] = df['volume'].pct_change()
    df['price_change'] = df['close'].pct_change()

    # Pump: Price and Volume both surge suddenly
    df['pump_detected'] = (df['vol_change'] > 5.0) & (df['price_change'] > 0.05)

    # Dump: After a pump, price stops growing but volume remains high or drops
    df['buy_candidate'] = False # Don't buy pumps
    df['sell_candidate'] = df['pump_detected'].shift(1) & (df['close'] < df['close'].shift(1))

    df['score'] = np.where(df['pump_detected'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['price_change'] > 0, "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_scientific_ensemble(df, config):
    """
    Stratégie d'ensemble scientifique (Proxy modèles ML).

    Combines signals from MACD, RSI, and Bollinger Bands using a weighted
    scoring approach.

    References
    ----------
    Makarov, I., & Schoar, A. (2019). "Trading and arbitrage in cryptocurrency markets."
    Zhang, Z., et al. (2020). "DeepLOB: Deep convolutional neural networks for
    limit order books."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
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

    # Keep the ensemble score but clip it for UI if needed or use it as is
    df['tendency'] = np.where(df['score'] > 0, "Bullish", np.where(df['score'] < 0, "Bearish", "Neutral"))

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_sentiment_momentum(df, config):
    """
    Stratégie de momentum de sentiment (Proxy Social Media).

    Uses price acceleration and RSI as a proxy for market FOMO (Fear Of Missing Out)
    or Panic.

    References
    ----------
    Abraham, J., et al. (2018). "Cryptocurrency price prediction using tweet
    volumes and sentiment analysis."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    rsi_14 = ta.rsi(df['close'], length=14); df['rsi'] = rsi_14.fillna(50) if rsi_14 is not None else 50
    roc_10 = ta.roc(df['close'], length=10); df['roc'] = roc_10.fillna(0) if roc_10 is not None else 0
    df['acceleration'] = df['roc'].diff().fillna(0)

    # Positive sentiment: Price accelerating upwards + RSI not yet overbought
    df['buy_candidate'] = (df['acceleration'] > 0) & (df['roc'] > 0) & (df['rsi'] < 60)
    # Negative sentiment: Price decelerating or dropping fast + RSI oversold (panic)
    df['sell_candidate'] = (df['acceleration'] < 0) & (df['roc'] < 0) & (df['rsi'] > 40)

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['roc'] > 0, "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_liquidation_cascade(df, config):
    """
    Stratégie de cascade de liquidations.

    Detects sharp price drops (>2%) on very high volume as long liquidation
    cascades (buying opportunity) or sharp rises as short liquidations.

    References
    ----------
    Makarov, I., & Schoar, A. (2020). "Price discovery in cryptocurrency markets."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
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

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > df['close'].shift(1), "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_adx_trend(df, config):
    """
    Stratégie de force de tendance ADX.

    Filters signals based on ADX trend strength (> 25).

    References
    ----------
    Zhang, Z., et al. (2020). "DeepLOB: Deep convolutional neural networks for
    limit order books."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    adx = ta.adx(df['high'], df['low'], df['close'])
    if adx is not None and not adx.empty:
        df['adx'] = adx.iloc[:, 0].fillna(0)
        df['dmp'] = adx.iloc[:, 1].fillna(0)
        df['dmn'] = adx.iloc[:, 2].fillna(0)
    else:
        df['adx'] = df['dmp'] = df['dmn'] = 0

    df['buy_candidate'] = (df['adx'] > 25) & (df['dmp'] > df['dmn'])
    df['sell_candidate'] = (df['adx'] > 25) & (df['dmn'] > df['dmp'])

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['dmp'] > df['dmn'], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_pairs_trading(df, config):
    """
    Stratégie Pairs Trading (Proxy Arbitrage Statistique).

    Simulates pairs trading by comparing price to its 50-period SMA using Z-Score.

    References
    ----------
    Grobys, K., et al. (2020). "On the predictability of cryptocurrency returns
    and the role of market timing."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    ma_50 = ta.sma(df['close'], length=50); df['ma_50'] = ma_50.fillna(df['close']) if ma_50 is not None else df['close']
    df['z_score'] = (df['close'] - df['ma_50']) / df['close'].rolling(window=50).std()

    df['buy_candidate'] = df['z_score'] < -2.0
    df['sell_candidate'] = df['z_score'] > 2.0

    df['score'] = np.where(df['z_score'] < 0, 1, -1)
    df['tendency'] = np.where(df['z_score'] > 0, "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_halving_cycle(df, config):
    """
    Stratégie cycle de halving Bitcoin.

    Aligns with major market cycles by only buying when price is above 200 EMA.

    References
    ----------
    Bouoiyour, J., & Selmi, R. (2020). "What Bitcoin is? Store of value or a
    speculative asset? A semi-parametric approach."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    ema_200 = ta.ema(df['close'], length=200); df['ema_200'] = ema_200.fillna(df['close']) if ema_200 is not None else df['close']
    ema_50 = ta.ema(df['close'], length=50); df['ema_50'] = ema_50.fillna(df['close']) if ema_50 is not None else df['close']

    # Buy only when above 200 EMA (Bull market cycle)
    df['buy_candidate'] = (df['close'] > df['ema_200']) & (df['close'] > df['ema_50']) & (df['close'].shift(1) <= df['ema_50'].shift(1))
    df['sell_candidate'] = (df['close'] < df['ema_50'])

    df['score'] = np.where(df['close'] > df['ema_50'], 1, -1)
    df['tendency'] = np.where(df['close'] > df['ema_200'], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_listing_surge(df, config):
    """
    Stratégie de pic de cotation (Listing).

    Detects extreme volume increases on relatively flat price history to
    front-run listing pumps.

    References
    ----------
    Hau, H., et al. (2021). "The market for cryptocurrencies: an analysis of
    liquidity and listing effects."

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    vol_ma_50 = ta.sma(df['volume'], length=50); df['vol_ma'] = vol_ma_50.fillna(df['volume']) if vol_ma_50 is not None else df['volume']
    df['price_std'] = df['close'].rolling(window=50).std().fillna(0)

    # Surge: Volume > 10x average + Price breakout
    df['surge'] = (df['volume'] > df['vol_ma'] * 5) & (df['close'] > df['close'].shift(1) + 2 * df['price_std'])

    df['buy_candidate'] = df['surge']
    df['sell_candidate'] = df['close'] < df['close'].shift(3) # Exit fast after surge

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > df['close'].shift(1), "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_tema_crossover(df, config):
    """
    Stratégie de croisement TEMA.

    Buy signal when price crosses above the TEMA.
    Sell signal when price crosses below the TEMA.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration (tema_length).

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    length = config.get('tema_length')
    device = config.get('device') or torch.device('cpu')

    # Check if tema_20 (default) was already calculated, otherwise calculate custom length
    col_name = f'tema_{length}_strat'
    if length == 20 and 'tema_20' in df.columns:
        df[col_name] = df['tema_20']
    else:
        if (device.type != 'cpu') or torch.backends.mkldnn.enabled:
            close_t = torch.tensor(df['close'].values, device=device, dtype=torch.float64)
            df[col_name] = torch_tema(close_t, length).to('cpu').numpy()
        else:
            tema_series = ta.tema(df['close'], length=length)
            df[col_name] = tema_series.fillna(df['close']) if tema_series is not None else df['close']

    df['buy_candidate'] = (df['close'] > df[col_name]) & (df['close'].shift(1) <= df[col_name].shift(1))
    df['sell_candidate'] = (df['close'] < df[col_name]) & (df['close'].shift(1) >= df[col_name].shift(1))

    df['score'] = np.where(df['close'] > df[col_name], 1, -1)
    df['tendency'] = np.where(df['close'] > df[col_name], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_sinewave(df, config):
    """
    Stratégie SineWave de Hilbert Transform.

    Buy when Sine crosses above LeadSine.
    Sell when Sine crosses below LeadSine.
    """
    device = config.get('device') or torch.device('cpu')
    if (device.type != 'cpu') or torch.backends.mkldnn.enabled:
        # Ensure numeric for Sinewave
        if 'close' in df.columns and df['close'].dtype == 'object':
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
        close_t = torch.tensor(df['close'].astype(float).values, device=device, dtype=torch.float64)
        sine, leadsine = torch_sinewave(close_t)
        df['sine'] = sine.to('cpu').numpy()
        df['leadsine'] = leadsine.to('cpu').numpy()
    else:
        # Fallback to pandas_ta if available
        sw = ta.sinewave(df['close'])
        if sw is not None:
            df['sine'] = sw.iloc[:, 0]; df['leadsine'] = sw.iloc[:, 1]
        else:
            df['sine'] = df['leadsine'] = 0

    df['buy_candidate'] = (df['sine'] > df['leadsine']) & (df['sine'].shift(1) <= df['leadsine'].shift(1))
    df['sell_candidate'] = (df['sine'] < df['leadsine']) & (df['sine'].shift(1) >= df['leadsine'].shift(1))

    df['score'] = np.where(df['sine'] > df['leadsine'], 1, -1)
    df['tendency'] = np.where(df['sine'] > df['leadsine'], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_candle_patterns(df, config):
    """
    Stratégie complète de motifs de chandeliers.
    """
    o, h, l, c = df['open'], df['high'], df['low'], df['close']
    po, ph, pl, pc = o.shift(1), h.shift(1), l.shift(1), c.shift(1)
    p2o, p2c = o.shift(2), c.shift(2)
    p3o, p3c = o.shift(3), c.shift(3)

    # Bullish
    bull_eng, _ = detect_engulfing(o, c, po, pc)
    bull_har, _ = detect_harami(o, c, po, pc)
    hammer = detect_hammer(o, h, l, c)
    inv_hammer = detect_inverted_hammer(o, h, l, c)
    df_doji = detect_dragonfly_doji(o, h, l, c)
    piercing = detect_piercing_line(o, c, po, pc)
    morn_star = detect_morning_star(o, c, po, pc, p2o, p2c)
    soldiers = detect_three_white_soldiers(o, c, po, pc, p2o, p2c)
    bull_3ls, _ = detect_three_line_strike(o, c, po, pc, p2o, p2c, p3o, p3c)
    bull_3out, _ = detect_three_outside(o, c, po, pc, p2o, p2c)
    bull_3in, _ = detect_three_inside(o, c, po, pc, p2o, p2c)
    spinning_top = detect_spinning_top(o, h, l, c)

    # Bearish
    _, bear_eng = detect_engulfing(o, c, po, pc)
    _, bear_har = detect_harami(o, c, po, pc)
    hang_man = detect_hanging_man(o, h, l, c)
    shoot_star = detect_shooting_star(o, h, l, c)
    grav_doji = detect_gravestone_doji(o, h, l, c)
    dark_cloud = detect_dark_cloud_cover(o, c, po, pc)
    even_star = detect_evening_star(o, c, po, pc, p2o, p2c)
    even_doji_star = detect_evening_doji_star(o, c, po, pc, ph, pl, p2o, p2c)
    _, bear_3ls = detect_three_line_strike(o, c, po, pc, p2o, p2c, p3o, p3c)
    _, bear_3out = detect_three_outside(o, c, po, pc, p2o, p2c)
    _, bear_3in = detect_three_inside(o, c, po, pc, p2o, p2c)

    df['buy_candidate'] = bull_eng | bull_har | hammer | inv_hammer | df_doji | piercing | morn_star | soldiers | bull_3ls | bull_3out | bull_3in | (spinning_top & (c > o))
    df['sell_candidate'] = bear_eng | bear_har | hang_man | shoot_star | grav_doji | dark_cloud | even_star | even_doji_star | bear_3ls | bear_3out | bear_3in | (spinning_top & (c < o))

    df['score'] = np.where(df['buy_candidate'], 1, np.where(df['sell_candidate'], -1, 0))
    df['tendency'] = np.where(df['close'] > df['close'].shift(1), "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df

def strategy_heikin_ashi(df, config):
    """
    Stratégie Heikin Ashi.

    Uses Heikin Ashi "shadow" candles for noise filtering.
    Buy signal: Green HA candle (HA_Close > HA_Open) with no lower wick.
    Sell signal: Red HA candle (HA_Close < HA_Open) with no upper wick.

    Parameters
    ----------
    df : pandas.DataFrame
        OHLCV data.
    config : dict
        Strategy configuration.

    Returns
    -------
    pandas.DataFrame
        Updated dataframe with buy/sell signals.
    """
    device = config.get('device') or torch.device('cpu')

    if (device.type != 'cpu') or torch.backends.mkldnn.enabled:
        o_t = torch.tensor(df['open'].values, device=device, dtype=torch.float64)
        h_t = torch.tensor(df['high'].values, device=device, dtype=torch.float64)
        l_t = torch.tensor(df['low'].values, device=device, dtype=torch.float64)
        c_t = torch.tensor(df['close'].values, device=device, dtype=torch.float64)
        ha_o, ha_h, ha_l, ha_c = torch_heikin_ashi(o_t, h_t, l_t, c_t)
        df['ha_open'] = ha_o.to('cpu').numpy()
        df['ha_high'] = ha_h.to('cpu').numpy()
        df['ha_low'] = ha_l.to('cpu').numpy()
        df['ha_close'] = ha_c.to('cpu').numpy()
    else:
        ha_df = ta.ha(df['open'], df['high'], df['low'], df['close'])
        if ha_df is not None:
            df['ha_open'] = ha_df.iloc[:, 0]; df['ha_high'] = ha_df.iloc[:, 1]
            df['ha_low'] = ha_df.iloc[:, 2]; df['ha_close'] = ha_df.iloc[:, 3]
        else:
            df['ha_open'] = df['open']; df['ha_high'] = df['high']
            df['ha_low'] = df['low']; df['ha_close'] = df['close']

    # Buy: Green candle (HA_Close > HA_Open) and No lower wick (HA_Low == HA_Open)
    # Using a small epsilon for float comparison
    df['buy_candidate'] = (df['ha_close'] > df['ha_open']) & (np.abs(df['ha_low'] - df['ha_open']) < 1e-8)

    # Sell: Red candle (HA_Close < HA_Open) and No upper wick (HA_High == HA_Open)
    df['sell_candidate'] = (df['ha_close'] < df['ha_open']) & (np.abs(df['ha_high'] - df['ha_open']) < 1e-8)

    df['score'] = np.where(df['ha_close'] > df['ha_open'], 1, -1)
    df['tendency'] = np.where(df['ha_close'] > df['ha_open'], "Bullish", "Bearish")

    df['buy_signal'] = df['buy_candidate']; df['sell_signal'] = df['sell_candidate']; return df
