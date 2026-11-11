"""Adapt Apixis core startup and shutdown to the kernel lifecycle."""

from apixis import start_core, stop_core


class ApixisCore:
    """Own Apixis core startup and shutdown within the kernel lifecycle."""

    async def start(self):
        await start_core()

    async def stop(self):
        await stop_core()

    def get_version(self) -> str:
        from apixis import VERSION

        return VERSION


apixis_core = ApixisCore()
