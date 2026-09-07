"""Response shapes every resource shares.

:class:`Page` lives here rather than beside any one domain because SPEC §12.2 makes
*every* list endpoint cursor-paginated: a client that can page organizations can page
models, logs and audit events without learning a second shape.
"""

from __future__ import annotations

from pydantic import BaseModel


class Page[ItemT](BaseModel):
    """One page of a cursor-paginated list (SPEC §12.2).

    ``next_cursor`` is ``None`` on the last page. There is no total: counting a large
    table on every request costs more than the page does, and the UI pages rather than
    showing "of N".
    """

    items: list[ItemT]
    next_cursor: str | None = None
