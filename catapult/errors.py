"""Normalized error categories for Catapult backend operations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

MISSING_SAVED_IPA = "missing_saved_ipa"
DEVICE_UNREACHABLE = "device_unreachable"
NOT_AUTHENTICATED = "not_authenticated"
TUNNEL_NOT_READY = "tunnel_not_ready"
PROFILE_MISSING = "profile_missing"
INVALID_SIGNATURE = "invalid_signature"
APPLE_REJECTED_BUNDLE = "apple_rejected_bundle"
SIGNING_CHAIN = "signing_chain"
UNKNOWN = "unknown"

KNOWN_CATEGORIES = {
    MISSING_SAVED_IPA,
    DEVICE_UNREACHABLE,
    NOT_AUTHENTICATED,
    TUNNEL_NOT_READY,
    PROFILE_MISSING,
    INVALID_SIGNATURE,
    APPLE_REJECTED_BUNDLE,
    SIGNING_CHAIN,
    UNKNOWN,
}

_PUBLIC_MESSAGES = {
    MISSING_SAVED_IPA: "Saved IPA is missing or no saved install record exists.",
    DEVICE_UNREACHABLE: "Device is not connected or reachable.",
    NOT_AUTHENTICATED: "Not authenticated. Please sign in first.",
    TUNNEL_NOT_READY: "Device tunnel is not ready.",
    PROFILE_MISSING: "Provisioning profile is missing or invalid.",
    INVALID_SIGNATURE: "Signed app signature is invalid.",
    APPLE_REJECTED_BUNDLE: "Apple rejected the bundle identifier for this account.",
    SIGNING_CHAIN: "Signing certificate chain or keychain setup failed.",
    UNKNOWN: "Operation failed.",
}

_SECRET_KEY_RE = re.compile(
    r"(?i)\b("
    r"password|passwd|pwd|token|authorization|cookie|secret|session_token|"
    r"identity_token|identity\s+token|idms_token|idms\s+token|gs_token|gs\s+token|"
    r"x-apple-gs-token|x-apple-identity-token|"
    r"adsid|dsprsid|sk"
    r")\b(\s*[:=]\s*)([^\s,;'\"]+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+")


@dataclass(frozen=True)
class NormalizedError:
    category: str
    message: str
    detail: str

    def to_dict(self, *, redact: bool = True) -> dict[str, str]:
        message = redact_sensitive(self.message) if redact else self.message
        detail = redact_sensitive(self.detail) if redact else self.detail
        return {
            "category": self.category,
            "message": message,
            "detail": detail,
        }


def redact_sensitive(value: Any) -> Any:
    """Redact token-like values in diagnostics payloads and log excerpts."""
    if not isinstance(value, str):
        return value
    text = _SECRET_KEY_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)
    return _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)} <redacted>", text)


def normalize_error(error: BaseException | str | None, *, default_message: str = "Operation failed.") -> NormalizedError:
    """Map a raw exception/string to a stable Catapult error category."""
    detail = _raw_detail(error)
    lowered = detail.lower()
    category = _category_for(lowered, error)
    public = _PUBLIC_MESSAGES.get(category, default_message)
    if category == UNKNOWN and detail:
        public = detail
    return NormalizedError(category=category, message=public, detail=detail)


def _raw_detail(error: BaseException | str | None) -> str:
    if error is None:
        return ""
    if isinstance(error, str):
        return error
    return str(error) or repr(error)


def _category_for(lowered: str, error: BaseException | str | None) -> str:
    result_code = getattr(error, "result_code", None)

    if any(
        phrase in lowered
        for phrase in (
            "no saved install",
            "saved ipa is missing",
            "saved ipa file",
            "install it once from an ipa",
            "choose the ipa again",
        )
    ):
        return MISSING_SAVED_IPA

    if any(
        phrase in lowered
        for phrase in (
            "not authenticated",
            "please sign in",
            "no pending auth session",
            "incorrect apple id or password",
            "authentication required",
        )
    ):
        return NOT_AUTHENTICATED

    if "apple rejected" in lowered or result_code == 9401 or (
        "bundle" in lowered and "not available" in lowered
    ):
        return APPLE_REJECTED_BUNDLE

    if "tunnel" in lowered and any(
        phrase in lowered
        for phrase in (
            "not ready",
            "no active",
            "setup",
            "did not become ready",
            "failed",
            "not marked ready",
        )
    ):
        return TUNNEL_NOT_READY

    if any(
        phrase in lowered
        for phrase in (
            "not connected or reachable",
            "device scan timed out",
            "device scan failed",
            "device ",
            "not found on the network",
            "trust this mac",
            "connection refused",
            "could not connect",
            "pairing failed",
        )
    ) and "profile" not in lowered:
        return DEVICE_UNREACHABLE

    if any(
        phrase in lowered
        for phrase in (
            "provisioning profile",
            "encodedprofile",
            "mobileprovision",
            "could not parse provisioning profile",
            "empty provisioning profile",
        )
    ):
        return PROFILE_MISSING

    if any(
        phrase in lowered
        for phrase in (
            "wwdr",
            "certificate chain",
            "unable to build chain",
            "errsecinternalcomponent",
            "find-identity",
            "no signing identities",
            "keychain",
            "security import",
        )
    ):
        return SIGNING_CHAIN

    if any(
        phrase in lowered
        for phrase in (
            "invalid signature",
            "ad-hoc",
            "adhoc",
            "codesign",
            "signature=adhoc",
            "signed app still",
        )
    ):
        return INVALID_SIGNATURE

    return UNKNOWN
