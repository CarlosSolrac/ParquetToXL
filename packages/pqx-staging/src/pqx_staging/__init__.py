"""Scratch lifecycle, stage-in, publish, delete, list and the profile lease.

Pure file movement over ``pqx-common``, testable against ``memory://`` and a real temporary
directory. It knows nothing about manifests: ``reconcile`` and the ownership rule are manifest and
configuration logic and live in ``pqx-plan``, so this stays a primitives library and the dependency
arrow does not reverse.
"""
