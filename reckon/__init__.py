from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("reckon-plans")
except PackageNotFoundError:
    __version__ = "dev"
