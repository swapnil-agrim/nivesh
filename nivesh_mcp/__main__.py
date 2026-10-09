"""stdio entrypoint used by .mcp.json and the Agent SDK: `python -m nivesh_mcp <server>`."""

import sys

from nivesh_mcp.registry import SERVERS


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in SERVERS:
        print(f"usage: python -m nivesh_mcp {{{','.join(SERVERS)}}}", file=sys.stderr)
        return 2
    SERVERS[argv[0]].mcp.run(transport="stdio", show_banner=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
