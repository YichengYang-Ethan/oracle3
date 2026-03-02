"""Jito Bundle submitter for MEV protection.

Wraps transactions into Jito Bundles and submits them to the
Jito Block Engine, with fallback to standard RPC on failure.

Agent tool: get_mev_protection_status() -> dict
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Jito Block Engine endpoints
JITO_MAINNET_URL = 'https://mainnet.block-engine.jito.wtf/api/v1/bundles'
JITO_TIP_ACCOUNT = 'Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY'

# Default tip: 10,000 lamports (0.00001 SOL)
DEFAULT_TIP_LAMPORTS = 10_000


@dataclass(frozen=True)
class JitoSubmitResult:
    """Result of a Jito Bundle submission."""

    success: bool
    bundle_id: str
    signature: str
    tip_lamports: int
    fallback_used: bool = False


class JitoSubmitter:
    """Submit Solana transactions via Jito Bundle for MEV protection.

    Wraps a transaction into a 2-tx bundle: [our_tx, tip_tx].
    If Jito submission fails, falls back to standard RPC.
    """

    def __init__(
        self,
        keypair: Any,
        rpc_url: str = 'https://api.mainnet-beta.solana.com',
        jito_url: str = JITO_MAINNET_URL,
        tip_lamports: int = DEFAULT_TIP_LAMPORTS,
        tip_account: str = JITO_TIP_ACCOUNT,
    ) -> None:
        self._keypair = keypair
        self.rpc_url = rpc_url
        self.jito_url = jito_url
        self.tip_lamports = tip_lamports
        self.tip_account = tip_account

        # Stats
        self._total_submitted: int = 0
        self._jito_successes: int = 0
        self._fallback_count: int = 0
        self._enabled: bool = True

    @property
    def public_key(self) -> str:
        return str(self._keypair.pubkey())

    async def submit_with_jito(self, tx_bytes: bytes) -> JitoSubmitResult:
        """Submit a transaction via Jito Bundle.

        Creates a bundle with the original transaction and a tip transaction,
        then submits to the Jito Block Engine. Falls back to standard RPC
        on failure.

        Args:
            tx_bytes: Serialized transaction bytes.

        Returns:
            JitoSubmitResult with submission details.
        """
        self._total_submitted += 1

        if not self._enabled:
            sig = await self._submit_standard_rpc(tx_bytes)
            self._fallback_count += 1
            return JitoSubmitResult(
                success=bool(sig),
                bundle_id='',
                signature=sig,
                tip_lamports=0,
                fallback_used=True,
            )

        try:
            # Build tip transaction
            tip_tx_bytes = await self._build_tip_transaction()

            # Encode both transactions
            tx_b64 = base64.b64encode(tx_bytes).decode('ascii')
            tip_b64 = base64.b64encode(tip_tx_bytes).decode('ascii')

            # Submit bundle to Jito
            bundle_payload = {
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'sendBundle',
                'params': [[tx_b64, tip_b64]],
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(self.jito_url, json=bundle_payload)
                result = resp.json()

            if 'error' in result:
                logger.warning('Jito bundle rejected: %s', result['error'])
                raise RuntimeError(f'Jito error: {result["error"]}')

            bundle_id = result.get('result', '')
            logger.info('Jito bundle submitted: %s', bundle_id)

            # Extract the signature from the original transaction
            sig = self._extract_signature(tx_bytes)
            self._jito_successes += 1

            return JitoSubmitResult(
                success=True,
                bundle_id=bundle_id,
                signature=sig,
                tip_lamports=self.tip_lamports,
            )

        except Exception as exc:
            logger.warning('Jito submission failed, falling back to RPC: %s', exc)
            sig = await self._submit_standard_rpc(tx_bytes)
            self._fallback_count += 1
            return JitoSubmitResult(
                success=bool(sig),
                bundle_id='',
                signature=sig,
                tip_lamports=0,
                fallback_used=True,
            )

    async def _build_tip_transaction(self) -> bytes:
        """Build a SOL transfer transaction to the Jito tip account."""
        try:
            from solders.message import MessageV0
            from solders.pubkey import Pubkey
            from solders.system_program import TransferParams, transfer
            from solders.transaction import VersionedTransaction

            payer = self._keypair.pubkey()
            tip_pubkey = Pubkey.from_string(self.tip_account)

            tip_ix = transfer(
                TransferParams(
                    from_pubkey=payer,
                    to_pubkey=tip_pubkey,
                    lamports=self.tip_lamports,
                )
            )

            # Get recent blockhash
            blockhash = await self._get_recent_blockhash()

            msg = MessageV0.try_compile(
                payer=payer,
                instructions=[tip_ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [self._keypair])
            return bytes(tx)

        except ImportError:
            logger.warning('solders not installed — cannot build tip transaction')
            raise

    async def _get_recent_blockhash(self) -> Any:
        """Fetch recent blockhash from RPC."""
        from solders.hash import Hash

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                self.rpc_url,
                json={
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'getLatestBlockhash',
                    'params': [{'commitment': 'finalized'}],
                },
            )
            result = resp.json()

        blockhash_str = result['result']['value']['blockhash']
        return Hash.from_string(blockhash_str)

    async def _submit_standard_rpc(self, tx_bytes: bytes) -> str:
        """Fall back to standard Solana RPC submission."""
        tx_b64 = base64.b64encode(tx_bytes).decode('ascii')
        rpc_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'sendTransaction',
            'params': [
                tx_b64,
                {'encoding': 'base64', 'skipPreflight': False},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.rpc_url, json=rpc_payload)
                result = resp.json()

            if 'error' in result:
                logger.error('Standard RPC error: %s', result['error'])
                return ''

            return result.get('result', '')
        except Exception:
            logger.exception('Standard RPC submission failed')
            return ''

    @staticmethod
    def _extract_signature(tx_bytes: bytes) -> str:
        """Extract the transaction signature from raw bytes."""
        try:
            from solders.transaction import VersionedTransaction

            tx = VersionedTransaction.from_bytes(tx_bytes)
            if tx.signatures:
                return str(tx.signatures[0])
        except Exception:
            pass
        return ''

    def get_mev_protection_status(self) -> dict[str, Any]:
        """Agent tool: return current MEV protection status."""
        return {
            'enabled': self._enabled,
            'jito_url': self.jito_url,
            'tip_lamports': self.tip_lamports,
            'total_submitted': self._total_submitted,
            'jito_successes': self._jito_successes,
            'fallback_count': self._fallback_count,
            'jito_success_rate': (
                round(self._jito_successes / self._total_submitted, 4)
                if self._total_submitted > 0
                else 0.0
            ),
        }
