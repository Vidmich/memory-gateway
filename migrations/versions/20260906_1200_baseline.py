"""Baseline: an empty starting point for the migration history.

Task 01 deliberately introduces no domain tables — task 02 owns the first ones and
defines the foreign-key topology. This revision exists so every later migration has a
stable ancestor and so `upgrade head` / `downgrade base` is exercised from day one.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
