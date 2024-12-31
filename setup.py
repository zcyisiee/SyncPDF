"""Setup script for yadt, to compile yadt._core.
"""

from __future__ import annotations

import sys
from glob import glob
from itertools import chain
from os import environ
from os.path import exists, join
from platform import machine
from typing import cast

from pybind11.setup_helpers import ParallelCompile, Pybind11Extension, build_ext
from setuptools import Extension, setup

extra_includes = ['/opt/homebrew/include']
extra_library_dirs = ["/usr/local/lib", '/opt/homebrew/lib']

# Use cast because mypy has trouble seeing Pybind11Extension is a subclass of
# Extension.
extmodule: Extension = cast(
    Extension,
    Pybind11Extension(
        'yadt._core',
        sources=sorted(glob('src/cbinding/*.cc')),
        depends=sorted(glob('src/cbinding/*.h')),
        include_dirs=[
            # Path to pybind11 headers
            *extra_includes,
        ],
        library_dirs=[*extra_library_dirs],
        libraries=['qpdf'],
        extra_link_args=[f'-Wl,-rpath,{lib}' for lib in extra_library_dirs],
        cxx_std=17,
    ),
)


if __name__ == '__main__':
    with ParallelCompile('PIKEPDF_NUM_BUILD_JOBS'):  # optional envvar
        setup(
            ext_modules=[extmodule],
            cmdclass={'build_ext': build_ext},  # type: ignore
            package_dir={"": "src"},
        )