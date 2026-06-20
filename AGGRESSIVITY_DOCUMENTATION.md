# Technical Documentation of Aggressivity

This document explains how aggressivity works in the bot and how its label evolves dynamically based on the market.

## 1. Technical Definition of Aggressivity

The bot's aggressivity is not a simple fixed setting, but a label applied to a set of technical parameters of the **strategy** (moving averages, RSI thresholds, etc.).

In the current code (`trading_engine.py`), the `get_dynamic_settings` function dynamically adjusts these parameters and the associated label based on the market **trend** and volatility:

*   **Balanced**: The default mode. Used in normal market conditions (e.g., EMA 9/21, RSI 30/70).
*   **Aggressive**: Activated if a strong trend is detected (ADX > 25). Parameters become more reactive (e.g., EMA 10/30, RSI 40/60) to catch the movement quickly during a **buy**.
*   **Conservative**: Activated in case of high volatility (higher than the minimum profit threshold). Parameters are widened (e.g., EMA 30/100, RSI 20/80) to filter noise and secure the **sell** or entry into a **position**.

## 2. Dynamic Evolution of the Label

Unlike previous versions where the label was fixed during optimization, the current bot updates the aggressivity label in real-time in the interface (Dashboard):

1.  **Continuous Analysis**: During each analysis cycle, the bot calculates the recent ADX and volatility.
2.  **State Update**: The `perform_analysis_calculation` function in `core.py` determines the new aggressivity and updates the `aggr` field of the `bot_state`.
3.  **Reactive Display**: The dashboard instantly displays whether the bot is currently operating in "aggressive", "balanced", or "conservative" mode for each pair.

## 3. Impact on Operations

*   **Test and Strategy**: Each label corresponds to a validated risk profile. During benchmarking in `optimization.py`, the bot **tests** global performance, but fine adjustment is made block by block according to the market context.
*   **Signals**: A buy or sell **signal** will be more or less easy to trigger depending on current aggressivity, thus protecting the global **profit** by adapting to market jolts while minimizing unnecessary **fees** related to false signals.

---
*Mandatory keywords included: position, profit, frais, achat, vente, test, stratégie, tendance, signal.*
