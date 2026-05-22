import click
from pathlib import Path


@click.group()
def main():
    """reckon — repo-agnostic agile planning system."""


@main.command()
@click.option("--port", default=8765, show_default=True, help="Port to listen on.")
@click.option("--host", default=None, help="Bind address (default: $DOCS_SERVER_BIND or 127.0.0.1).")
@click.option("--mounts", "mounts_file", default=None, type=click.Path(path_type=Path), help="Path to mounts.json.")
def serve(port, host, mounts_file):
    """Start the planning docs server."""
    from reckon.serve import main as serve_main
    serve_main(port=port, host=host, mounts_file=mounts_file)
