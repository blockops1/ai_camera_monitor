"""pipeline — orchestrates the alert pipeline.

This is the ONLY module that imports multiple domains. The
orchestrator wires together:
  vehicle_position → vehicle_identifier → vehicle_matcher → telegram_formatter

The orchestrator does NOT send Telegrams. It returns structured
results that the listener (or any other caller) can handle.

Boundary invariant:
  - pipeline/ may import from any sibling domain.
  - Sibling domains may NOT import from pipeline/.
  - The listener (callers) may import from pipeline/ but nothing else
    from the standalone domains — that keeps the orchestrator the
    single integration point.
"""
