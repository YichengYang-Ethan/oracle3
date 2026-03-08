"""Market relations package -- persistent graph of discovered market relationships.

Provides:
- ``MarketRelation`` / ``ValidationResult`` dataclasses for relation metadata
- ``RelationStore`` for JSON-backed CRUD + graph queries
- ``validate_relation()`` for quantitative validation (cointegration, correlation, etc.)
"""

from oracle3.market.relations import (
    MarketRelation,
    RelationStore,
    ValidationResult,
    SPREAD_TYPES,
    STATUS_LIFECYCLE,
)
from oracle3.market.validation import validate_relation

__all__ = [
    "MarketRelation",
    "RelationStore",
    "ValidationResult",
    "SPREAD_TYPES",
    "STATUS_LIFECYCLE",
    "validate_relation",
]
