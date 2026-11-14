"""Fixed refusal reason codes for the Silver starter-model scaffolder (backlog 145).

Codes are stable lowercase identifiers. A run emits exactly one code, for the first failing
check. Codes never carry interpolated names, paths, fingerprints, or exception text.

Sources:
- `docs/scaffolder-contract-proposal.md` sections 3.1, 3.2, 6 (workspace, scope, collisions).
- `docs/scaffolder-input-contract.md` "Fixed reason codes" (input admission and bindings).
- Core gates defined by the scaffolder core (this package).
"""

from __future__ import annotations


class Refusal(Exception):
    """Raised by a check with exactly one fixed reason code; carries no other data."""

    def __init__(self, code: str):
        if code not in ALL_CODES:
            raise ValueError("Unknown scaffolder reason code.")
        super().__init__(code)
        self.code = code


# Invocation (contract proposal section 3).
INVALID_SOURCE_ID = "invalid_source_id"
INVALID_SOURCE_BINDING_ID = "invalid_source_binding_id"

# Workspace checks (contract proposal section 3.1).
WORKSPACE_INSIDE_STARTER_REPO = "workspace_inside_starter_repo"
WORKSPACE_INVALID = "workspace_invalid"
EDITION_PROJECT_MISMATCH = "edition_project_mismatch"
WORKSPACE_MISSING_MIGRATION_MODELS = "workspace_missing_migration_models"
WORKSPACE_IS_STARTER_CHECKOUT = "workspace_is_starter_checkout"

# Canonical retail scope (contract proposal section 3.2).
NON_CANONICAL_TABLE = "non_canonical_table"
NON_CANONICAL_TARGET = "non_canonical_target"
MISSING_CONTRACT_COLUMN = "missing_contract_column"
MISSING_PRIMARY_KEY = "missing_primary_key"
UNSAFE_INVENTORY_IDENTIFIER = "unsafe_inventory_identifier"

# Collision precedence and path safety (contract proposal sections 6.1-6.3).
SYMLINK_REFUSED = "symlink_refused"
NOT_REGULAR_FILE = "not_regular_file"
OUTPUT_EXISTS = "output_exists"
STAGING_EXISTS = "staging_exists"
STAGING_ROOT_INVALID = "staging_root_invalid"

# Input contract (docs/scaffolder-input-contract.md), in contract table order.
INPUT_MISSING = "scaffold_input_missing"
INPUT_NOT_PRIVATE = "scaffold_input_not_private"
INPUT_OVERSIZED = "scaffold_input_oversized"
INPUT_MALFORMED = "scaffold_input_malformed"
INPUT_UNSUPPORTED_VERSION = "scaffold_input_unsupported_version"
FIELD_MISSING = "scaffold_field_missing"
FIELD_MALFORMED = "scaffold_field_malformed"
MAPPING_INVALID = "scaffold_mapping_invalid"
FINGERPRINT_RECOMPUTE_MISMATCH = "scaffold_fingerprint_recompute_mismatch"
MAPPING_FINGERPRINT_MISMATCH = "scaffold_mapping_fingerprint_mismatch"
SCHEMA_CONFIRMATION_MISMATCH = "scaffold_schema_confirmation_mismatch"
SOURCE_KIND_MISMATCH = "scaffold_source_kind_mismatch"
SOURCE_IDENTITY_UNAVAILABLE = "scaffold_source_identity_unavailable"
SOURCE_IDENTITY_MISMATCH = "scaffold_source_identity_mismatch"
SOURCE_SCHEMA_UNAVAILABLE = "scaffold_source_schema_unavailable"
SELECTED_TABLES_MISMATCH = "scaffold_selected_tables_mismatch"
DESTINATION_MISMATCH = "scaffold_destination_mismatch"
NAMESPACE_MISMATCH = "scaffold_namespace_mismatch"
TARGET_IDENTITY_MISMATCH = "scaffold_target_identity_mismatch"
DESTINATION_EDITION_UNBOUND = "scaffold_destination_edition_unbound"
EVIDENCE_NOT_READY = "scaffold_evidence_not_ready"
EVIDENCE_RUN_NOT_COMPLETED = "scaffold_evidence_run_not_completed"
EVIDENCE_SCHEMA_NOT_MATCHED = "scaffold_evidence_schema_not_matched"
EVIDENCE_STALE = "scaffold_evidence_stale"
EVIDENCE_FRESHNESS_UNAVAILABLE = "scaffold_evidence_freshness_unavailable"

# Core gates and renderer contract .
# Staging, sealing, and atomic no-replace publication (contract 6.2) are not implemented
# until atomic workspace publication lands; every non-dry run refuses with this.
FINAL_GENERATION_GATED = "final_generation_gated"
# A registered edition renderer returned output that breaks the renderer contract.
RENDERER_OUTPUT_INVALID = "renderer_output_invalid"

INPUT_CONTRACT_CODES = (
    INPUT_MISSING, INPUT_NOT_PRIVATE, INPUT_OVERSIZED, INPUT_MALFORMED, INPUT_UNSUPPORTED_VERSION,
    FIELD_MISSING, FIELD_MALFORMED, MAPPING_INVALID, FINGERPRINT_RECOMPUTE_MISMATCH,
    MAPPING_FINGERPRINT_MISMATCH, SCHEMA_CONFIRMATION_MISMATCH, SOURCE_KIND_MISMATCH,
    SOURCE_IDENTITY_UNAVAILABLE, SOURCE_IDENTITY_MISMATCH, SOURCE_SCHEMA_UNAVAILABLE,
    SELECTED_TABLES_MISMATCH, DESTINATION_MISMATCH, NAMESPACE_MISMATCH, TARGET_IDENTITY_MISMATCH,
    DESTINATION_EDITION_UNBOUND, EVIDENCE_NOT_READY, EVIDENCE_RUN_NOT_COMPLETED,
    EVIDENCE_SCHEMA_NOT_MATCHED, EVIDENCE_STALE, EVIDENCE_FRESHNESS_UNAVAILABLE,
)

ALL_CODES = frozenset({
    INVALID_SOURCE_ID, INVALID_SOURCE_BINDING_ID,
    WORKSPACE_INSIDE_STARTER_REPO, WORKSPACE_INVALID, EDITION_PROJECT_MISMATCH,
    WORKSPACE_MISSING_MIGRATION_MODELS, WORKSPACE_IS_STARTER_CHECKOUT,
    NON_CANONICAL_TABLE, NON_CANONICAL_TARGET, MISSING_CONTRACT_COLUMN, MISSING_PRIMARY_KEY,
    UNSAFE_INVENTORY_IDENTIFIER,
    SYMLINK_REFUSED, NOT_REGULAR_FILE, OUTPUT_EXISTS, STAGING_EXISTS, STAGING_ROOT_INVALID,
    *INPUT_CONTRACT_CODES,
    FINAL_GENERATION_GATED, RENDERER_OUTPUT_INVALID,
})
