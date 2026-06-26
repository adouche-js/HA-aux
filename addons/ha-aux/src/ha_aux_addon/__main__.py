"""Entry point for running HA aux as a module: python -m ha_aux_addon"""

from .server import main

if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
