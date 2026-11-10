import asyncio
import sys
from pathlib import Path

from alira.utils.logger import logger

ALIRA_DIR = Path(__file__).resolve().parents[2]   # -> src/alira

class BaseModule:

    def __init__(
        self,
        name: str,
        version: str,
        *,
        description: str = "",
        required_modules: list[str] = None,
        requirements_txt: str = None,
        author: str = None,
        logo_banner: str = None,
    ):
        '''Initialize the BaseModule with its metadata.

        Args:
            name (str): The name of the module.
            version (str): The version of the module.
            description (str, optional): A brief description of the module. Defaults to "".
            required_modules (list[str], optional): A list of module names that this module depends on. Defaults to None.
            requirements_txt (str, optional): Absolute path or path relative to alira/modules.
                Defaults to <name>/requirements.txt within alira/modules.
            author (str, optional): The author of the module. Defaults to None.
            logo_banner (str, optional): A path to a logo banner for the module. Defaults to None.
        '''
        self.name = name
        self.version = version
        self.description = description
        self.required_modules = required_modules or []
        self.requirements_txt = requirements_txt
        self.author = author
        self.logo_banner = logo_banner
        if self.requirements_txt:
            self._requirements_txt_path = (ALIRA_DIR / "modules" / self.requirements_txt).resolve()
            if not self._requirements_txt_path.is_file():
                raise FileNotFoundError(f"requirements.txt file not found: {self._requirements_txt_path}")
        else:
            self._requirements_txt_path = (ALIRA_DIR / "modules" / self.name / "requirements.txt").resolve()
            if not self._requirements_txt_path.is_file():
                self._requirements_txt_path = None  # No requirements.txt file found, skipping


    async def install(self) -> bool:
        '''Install the module dependencies using uv.

        Install dependencies into the current Python environment using uv.
        Relative dependency paths are resolved from the requirements directory.

        Returns:
            bool: True if the module was successfully installed, False otherwise.
        '''
        if self._requirements_txt_path is None:
            return True

        logger.info(f"Installing dependencies for {self.name} from {self._requirements_txt_path}")

        try:
            process = await asyncio.create_subprocess_exec(
                "uv", "pip", "install",
                "--python", sys.executable,
                "--requirement", str(self._requirements_txt_path),
                cwd=self._requirements_txt_path.parent,
            )
        except OSError as exc:
            logger.error(f"Failed to launch dependency installation for {self.name}: {exc}")
            return False

        try:
            return_code = await process.wait()
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise

        if return_code != 0:
            logger.error(
                f"Dependency installation for {self.name} failed with exit code {return_code}"
            )
            return False
        return True

    async def startup(self):
        '''Start the module's services and perform any necessary startup operations.'''
        if self.logo_banner:
            print(self.logo_banner)

    async def shutdown(self):
        '''Stop the module's services and perform any necessary cleanup operations.'''
        raise NotImplementedError("Subclasses should implement this method.")

    async def uninstall(self):
        pass
