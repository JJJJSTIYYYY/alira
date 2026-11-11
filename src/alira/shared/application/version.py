"""Alira version comparison and startup banner rendering."""

from packaging.version import Version

from alira._version import __version__


def compare_version(v: str) -> int:
    """
    Compare the given version string with the current system version.

    Args:
        v: The version string to compare (e.g., "1.2.3").

    Returns:
        int:
            - 0  if ``v`` equals the current version (``__version__``).
            - -1 if ``v`` is older (less than the current version).
            - 1  if ``v`` is newer (greater than the current version).
    """
    current, candidate = Version(__version__), Version(v)
    return (candidate > current) - (candidate < current)


def print_logo():
    teal = "\033[38;2;0;200;170m"
    orange = "\033[38;2;255;140;0m"
    orange_background = "\033[48;2;255;140;0m"
    gray = "\033[38;2;140;140;140m"
    reset = "\033[0m"
    apixis_link = "\033]8;;https://github.com/JJJJSTIYYYY/apixis\033\\apixis\033]8;;\033\\"

    logo = [
        ("    ___    __  ______", "____  ___"),
        (r"   /   |  / /  \   _", r"/ __ \/   |"),
        ("  / /| | / /   / / ", "/ /_/ / /| |"),
        (" / ___ |/ /___/ /_", "/ _, _/ ___ |"),
        ("/_/  |_/____/____", "/_/ |_/_/  |_|"),
    ]

    print("")
    for index, (ali, ra) in enumerate(logo):
        version_tag = (
            f"  {orange_background}{teal} v{__version__} {reset}"
            if index == 0 else ""
        )
        print(f"{teal}{ali}{orange}{ra}{reset}{version_tag}")
    print(f"{gray}"+("-" * 31)+f"{reset}")
    print(f"{gray}Alira Harness powered by {apixis_link}{reset}")
    print(f"{gray}"+("-" * 31)+f"{reset}\n")
