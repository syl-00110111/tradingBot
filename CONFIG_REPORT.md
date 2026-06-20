# Configuration Parameters Report

This report explains the configuration parameters found in `config.default.json` for the Cryptocurrencies multiplatform trading bot, their influence on bot behavior, and a hypothetical trade example.

## Parameter Explanations

### Core Trading Settings

*   **`max_open_positions`** (Default: `10`)
    *   **Description**: Limits the maximum number of concurrent open trades across all pairs.
    *   **Influence**: Prevents the bot from over-extending its capital and helps manage risk by capping the total number of active positions.

*   **`base_bet`** (Default: `"10%"`)
    *   **Description**: The base amount to risk per trade, expressed as a percentage of the available quote asset balance (e.g., USDT, USDC).
    *   **Influence**: Determines the initial size of a position. A value of `"10%"` means the bot will use 10% of your available balance for each new trade.

*   **`global_risk_multiplier`** (Default: `1.2`)
    *   **Description**: A scaling factor applied to the base trade amount.
    *   **Influence**: Linearly increases or decreases the calculated position size. If `base_bet` is 100 USDT and `global_risk_multiplier` is 1.2, the actual target trade amount becomes 120 USDT.

### Profit Thresholds (`profit_thresholds`)

    *   **Description**: The minimum profit a pattern must generate during the benchmarking phase to be considered a "Success Pattern" (SPM).
    *   **Influence**: Filters out low-performing signals during historical analysis. Only strategies that yield at least this much profit in the test window are saved.

*   **`no_patterns_msg_threshold`** (Default: `0.01`)
    *   **Description**: A fallback absolute profit threshold used to display a warning if no profitable patterns are found.
    *   **Influence**: Only affects UI feedback. If the best found pattern's profit is below this (and the dynamic % threshold), the bot informs the user that no high-quality patterns were found.

*   **`no_patterns_msg_threshold_pct`** (Default: `0.005` / 0.5%)
    *   **Description**: The percentage of the total balance used to calculate a dynamic threshold for the "no patterns" warning.
    *   **Influence**: Ensures the UI warning is relevant to the user's account size.

*   **`bench_avg_threshold`** (Default: `0.05` / 5.0%)
    *   **Description**: A threshold used during benchmarking to identify "winning" patterns for calculating an average benchmark profit.
    *   **Influence**: It helps the bot calculate a more realistic "average" expectation by focusing on patterns that met this specific profit hurdle.

*   **`mc_validation_hurdle`** (Default: `0.0015` / 0.15%)
    *   **Description**: The minimum "profit probability" improvement required for a Monte Carlo simulation to validate a strategy.
    *   **Influence**: Used in `analyze_pair` to decide if an expired or regime-shifted pattern can still be reused. It adds a layer of statistical validation before triggering a re-benchmark.

### Win Streak Bonus (`win_streak_bonus`)

*   **`enabled`** (Default: `true`)
    *   **Description**: Toggles the win streak multiplier feature.
*   **`threshold`** (Default: `2`)
    *   **Description**: The number of consecutive profitable trades required for a specific symbol to trigger the bonus.
*   **`multiplier`** (Default: `1.3`)
    *   **Description**: The factor by which the position size is multiplied when the threshold is met.
    *   **Influence**: Rewards successful performance by increasing exposure on "hot" pairs.


*   Defines three profiles: **Short**, **Medium**, and **Long**.
*   **`duration_hours`**: The historical window looked at for benchmarking.
*   **`timeframe`**: The candle interval used (e.g., `"1m"`, `"15m"`, `"1h"`).
*   **`eval_candles`**: The number of candles used to define the length of a "success pattern".

---

## Trade Example (Hypothetical)

**Scenario Setup:**
*   **Available Balance**: 1,000 USDT
*   **Current BTC/USDT Price**: 50,000 USDT
*   **Win Streak for BTC/USDT**: 2 (Threshold met!)
*   **Exchange Fee**: 0.1%

**Configuration:**
*   `base_bet`: `"10%"`
*   `global_risk_multiplier`: `1.2`
*   `win_streak_bonus.multiplier`: `1.3`

**Calculations:**

1.  **Calculate Base Trade Amount**:
    `1,000 USDT (Balance) * 10% (Base Bet) = 100 USDT`

2.  **Apply Global Risk Multiplier**:
    `100 USDT * 1.2 = 120 USDT`

3.  **Apply Win Streak Bonus**:
    Since the win streak is 2 (matching the threshold), the multiplier is applied:
    `120 USDT * 1.3 = 156 USDT`

4.  **Final Position Size (in BTC)**:
    `156 USDT / 50,000 USDT (Price) = 0.00312 BTC`

5.  **Execution**:
    The bot will attempt to BUY **0.00312 BTC**.
    The cost will be **156 USDT** (+ fees).

    If a pattern is detected, the bot expects a minimum profit of **1.5%** (1.5% of 156 USDT = 2.34 USDT) based on historical performance before it considers the entry high-quality.
