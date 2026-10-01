"""Reject incomplete source checkouts before building a distribution."""
from pathlib import Path
from setuptools import setup

root = Path(__file__).parent
modules = ("molet_interface", "molet_auxiliary", "molet_tools", "molet_examples")
missing = [f"{name}.py" for name in modules if not (root / f"{name}.py").is_file()]
if missing:
    raise RuntimeError(
        "Missing interface source modules: " + ", ".join(missing)
        + ". Add the source modules to the repository root before installing."
    )

setup()
