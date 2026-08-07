"""W3 Optimize — builderize a site in place (ADR-009).

Phase A transforms live here. Every one is a record-to-record function with a
machine gate; the rendering oracle (`oracle.py`) is the equivalence relation
that lets a structural rewrite claim "changed nothing visible".
"""
