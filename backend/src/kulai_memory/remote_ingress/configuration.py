"""Explicit TLS inputs and bounded resource policy; remote mode is off by default."""
from dataclasses import dataclass
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit


class RemoteIngressError(ValueError):
    def __init__(self):
        super().__init__("Remote ingress configuration or security unavailable.")


def dns_name(value: str) -> bool:
    return isinstance(value, str) and len(value) <= 253 and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split("."))


@dataclass(frozen=True)
class RemoteIngressConfig:
    enabled: bool = False
    public_origin: str = ""
    expected_gateway_identity: str = ""
    certificate: Path | None = None
    private_key: Path | None = None
    client_ca: Path | None = None
    gateway_sha256: str | None = None
    max_body_bytes: int = 65536
    max_frame_bytes: int = 262144
    http_timeout: float = 240
    body_timeout: float = 15
    websocket_timeout: float = 900
    idle_timeout: float = 240

    def __post_init__(self):
        try:
            if type(self.enabled) is not bool:
                raise ValueError()
            for value, maximum in ((self.max_body_bytes, 65536), (self.max_frame_bytes, 262144)):
                if type(value) is not int or not 1 <= value <= maximum:
                    raise ValueError()
            for value, maximum in ((self.http_timeout, 240), (self.body_timeout, 30),
                                   (self.websocket_timeout, 900), (self.idle_timeout, 240)):
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 < value <= maximum:
                    raise ValueError()
            if not self.enabled:
                return
            origin = urlsplit(self.public_origin)
            port = origin.port if origin.port is not None else 443
            authority = (origin.hostname or '') + (':' + str(origin.port) if origin.port is not None else '')
            if (origin.scheme != "https" or not dns_name(origin.hostname or "") or
                    origin.username is not None or origin.password is not None or
                    origin.path or origin.query or origin.fragment or
                    self.public_origin != "https://" + origin.netloc or
                    not 1 <= port <= 65535 or origin.netloc.lower() != authority or
                    not dns_name(self.expected_gateway_identity)):
                raise ValueError()
            if self.gateway_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.gateway_sha256):
                raise ValueError()
            for path in (self.certificate, self.private_key, self.client_ca):
                if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
                    raise ValueError()
        except Exception:
            raise RemoteIngressError() from None

    def tls_context(self) -> ssl.SSLContext:
        if not self.enabled:
            raise RemoteIngressError()
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.maximum_version = ssl.TLSVersion.TLSv1_3
            context.verify_mode = ssl.CERT_REQUIRED
            context.verify_flags |= ssl.VERIFY_X509_STRICT
            context.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_TICKET
            context.keylog_filename = None
            context.set_alpn_protocols(["http/1.1"])
            context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
            # Never enter OpenSSL's interactive password prompt during startup.
            context.load_cert_chain(str(self.certificate), str(self.private_key), password=lambda: "")
            context.load_verify_locations(cafile=str(self.client_ca))
            return context
        except Exception:
            raise RemoteIngressError() from None
