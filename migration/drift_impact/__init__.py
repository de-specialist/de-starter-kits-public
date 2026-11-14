"""Offline, advisory schema drift-impact analysis (backlog cycle 18).

Modules in this package consume already-validated, fingerprint-bound schema metadata. They
never read source rows, contact a warehouse, infer SQL lineage, approve a schema, or write a
baseline, and they are not wired into the loader or dbt path.
"""
