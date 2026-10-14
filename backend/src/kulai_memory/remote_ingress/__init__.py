"""Opt-in, in-process mTLS ingress. No HTTP header can create a trusted peer."""
from .configuration import RemoteIngressConfig, RemoteIngressError

__all__ = ["RemoteIngressConfig", "RemoteIngressError"]
