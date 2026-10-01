"""Hook scripts a harness runs at a tool boundary.

Most modules here are designed to be copied standalone into a target
repository's harness settings — see ``native_agent_guard`` for the
self-contained convention those hooks follow.

``worker_git_guard`` is the exception: it imports ``reckon.worker_git_shim``
from the checkout it sits in, so it must run from a reckon checkout. A
standalone copy would raise on that import and fail open.
"""
