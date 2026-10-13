"""Explicit secure default and a numeric-loopback-only development exception."""
from dataclasses import dataclass
from enum import Enum
from ipaddress import ip_address


class SecurityMode(str, Enum):
    LOCAL_DEV = "LOCAL_DEV"
    SECURE = "SECURE"


def loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    mode: SecurityMode = SecurityMode.SECURE
    authentication_timeout: float = 5.0

    def __post_init__(self) -> None:
        if (not isinstance(self.mode, SecurityMode) or isinstance(self.authentication_timeout, bool)
                or not 0 < self.authentication_timeout <= 30):
            raise ValueError("Invalid security configuration.")
