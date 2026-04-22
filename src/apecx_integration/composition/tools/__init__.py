"""Project-specific Tool adapters that wrap nanobrain tools for the
APECx local-default deployment.

Currently just ``BVBRCSnapshotTool`` — a ``BVBRCTool`` subclass that
reads from local TSV / FASTA snapshot files instead of calling the
BV-BRC REST API. See its module docstring for the why and scope.
"""
