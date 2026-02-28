#!/usr/bin/env bash
set -euo pipefail

echo "=== Oracle3: AI-Native Prediction Market Agent on Solana ==="
echo ""
echo "--- Step 1: List DFlow/Solana markets ---"
oracle3 market list --exchange solana --limit 5
echo ""
echo "--- Step 2: Paper trade with Solana agent strategy (2 min) ---"
oracle3 paper run \
  --exchange solana \
  --strategy-ref oracle3.strategy.contrib.solana_agent_strategy:SolanaAgentStrategy \
  --monitor \
  --duration 120
echo ""
echo "--- Step 3: Show on-chain trade log ---"
oracle3 trade-log --limit 10
echo ""
echo "=== Demo complete! ==="
