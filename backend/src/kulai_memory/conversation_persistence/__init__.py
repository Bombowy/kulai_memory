"""Explicitly constructed V2 adapter. Importing never opens a DB or runs DDL."""

from .postgres import PostgresConversationRepository

__all__ = ["PostgresConversationRepository"]
