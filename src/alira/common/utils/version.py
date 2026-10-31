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
    v1_parts = [int(x) for x in __version__.split('.')]
    v2_parts = [int(x) for x in v.split('.')]

    max_len = max(len(v1_parts), len(v2_parts))
    v1_parts += [0] * (max_len - len(v1_parts))
    v2_parts += [0] * (max_len - len(v2_parts))

    if v2_parts == v1_parts:
        return 0
    elif v2_parts < v1_parts:
        return -1
    else:
        return 1



# def print_logo():
#     teal = "\033[38;2;0;200;170m"
#     gray = "\033[38;2;140;140;140m"
#     reset = "\033[0m"

#     logo = r"""
#     ___    __  __________  ___
#    /   |  / /  \   _/ __ \/   |
#   / /| | / /   / / / /_/ / /| |
#  / ___ |/ /___/ /_/ _, _/ ___ |
# /_/  |_/____/____/_/ |_/_/  |_|
# """.strip("\n")

#     print(f"{teal}{logo}{reset}")
#     print(f"{gray}    Alira Agent  |  v{__version__}{reset}\n\n\n")


def print_logo():
    teal = "\033[38;2;0;200;170m"
    orange = "\033[38;2;255;140;0m"
    gray = "\033[38;2;140;140;140m"
    reset = "\033[0m"

    logo = [
        ("    ___    __  ______", "____  ___"),
        (r"   /   |  / /  \   _", r"/ __ \/   |"),
        ("  / /| | / /   / / ", "/ /_/ / /| |"),
        (" / ___ |/ /___/ /_", "/ _, _/ ___ |"),
        ("/_/  |_/____/____", "/_/ |_/_/  |_|"),
    ]

    for ali, ra in logo:
        print(f"{teal}{ali}{orange}{ra}{reset}")

    print(f"{gray}    Alira Agent  |  v{__version__}{reset}\n\n\n")
