"""Private, ephemeral view models; decision intents contain only technical IDs."""

from dataclasses import dataclass, field
from uuid import UUID

from ...application.fact_memory.models import DecisionReceipt, ReviewSnapshot, StoredRelation


@dataclass(frozen=True)
class Query:
    mode: str = "facts"
    status: str = "PENDING"
    information_type: str | None = None
    relation: str | None = None
    after: UUID | None = None
    limit: int = 20
    selected: UUID | None = None


@dataclass(frozen=True)
class Row:
    id: UUID
    status: str
    lock_version: int
    cells: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Detail:
    id: UUID
    status: str
    lock_version: int
    snapshots: tuple[ReviewSnapshot, ...] = field(repr=False)
    decisions: tuple[DecisionReceipt, ...] = ()
    versions: tuple[tuple[int, str], ...] = ()
    relations: tuple[tuple[UUID, str, str], ...] = ()
    suggestion: StoredRelation | None = field(default=None, repr=False)


@dataclass(frozen=True)
class View:
    rows: tuple[Row, ...] = field(repr=False)
    pending_count: int
    total_count: int
    next_after: UUID | None
    detail: Detail | None = field(default=None, repr=False)
    stale_selection: bool = False


@dataclass(frozen=True)
class Intent:
    target: UUID
    kind: str
    action: str
    expected_version: int
    request_id: UUID


@dataclass(frozen=True)
class Outcome:
    state: str  # recorded / absent / uncertain / denied
    receipt: DecisionReceipt | None = None
    error_code: str | None = None


def safe_message(error):
    code = error if isinstance(error, str) else getattr(error, "code", "")
    return {
        "facts.source_invalid": "Źródło jest nieaktualne lub usunięte. Odśwież i wykonaj nową analizę.",
        "facts.not_found": "Propozycja została usunięta. Odśwież widok.",
        "facts.concurrent_decision": "Inna decyzja zmieniła propozycję. Odśwież widok.",
        "facts.invalid_transition": "Ten status nie dopuszcza tej decyzji. Odśwież widok.",
        "facts.semantic_rejected": "Weryfikator jest niedostępny lub nie potwierdził źródła. Zgody nie zapisano.",
        "facts.owned_database_required": "Nie potwierdzono ownership testowej bazy. Operacja zatrzymana.",
        "facts.forbidden": "Brak zaufanej lokalnej sesji zatwierdzania.",
        "facts.idempotency_conflict": "Klucz żądania nie odpowiada tej decyzji. Operacja zatrzymana.",
    }.get(code, "Nie udało się odczytać bazy lub wykonać operacji. Dane panelu wyczyszczono.")
