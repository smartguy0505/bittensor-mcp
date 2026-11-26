"""Starts the server from a checkout: python -I server.py [--version].

Clients registered with this path keep working. The code is in bittensor_mcp/server.py; an installed copy starts
with the bittensor-mcp command. -I keeps this folder off sys.path, so it is put first here: the checkout's code
runs, even if another version of the package is installed in the same environment.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bittensor_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
