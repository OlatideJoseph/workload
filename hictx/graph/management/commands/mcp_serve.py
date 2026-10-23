"""
Run the hictx MCP server (stdio) so AI coding agents can query the captured
context as tools.

    uv run python manage.py mcp_serve

Register it with an agent, e.g. Claude Code:
    claude mcp add hictx -- uv run --directory /abs/path/to/hictx/workload \
        python manage.py mcp_serve

The server speaks MCP over stdio, so this command must not print anything to
stdout itself.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the hictx MCP server (stdio), exposing captured context to AI agents."

    def handle(self, *args, **options):
        # Import here so Django is fully configured before the server starts.
        from hictx.graph.services.mcp_server import mcp

        mcp.run()  # stdio transport; blocks until the client disconnects
