"""Console entry point: ``python -m vinyl_archive``.

Binding comes from ``[server]`` in the config file, so the deployment has a
single source of truth. The app object is built from the very config whose
host/port we bind — passing an import string instead would re-read the file
and could bind one config while serving another.
"""

from __future__ import annotations

import uvicorn

from .config import Config
from .main import create_app


def main() -> None:
    config = Config.load()
    uvicorn.run(create_app(config),
                host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
