"""Common action/project policy, independent of HTTP, WS and Memory/RAG."""
from uuid import UUID
from .models import Action, Principal


class AuthorizationPolicy:
    def permits(self, principal: Principal, action: Action | None, *, project_id: UUID | None = None) -> bool:
        return (action is not None and action in principal.actions
                and (principal.project_id is None or principal.project_id == project_id))
