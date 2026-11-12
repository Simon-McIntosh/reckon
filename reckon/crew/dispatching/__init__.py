"""Dispatch preparation, scope claims, comparison arms and launch composition."""


def _unbound_dispatch_call(*args, **kwargs):
    """Refuse a cyclic call until the compatibility facade finishes loading."""
    raise RuntimeError("dispatch facade has not bound its concern modules")
