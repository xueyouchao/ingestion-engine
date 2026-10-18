"""Deployment-side adapters: rented GPU workers and the provider SDKs that rent them.

Nothing here is imported by ``finepdfs``. The pipeline reaches a worker through the existing
``CommandGpuProvider``, which runs a start command and a stop command; these scripts are that
command line (ADR-0009).
"""
