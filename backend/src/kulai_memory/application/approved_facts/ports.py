"""Short consistent reads; implementations must release transactions on return."""

from typing import Protocol

from .models import ApprovedCollection, ApprovedFact, FactRelation


class ApprovedFactsReader(Protocol):
    async def read(self, *, limit: int, project: str | None = None,
                   subject_scope: str | None = None) -> ApprovedCollection: ...

    async def revalidate(self, facts: tuple[ApprovedFact, ...],
                         relations: tuple[FactRelation, ...]) -> None: ...
