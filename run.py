#!/usr/bin/env python3
"""Entry point for SuperKaraoke server."""
import argparse
import uvicorn
from server.config import settings


def main():
    parser = argparse.ArgumentParser(description="SuperKaraoke server")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--media-dir", default=str(settings.media_dir),
                        help="Path to karaoke media directory")
    parser.add_argument("--allowed-networks", default=None,
                        metavar="CIDRS",
                        help='Comma-separated CIDR subnets that skip authentication '
                             '(e.g. "192.168.0.0/16,10.0.0.0/8")')
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")
    args = parser.parse_args()

    # Override settings from CLI
    import os
    os.environ["SK_MEDIA_DIR"] = args.media_dir
    if args.allowed_networks is not None:
        os.environ["SK_ALLOWED_NETWORKS"] = args.allowed_networks

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
