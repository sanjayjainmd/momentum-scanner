# Momentum Scanner

A rules-based daily stock screener that looks for swing-trade setups in strong, trending stocks and emails a ranked shortlist each weekday morning.

## How it works

Each run passes a stock universe through a layered filter. A stock has to clear every layer to make the list:

| Layer | Check |
|---|---|
| 0 | **Market regime**: SPY and QQQ above their 50-day averages, or the whole scan stops |
| 0b | **Volatility gate**: no breakouts when VIX > 25, no trades when VIX > 35 |
| 0c | **Earnings blackout**: skip stocks reporting within a few days |
| 1 | **Trend**: price > 50-day > 200-day average, rising 20-day EMA |
| 1c | **Relative strength**: ranks in the top part of the universe vs. SPY |
| 2 | **Setup**: either a quiet pullback to the 50-day or a high-volume breakout |
| 3 | **Confirmation**: volume and ATR checks |
| 4 | **Options flow**: 30–90 day call/put ratio |
| 5 | **Risk/reward**: at least 2:1, with position size and expected value |

Results go out as an HTML email. An optional approval server lets each idea be approved or rejected from the email before any order is placed with a broker (Alpaca or Interactive Brokers).

## Files

- `runner.py`: entry point that runs the full pipeline
- `scanner.py`: the filter layers
- `universe_builder.py`, `data_fetcher.py`: build the stock list and pull data (yfinance)
- `backtest.py`: tests the rules on historical data
- `email_reporter.py`: formats and sends the email
- `approval_server.py`, `alpaca_client.py`, `ibkr_client.py`: optional trade approval and execution
- `config.py`: every threshold in one place

## Running it

```bash
pip install -r requirements.txt
python runner.py
```

Credentials (broker keys, Gmail app password) are read from environment variables. A GitHub Actions workflow runs the scan at 8 AM ET on weekdays.

*Personal research tool. Not investment advice.*
