"""Experimental prototypes — NOT production code.

Modules in this package are skeletons kept for design exploration. They are
**not** wired into the trading path and must not be relied on for real trading:

- ``flash_loan``: a flash-loan arbitrage sketch whose on-chain transaction
  currently contains only a Memo instruction, not real borrow/buy/sell/repay
  CPI instructions. It does not execute arbitrage and does not book profit.

Importing anything here signals that you are opting into unfinished code.
"""
