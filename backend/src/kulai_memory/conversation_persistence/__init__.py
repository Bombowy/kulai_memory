"""Opt-in V2 adapter. Not imported or initialized by the V1 application."""

from .postgres import PostgresConversationRepository

__all__ = ["PostgresConversationRepository"]
