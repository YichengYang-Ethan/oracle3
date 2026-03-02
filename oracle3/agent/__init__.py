"""Multi-agent coordination for Oracle3.

Provides a pipeline of specialized agents:
  SignalAgent → RiskAgent → ExecutionAgent
"""

from oracle3.agent.coordinator import AgentCoordinator

__all__ = ['AgentCoordinator']
