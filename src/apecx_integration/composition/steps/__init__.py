"""Nanobrain `BaseStep` subclasses that are specific to the APECx
Control Plane contract. They live here (not in
``nanobrain/nanobrain/library/steps/``) because the HTTP contract
they consume — ``/verified_synonyms/*`` in particular — is defined
by this project, not by the framework. Keeping them here keeps
nanobrain free of project-specific API assumptions.

Generic, reusable steps (CSV readers, TSV loaders, etc.) continue
to belong in nanobrain proper.
"""
