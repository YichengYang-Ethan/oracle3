"""On-chain trade logger using Solana Memo program."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Solana Memo Program ID
MEMO_PROGRAM_ID = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'


class OnChainLogger:
    """Logs trade metadata to the Solana blockchain via Memo instructions."""

    def __init__(
        self,
        keypair: Any,
        rpc_url: str = 'https://api.mainnet-beta.solana.com',
    ):
        self._keypair = keypair
        self.rpc_url = rpc_url

    @property
    def public_key(self) -> str:
        return str(self._keypair.pubkey())

    async def log_trade(
        self,
        market_ticker: str,
        side: str,
        price: float,
        quantity: int,
        trade_signature: str,
    ) -> str:
        """Write a trade memo to the Solana blockchain.

        Returns the memo transaction signature.
        """
        memo_data = json.dumps({
            'app': 'oracle3',
            'action': 'trade',
            'market': market_ticker,
            'side': side,
            'price': price,
            'qty': quantity,
            'ref': trade_signature[:16],
            'ts': datetime.now(timezone.utc).isoformat(),
        }, separators=(',', ':'))

        try:
            from solders.hash import Hash
            from solders.instruction import AccountMeta, Instruction
            from solders.message import MessageV0
            from solders.pubkey import Pubkey
            from solders.transaction import VersionedTransaction

            memo_program = Pubkey.from_string(MEMO_PROGRAM_ID)
            signer = self._keypair.pubkey()

            memo_ix = Instruction(
                program_id=memo_program,
                accounts=[AccountMeta(pubkey=signer, is_signer=True, is_writable=True)],
                data=memo_data.encode('utf-8'),
            )

            # Get recent blockhash
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.rpc_url, json={
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'getLatestBlockhash',
                    'params': [{'commitment': 'finalized'}],
                })
                blockhash_str = resp.json()['result']['value']['blockhash']

            blockhash = Hash.from_string(blockhash_str)
            msg = MessageV0.try_compile(
                payer=signer,
                instructions=[memo_ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self._keypair])

            raw = bytes(tx)
            tx_b64 = base64.b64encode(raw).decode('ascii')

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.rpc_url, json={
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'sendTransaction',
                    'params': [tx_b64, {'encoding': 'base64'}],
                })
                result = resp.json()

            if 'error' in result:
                logger.error('Memo tx failed: %s', result['error'])
                return ''

            sig = result.get('result', '')
            logger.info('Trade memo logged: %s', sig)
            return sig

        except ImportError:
            logger.warning('solders not installed — skipping on-chain logging')
            return ''
        except Exception as e:
            logger.error('Failed to log trade on-chain: %s', e)
            return ''

    async def get_trade_log(self, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch recent memo transactions from this wallet."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(self.rpc_url, json={
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'getSignaturesForAddress',
                    'params': [self.public_key, {'limit': limit}],
                })
                result = resp.json()

            signatures = result.get('result', [])
            trades: list[dict[str, Any]] = []

            for sig_info in signatures:
                sig = sig_info.get('signature', '')
                resp2 = None
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp2 = await client.post(self.rpc_url, json={
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'getTransaction',
                        'params': [sig, {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}],
                    })
                    tx_data = resp2.json().get('result')

                if not tx_data:
                    continue

                # Extract memo data from log messages
                log_messages = tx_data.get('meta', {}).get('logMessages', [])
                for msg in log_messages:
                    if 'Program log: Memo' in msg or '"app":"oracle3"' in msg:
                        # Try to parse the memo JSON
                        for part in msg.split('Memo (len '):
                            if '"oracle3"' in part:
                                start = part.find('{')
                                end = part.rfind('}') + 1
                                if start >= 0 and end > start:
                                    try:
                                        memo = json.loads(part[start:end])
                                        memo['signature'] = sig
                                        trades.append(memo)
                                    except json.JSONDecodeError:
                                        pass

            return trades

        except Exception as e:
            logger.error('Failed to fetch trade log: %s', e)
            return []
