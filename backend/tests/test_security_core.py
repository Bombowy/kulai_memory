"""Neutral security core: identity, lifetime, atomic rotation and safe records."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from kulai_memory.security import (
    Action, Authenticator, AuthorizationPolicy, CredentialManager, DeviceStatus, SecurityConfig,
    SecurityError, SecurityMode,
)
from kulai_memory.security.authentication import token_digest
from kulai_memory.security.in_memory import InMemorySecurityStore
from kulai_memory.settings import Settings


async def credentials():
    clock = [datetime(2026, 10, 13, tzinfo=timezone.utc)]
    store = InMemorySecurityStore()
    manager = CredentialManager(store, clock=lambda: clock[0])
    device = await manager.register_device(trusted=True)
    credential = await manager.issue(device.id)
    return clock, store, manager, device, credential


def test_random_separate_identity_digest_only_and_restart_denies():
    async def scenario():
        clock, store, manager, device, credential = await credentials()
        second = await manager.issue(device.id)
        assert credential.access_token != second.access_token
        assert len(credential.access_token) == 43
        assert str(device.id) not in credential.access_token
        record = await store.lookup(token_digest(credential.access_token))
        assert record.token.id != record.device.id
        assert credential.access_token not in repr(credential) + repr(record) + repr(store.__dict__)
        assert record.token.digest not in repr(record)
        principal = await Authenticator(store, clock=lambda: clock[0]).authenticate(credential.access_token)
        assert principal.device_id == device.id
        with pytest.raises(SecurityError):
            await Authenticator(InMemorySecurityStore()).authenticate(credential.access_token)
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["pending", "revoked_device", "revoked_token", "expired", "future", "other_device", "unknown"])
def test_inactive_credentials_and_wrong_device_rejected(state):
    async def scenario():
        clock, store, manager, device, token = await credentials()
        secret, claimed = token.access_token, device.id
        if state == "pending":
            await store.set_device_status(device.id, DeviceStatus.PENDING)
        elif state == "revoked_device":
            await manager.revoke_device(device.id)
        elif state == "revoked_token":
            await manager.revoke(token.token_id)
        elif state == "expired":
            clock[0] = token.expires_at
        elif state == "future":
            clock[0] -= timedelta(seconds=1)
        elif state == "other_device":
            claimed = (await manager.register_device(trusted=True)).id
        else:
            secret = "x" * 43
        with pytest.raises(SecurityError, match="^Authentication required\\.$"):
            await Authenticator(store, clock=lambda: clock[0]).authenticate(secret, claimed_device_id=claimed)
    asyncio.run(scenario())


def test_pending_must_be_trusted_and_revoked_device_cannot_be_resurrected():
    async def scenario():
        store = InMemorySecurityStore()
        manager = CredentialManager(store)
        device = await manager.register_device()
        with pytest.raises(SecurityError):
            await manager.issue(device.id)
        await manager.trust_device(device.id)
        await manager.issue(device.id)
        await manager.revoke_device(device.id)
        with pytest.raises(SecurityError):
            await manager.trust_device(device.id)
        with pytest.raises(SecurityError):
            await manager.issue(device.id)
    asyncio.run(scenario())


def test_atomic_rotation_preserves_permissions_and_only_one_successor():
    async def scenario():
        clock, store, manager, device, credential = await credentials()
        results = await asyncio.gather(*(
            manager.rotate(credential.access_token, device_id=device.id) for _ in range(2)), return_exceptions=True)
        assert sum(isinstance(r, SecurityError) for r in results) == 1
        replacement = next(r for r in results if not isinstance(r, Exception))
        auth = Authenticator(store, clock=lambda: clock[0])
        with pytest.raises(SecurityError):
            await auth.authenticate(credential.access_token)
        new = await auth.authenticate(replacement.access_token)
        assert new.actions == frozenset(Action)
        await manager.revoke(replacement.token_id)
        await manager.revoke(replacement.token_id)  # idempotent
        with pytest.raises(SecurityError):
            await manager.rotate(replacement.access_token, device_id=device.id)
    asyncio.run(scenario())


def test_rotation_rejects_device_or_scope_change_without_revoking_old():
    async def scenario():
        clock, store, _, _, credential = await credentials()
        original = (await store.lookup(token_digest(credential.access_token))).token
        for changes in ({"device_id": uuid4()}, {"actions": frozenset({Action.MEMORY_READ})}, {"project_id": uuid4()}):
            replacement = replace(original, id=uuid4(), digest="a" * 64, **changes)
            with pytest.raises(SecurityError):
                await store.rotate_token(original.id, replacement, now=clock[0])
        assert await Authenticator(store, clock=lambda: clock[0]).authenticate(credential.access_token)
    asyncio.run(scenario())


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), timedelta(days=31), 3600])
def test_invalid_lifetime_rejected(ttl):
    async def scenario():
        _, _, manager, device, _ = await credentials()
        with pytest.raises(SecurityError):
            await manager.issue(device.id, ttl=ttl)
    asyncio.run(scenario())


@pytest.mark.parametrize("secret", ["", "bad", "x" * 42, "x" * 44, "x" * 42 + "\n", "ż" * 43])
def test_invalid_secret_format_has_no_input_in_error(secret):
    with pytest.raises(SecurityError) as caught:
        token_digest(secret)
    assert str(caught.value) == "Authentication required."
    assert caught.value.__cause__ is None


def test_policy_separates_actions_projects_and_unknown_operations():
    async def scenario():
        clock, store, manager, device, _ = await credentials()
        project = uuid4()
        credential = await manager.issue(device.id, actions=frozenset({Action.MEMORY_READ}), project_id=project)
        principal = await Authenticator(store, clock=lambda: clock[0]).authenticate(credential.access_token)
        policy = AuthorizationPolicy()
        assert policy.permits(principal, Action.MEMORY_READ, project_id=project)
        assert not policy.permits(principal, Action.MEMORY_READ, project_id=uuid4())
        assert not policy.permits(principal, Action.MEMORY_READ)
        assert not policy.permits(principal, Action.MEMORY_WRITE, project_id=project)
        assert not policy.permits(principal, None, project_id=project)
    asyncio.run(scenario())


def test_defaults_secure_and_invalid_modes_fail_validation():
    assert Settings(_env_file=None).kulai_security_mode is SecurityMode.SECURE
    assert SecurityConfig().mode is SecurityMode.SECURE
    with pytest.raises(ValueError):
        Settings(_env_file=None, kulai_security_mode="invalid")
    with pytest.raises(ValueError):
        SecurityConfig(mode="LOCAL_DEV")


def test_inconsistent_storage_snapshot_fails_closed():
    async def scenario():
        clock, store, _, _, credential = await credentials()
        original = await store.lookup(token_digest(credential.access_token))
        class InconsistentReader:
            async def lookup(self, digest):
                return replace(original, token=replace(original.token, digest="f" * 64))
        with pytest.raises(SecurityError):
            await Authenticator(InconsistentReader(), clock=lambda: clock[0]).authenticate(credential.access_token)
    asyncio.run(scenario())
