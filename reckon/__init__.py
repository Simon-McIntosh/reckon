try:
    from importlib.metadata import version
    __version__ = version("reckon")
except Exception:
    __version__ = "dev"
