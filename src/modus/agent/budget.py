"""Agent-layer budget helpers.

The run budget is ``modus.runtime.budget.RunBudget`` (the single authority for
turn/token/wall-time accounting, shared by every mode).  This module no longer
hosts a second accounting system; it exists so the package layout stays stable
for imports.
"""
