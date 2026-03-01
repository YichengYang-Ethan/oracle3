#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  Oracle3 — Hackathon Demo Script
#  AI-Native Prediction Market Agent on Solana
# ============================================================

# Colors
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_DIM='\033[2m'
C_BLUE='\033[38;5;75m'
C_PURPLE='\033[38;5;141m'
C_GREEN='\033[38;5;84m'
C_YELLOW='\033[38;5;221m'
C_RED='\033[38;5;210m'
C_CYAN='\033[38;5;87m'
C_BG='\033[48;5;236m'

DURATION="${1:-180}"

banner() {
  echo ""
  echo -e "${C_BLUE}${C_BOLD}"
  echo "   ___                 _      _____ "
  echo "  / _ \ _ __ __ _  ___| | ___|___ / "
  echo " | | | | '__/ _\` |/ __| |/ _ \ |_ \\ "
  echo " | |_| | | | (_| | (__| |  __/___) |"
  echo "  \___/|_|  \__,_|\___|_|\___|____/ "
  echo -e "${C_RESET}"
  echo -e "${C_PURPLE}${C_BOLD}  AI-Native Prediction Market Agent on Solana${C_RESET}"
  echo -e "${C_DIM}  ──────────────────────────────────────────────${C_RESET}"
  echo ""
}

step() {
  local num=$1
  shift
  echo ""
  echo -e "  ${C_CYAN}${C_BOLD}[$num]${C_RESET} ${C_BOLD}$*${C_RESET}"
  echo -e "  ${C_DIM}$(printf '%.0s─' {1..50})${C_RESET}"
  echo ""
}

info() {
  echo -e "  ${C_DIM}$*${C_RESET}"
}

success() {
  echo -e "  ${C_GREEN}$*${C_RESET}"
}

wait_key() {
  echo ""
  echo -e "  ${C_YELLOW}Press ENTER to continue...${C_RESET}"
  read -r
}

# ──────────────────────────────────────────────
banner

echo -e "  ${C_BOLD}Demo Overview:${C_RESET}"
echo -e "  ${C_DIM}1.${C_RESET} Browse live Solana prediction markets (DFlow)"
echo -e "  ${C_DIM}2.${C_RESET} Launch AI paper trading with web dashboard"
echo -e "  ${C_DIM}3.${C_RESET} View on-chain trade log (Solana Memo)"
echo -e "  ${C_DIM}4.${C_RESET} Generate Solana Blink for shareable trade"
echo ""
echo -e "  ${C_DIM}Duration: ${DURATION}s | Dashboard: http://localhost:3000${C_RESET}"

wait_key

# ──────────────────────────────────────────────
step 1 "Discover Live Prediction Markets"
info "Querying DFlow Metadata API for active Solana markets..."
echo ""

oracle3 market list --exchange solana --limit 8 2>/dev/null || {
  echo -e "  ${C_YELLOW}(Market listing requires network access — skipping)${C_RESET}"
}

wait_key

# ──────────────────────────────────────────────
step 2 "AI Paper Trading with Live Dashboard"
info "Strategy: SolanaAgentStrategy (LLM + heuristic hybrid)"
info "Exchange: Solana/DFlow | Capital: \$1,000 USDC"
info "Dashboard: ${C_BLUE}http://localhost:3000${C_RESET}"
echo ""
echo -e "  ${C_GREEN}${C_BOLD}Starting trading engine + web dashboard...${C_RESET}"
echo ""

oracle3 dashboard \
  --exchange solana \
  --strategy-ref oracle3.strategy.contrib.solana_agent_strategy:SolanaAgentStrategy \
  --initial-capital 1000 \
  --duration "$DURATION" 2>/dev/null || true

echo ""
success "Trading session complete."

wait_key

# ──────────────────────────────────────────────
step 3 "On-Chain Trade Log (Solana Memo)"
info "Every trade is logged immutably to the Solana blockchain."
info "Fetching recent on-chain trade records..."
echo ""

oracle3 trade-log --limit 10 2>/dev/null || {
  echo -e "  ${C_YELLOW}(Trade log requires Solana RPC — skipping)${C_RESET}"
}

wait_key

# ──────────────────────────────────────────────
step 4 "Summary"
echo ""
echo -e "  ${C_BOLD}What you just saw:${C_RESET}"
echo ""
echo -e "  ${C_GREEN}1.${C_RESET} Live market discovery from DFlow's Solana-based exchange"
echo -e "  ${C_GREEN}2.${C_RESET} AI agent autonomously analyzing news + order books"
echo -e "  ${C_GREEN}3.${C_RESET} Real-time web dashboard with equity curve & P&L"
echo -e "  ${C_GREEN}4.${C_RESET} Every trade logged on-chain for full transparency"
echo ""
echo -e "  ${C_BOLD}Key Differentiators:${C_RESET}"
echo ""
echo -e "  ${C_PURPLE}Solana-native${C_RESET}    Instant settlement, SPL token positions"
echo -e "  ${C_PURPLE}AI-powered${C_RESET}       LLM agent with 8 trading tools"
echo -e "  ${C_PURPLE}Multi-exchange${C_RESET}   Solana + Polymarket + Kalshi"
echo -e "  ${C_PURPLE}Transparent${C_RESET}      On-chain logging via Memo program"
echo -e "  ${C_PURPLE}Shareable${C_RESET}        Solana Blinks for URL-based trades"
echo ""
echo -e "${C_BLUE}${C_BOLD}  ──────────────────────────────────────────────${C_RESET}"
echo -e "${C_BLUE}${C_BOLD}   Built for HackIllinois 2026${C_RESET}"
echo -e "${C_BLUE}${C_BOLD}   https://github.com/YichengYang-Ethan/oracle3${C_RESET}"
echo -e "${C_BLUE}${C_BOLD}  ──────────────────────────────────────────────${C_RESET}"
echo ""
