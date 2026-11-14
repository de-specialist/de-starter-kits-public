"""Shared credential exclusions and payload checks for edition exports."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path


SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".docker",
    ".gcloud",
    ".kube",
    ".ssh",
    ".terraform",
    "credentials",
    "secrets",
}

SENSITIVE_FILENAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "adc.json",
    "application_default_credentials.json",
    "credentials",
    "credentials.json",
    "google_credentials.json",
    "google-credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "netrc",
    "profiles.yml",
    "terraform.tfstate",
    "terraform.tfstate.backup",
}

SENSITIVE_EXTENSIONS = {
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".p8",
    ".pem",
    ".pkcs8",
    ".ppk",
    ".pfx",
    ".tfstate",
    ".tfplan",
    ".plan",
    ".tfvars",
}

SENSITIVE_PATTERNS = (
    "gha-creds-*.json",
    "*service-account*.json",
    "*service_account*.json",
    "*serviceaccount*.json",
    "*credentials*.json",
    "*credential*.json",
    "*application_default_credentials*.json",
    "*access-token*.json",
    "*access_token*.json",
    "credentials.tfrc.json",
    "*.tfvars.json",
    "*.tfplan",
    "*.plan",
)

SENSITIVE_PATH_SEQUENCES = {
    (".config", "gcloud"),
}

_PRIVATE_KEY_MARKER = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|"
    rb"-----BEGIN PGP PRIVATE KEY BLOCK-----",
    re.IGNORECASE,
)
_GOOGLE_SERVICE_ACCOUNT = re.compile(
    rb"(?:[\"']?type[\"']?\s*[:=]\s*[\"']?service_account\b)|"
    rb"[\"']?private_key_id[\"']?\s*:",
    re.IGNORECASE,
)
_KNOWN_TOKEN = re.compile(
    rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b|"
    rb"\bgithub_pat_[A-Za-z0-9_]{30,}\b|"
    rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|"
    rb"\bAIza[0-9A-Za-z_-]{30,}\b|"
    rb"\bya29\.[0-9A-Za-z_-]{20,}\b|"
    rb"\bsk-(?:proj-|ant-api\d+-|live_)?[A-Za-z0-9_-]{20,}\b",
    re.IGNORECASE,
)
_JWT = re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b")
_CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?im)(?:[\"']?\b(?:password|passwd|secret|client_secret|client-secret|"
    rb"private_key|private-key|access_token|access-token|refresh_token|refresh-token|"
    rb"api_key|api-key|aws_secret_access_key|aws_access_key_id|aws_session_token|"
    rb"access_key|token)\b[\"']?)\s*[:=]\s*[\"']?([^\s\"'#},]+)"
)


def is_sensitive_path(relative: Path) -> bool:
    """Return True for common local private-key and cloud-credential paths."""
    parts = tuple(part.lower() for part in relative.parts)
    filename = relative.name.lower()

    if any(part in SENSITIVE_DIRECTORIES for part in parts):
        return True
    if any(
        any(parts[index : index + len(sequence)] == sequence for index in range(len(parts)))
        for sequence in SENSITIVE_PATH_SEQUENCES
    ):
        return True
    if filename in SENSITIVE_FILENAMES:
        return True
    if Path(filename).suffix.lower() in SENSITIVE_EXTENSIONS:
        return True
    if any(fnmatch.fnmatchcase(filename, pattern) for pattern in SENSITIVE_PATTERNS):
        return True
    if filename.startswith(".env") and (filename == ".env" or filename.startswith(".env.")):
        return True
    return False


def credential_findings(path: Path) -> tuple[str, ...]:
    """Return high-confidence credential markers without returning secret values."""
    payload = path.read_bytes()
    findings: list[str] = []

    if _PRIVATE_KEY_MARKER.search(payload):
        findings.append("private-key PEM marker")
    if _GOOGLE_SERVICE_ACCOUNT.search(payload):
        findings.append("Google service-account JSON field")
    if _KNOWN_TOKEN.search(payload):
        findings.append("known cloud or provider token format")
    if _JWT.search(payload):
        findings.append("JWT-like token")

    for match in _CREDENTIAL_ASSIGNMENT.finditer(payload):
        value = match.group(1).strip().lower()
        if _is_placeholder(value):
            continue
        findings.append("credential-like key/value assignment")
        break

    return tuple(dict.fromkeys(findings))


def _is_placeholder(value: bytes) -> bool:
    normalized = value.strip().strip(b"\"'` ").lower()
    if not normalized:
        return True
    if normalized.startswith((b"{{", b"${", b"$", b"<", b"{")):
        return True
    if any(
        marker in normalized
        for marker in (
            b"env_var(",
            b"getenv(",
            b"your_",
            b"your-",
            b"replace_me",
            b"replace-me",
            b"placeholder",
            b"example",
            b"dummy",
            b"changeme",
            b"change_me",
            b"redacted",
            b"not_set",
        )
    ):
        return True
    return normalized in {b"none", b"null", b"false", b"true", b"xxxx", b"..."}
