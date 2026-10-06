"""Suite-wide: say which child containment this run exercises (#2)."""

from processor._contain import REQUIRED_ABI, landlock_abi, strongest_available


def pytest_report_header() -> str:
    """``required`` runs every child contained; ``off`` skips the containment tests."""
    abi, mode = landlock_abi(), strongest_available()
    if mode == "required":
        return f"child containment: required (Landlock ABI {abi})"
    return (
        f"child containment: off (Landlock ABI {abi} < {REQUIRED_ABI}, or no seccomp table "
        "for this arch): the containment tests are skipped"
    )
