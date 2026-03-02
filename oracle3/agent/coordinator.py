"""Multi-agent coordinator — pipeline of SignalAgent → RiskAgent → ExecutionAgent.

Orchestrates specialized agents using asyncio.Queue communication.

Agent tool: delegate_to_specialist(agent_type, task) -> str
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline data types
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    """A trading signal produced by the SignalAgent."""

    ticker: str
    direction: str  # buy, sell, hold
    confidence: float  # 0-1
    reasoning: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeProposal:
    """A risk-checked trade proposal from the RiskAgent."""

    ticker: str
    side: str
    quantity: float
    price: float
    approved: bool = True
    risk_notes: str = ''
    signal: Signal | None = None


@dataclass
class ExecutionResult:
    """The result of executing a trade from the ExecutionAgent."""

    ticker: str
    side: str
    quantity: float
    price: float
    success: bool
    signature: str = ''
    error: str = ''


# ---------------------------------------------------------------------------
# Agent roles
# ---------------------------------------------------------------------------


class BaseAgent:
    """Base class for pipeline agents."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def process(self, input_data: Any) -> Any:
        raise NotImplementedError


class SignalAgent(BaseAgent):
    """Generates trading signals from market data."""

    def __init__(self) -> None:
        super().__init__('signal_agent')

    async def process(self, task: dict[str, Any]) -> Signal:
        """Analyze market data and produce a signal."""
        ticker = task.get('ticker', '')
        price = task.get('price', 0.0)
        context = task.get('context', '')

        # Default signal (override in production with LLM or quant logic)
        return Signal(
            ticker=ticker,
            direction='hold',
            confidence=0.5,
            reasoning=f'Default analysis for {ticker} at {price}',
            metadata={'source': 'signal_agent', 'context': context[:200]},
        )


class RiskAgent(BaseAgent):
    """Evaluates trading signals and applies risk constraints."""

    def __init__(self, risk_manager: Any | None = None) -> None:
        super().__init__('risk_agent')
        self._risk_manager = risk_manager

    async def process(self, signal: Signal) -> TradeProposal:
        """Convert a signal into a risk-checked trade proposal."""
        if signal.direction == 'hold' or signal.confidence < 0.3:
            return TradeProposal(
                ticker=signal.ticker,
                side='hold',
                quantity=0.0,
                price=0.0,
                approved=False,
                risk_notes='Signal too weak or hold',
                signal=signal,
            )

        # Apply risk checks if available
        approved = True
        risk_notes = 'passed'

        if self._risk_manager:
            try:
                status = self._risk_manager.get_risk_status()
                remaining = float(status.get('daily_remaining', '999999'))
                if remaining <= 0:
                    approved = False
                    risk_notes = 'daily limit reached'
            except Exception:
                risk_notes = 'risk check error (allowed)'

        return TradeProposal(
            ticker=signal.ticker,
            side=signal.direction,
            quantity=10.0,  # default size
            price=signal.metadata.get('price', 0.0),
            approved=approved,
            risk_notes=risk_notes,
            signal=signal,
        )


class ExecutionAgent(BaseAgent):
    """Executes approved trade proposals."""

    def __init__(self, trader: Any | None = None) -> None:
        super().__init__('execution_agent')
        self._trader = trader

    async def process(self, proposal: TradeProposal) -> ExecutionResult:
        """Execute an approved trade proposal."""
        if not proposal.approved:
            return ExecutionResult(
                ticker=proposal.ticker,
                side=proposal.side,
                quantity=0.0,
                price=0.0,
                success=False,
                error='Proposal not approved by risk agent',
            )

        # In production, this would call trader.place_order()
        return ExecutionResult(
            ticker=proposal.ticker,
            side=proposal.side,
            quantity=proposal.quantity,
            price=proposal.price,
            success=True,
            signature='simulated',
        )


# ---------------------------------------------------------------------------
# AgentCoordinator
# ---------------------------------------------------------------------------


class AgentCoordinator:
    """Orchestrates a pipeline of specialized agents.

    Pipeline: SignalAgent → RiskAgent → ExecutionAgent

    Communication uses asyncio.Queue for non-blocking message passing.
    """

    def __init__(
        self,
        signal_agent: SignalAgent | None = None,
        risk_agent: RiskAgent | None = None,
        execution_agent: ExecutionAgent | None = None,
    ) -> None:
        self.signal_agent = signal_agent or SignalAgent()
        self.risk_agent = risk_agent or RiskAgent()
        self.execution_agent = execution_agent or ExecutionAgent()

        self._signal_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._proposal_queue: asyncio.Queue[TradeProposal] = asyncio.Queue()
        self._result_queue: asyncio.Queue[ExecutionResult] = asyncio.Queue()

        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._results: list[ExecutionResult] = []

    async def start(self) -> None:
        """Start the agent pipeline workers."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._signal_worker()),
            asyncio.create_task(self._risk_worker()),
            asyncio.create_task(self._execution_worker()),
        ]

    async def stop(self) -> None:
        """Stop the agent pipeline."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def submit_task(self, task: dict[str, Any]) -> None:
        """Submit a task to the signal agent for processing."""
        await self._signal_queue.put(task)

    async def get_result(self, timeout: float = 5.0) -> ExecutionResult | None:
        """Wait for and return the next execution result."""
        try:
            return await asyncio.wait_for(self._result_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def delegate_to_specialist(
        self,
        agent_type: str,
        task: str,
    ) -> str:
        """Agent tool: delegate a task to a specialist agent.

        Args:
            agent_type: 'signal', 'risk', or 'execution'.
            task: Description of the task.

        Returns:
            String result from the specialist.
        """
        if agent_type == 'signal':
            signal = await self.signal_agent.process({'context': task, 'ticker': '', 'price': 0.0})
            return f'Signal: {signal.direction} (confidence={signal.confidence:.2f}) — {signal.reasoning}'
        elif agent_type == 'risk':
            # Create a dummy signal for risk evaluation
            signal = Signal(ticker='', direction='buy', confidence=0.5, reasoning=task)
            proposal = await self.risk_agent.process(signal)
            return f'Risk: approved={proposal.approved}, notes={proposal.risk_notes}'
        elif agent_type == 'execution':
            proposal = TradeProposal(
                ticker='', side='buy', quantity=0, price=0, approved=False, risk_notes=task
            )
            result = await self.execution_agent.process(proposal)
            return f'Execution: success={result.success}, error={result.error}'
        else:
            return f'Unknown agent type: {agent_type}'

    async def run_pipeline(self, task: dict[str, Any]) -> ExecutionResult:
        """Run the full pipeline synchronously for a single task.

        This is a convenience method that doesn't use the queues.
        """
        signal = await self.signal_agent.process(task)
        proposal = await self.risk_agent.process(signal)
        result = await self.execution_agent.process(proposal)
        self._results.append(result)
        return result

    @property
    def results(self) -> list[ExecutionResult]:
        return list(self._results)

    def get_pipeline_status(self) -> dict[str, Any]:
        """Return the current pipeline status."""
        return {
            'running': self._running,
            'signal_queue_size': self._signal_queue.qsize(),
            'proposal_queue_size': self._proposal_queue.qsize(),
            'result_queue_size': self._result_queue.qsize(),
            'total_results': len(self._results),
        }

    # ---- Worker loops ----

    async def _signal_worker(self) -> None:
        while self._running:
            try:
                task = await asyncio.wait_for(self._signal_queue.get(), timeout=1.0)
                signal = await self.signal_agent.process(task)
                proposal = await self.risk_agent.process(signal)
                await self._proposal_queue.put(proposal)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug('Signal worker error', exc_info=True)

    async def _risk_worker(self) -> None:
        """Risk worker processes proposals (already processed inline by signal worker)."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break

    async def _execution_worker(self) -> None:
        while self._running:
            try:
                proposal = await asyncio.wait_for(
                    self._proposal_queue.get(), timeout=1.0
                )
                result = await self.execution_agent.process(proposal)
                self._results.append(result)
                await self._result_queue.put(result)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug('Execution worker error', exc_info=True)
