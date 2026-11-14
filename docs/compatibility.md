# Compatibility

Python 3.11 is the target runtime. Each edition has independent direct requirements
and a hash-locked transitive requirements file. The lockfiles are the source of
truth for dependency versions. Install with `python -m pip install --require-hashes
-r requirements-lock.txt` from the edition directory.

Public CI validates parsing, synthetic fixtures and static checks. It does not
establish authenticated warehouse compatibility or cloud execution. Validate any
real deployment in your own isolated, private environment before use.

When changing dependencies, regenerate the matching Python 3.11 lock with hashes,
check direct requirements against it, install into a separate environment and run
that edition's checks. Do not reuse mutable environments between editions.
