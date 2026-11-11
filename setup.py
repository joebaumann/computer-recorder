# Modified for the recorder-only fork: package only recorder dependencies.
from pathlib import Path

from setuptools import find_packages, setup

setup(
    name="ComputerRecorder",
    version="0.1.0",
    packages=find_packages(include=["crec", "crec.*"]),
    install_requires=[
        "pillow",
        "mss",
        "pynput",
        "shapely",
        "pyobjc-framework-Quartz; sys_platform == 'darwin'",
        "SQLAlchemy>=2.0.0",
        "pydantic>=2.0.0",
        "aiosqlite",
        "greenlet",
    ],
    entry_points={"console_scripts": ["crec=crec.cli:main"]},
    description="Local screenshots and mouse/keyboard recording for macOS studies",
    long_description=Path(__file__).with_name("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    url="https://github.com/joebaumann/computer-recorder",
    license="Apache-2.0",
    license_files=["LICENSE"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: MacOS :: MacOS X",
    ],
    python_requires=">=3.10",
)
