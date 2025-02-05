################################################################################
#
# Copyright (C) 2025 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
################################################################################

import os
import re
from pathlib import Path
from typing import List, NamedTuple, Union
from subprocess import run, PIPE

ROCM_BIN_PATH = Path("/opt/rocm/bin")
ROCM_LLVM_BIN_PATH = Path("/opt/rocm/lib/llvm/bin")

if os.name == "nt":
    def _windowsLatestRocmBin(path: Union[Path, str]) -> Path:
        """Get the path to the latest ROCm bin directory, on Windows.

        This function assumes that ROCm versions are differentiated with the form ``X.Y``.

        Args:
            path: The path to the ROCm root directory, typically ``C:/Program Files/AMD/ROCm``.

        Returns:
            The path to the ROCm bin directory for the latest ROCm version.
            Typically of the form ``C:/Program Files/AMD/ROCm/X.Y/bin``.
        """
        path = Path(path)
        pattern = re.compile(r"^\d+\.\d+$")
        versions = filter(lambda d: d.is_dir() and pattern.match(d.name), path.iterdir())
        latest = max(versions, key=lambda d: tuple(map(int, d.name.split("."))))
        return latest / "bin"
    # LLVM binaries are in the same directory as ROCm binaries on Windows
    ROCM_BIN_PATH = _windowsLatestRocmBin("C:/Program Files/AMD/ROCm")
    ROCM_LLVM_BIN_PATH = _windowsLatestRocmBin("C:/Program Files/AMD/ROCm")


osSelect = lambda linux, windows: linux if os.name != "nt" else windows


class ToolchainDefaults(NamedTuple):
    CXX_COMPILER= osSelect(linux="amdclang++", windows="clang++.exe")
    C_COMPILER= osSelect(linux="amdclang", windows="clang.exe")
    OFFLOAD_BUNDLER= osSelect(linux="clang-offload-bundler", windows="clang-offload-bundler.exe")
    ASSEMBLER = osSelect(linux="amdclang++", windows="clang++.exe")
    HIP_CONFIG = osSelect(linux="hipconfig", windows="hipconfig")


def _supportedComponent(component: str, targets: List[str]) -> bool:
    isSupported = any([component == t for t in targets]) or any([Path(component).name == t for t in targets])
    return isSupported


def supportedCCompiler(compiler: str) -> bool:
    """Determine if a C compiler/assembler is supported by Tensile.

    Args:
        compiler: The name of a compiler to test for support.

    Return:
        If supported True; otherwise, False.
    """
    return _supportedComponent(compiler, [ToolchainDefaults.C_COMPILER])


def supportedCxxCompiler(compiler: str) -> bool:
    """Determine if a C++/HIP compiler/assembler is supported by Tensile.

    Args:
        compiler: The name of a compiler to test for support.

    Return:
        If supported True; otherwise, False.
    """
    return _supportedComponent(compiler, [ToolchainDefaults.CXX_COMPILER])


def supportedOffloadBundler(bundler: str) -> bool:
    """Determine if an offload bundler is supported by Tensile.

    Args:
        bundler: The name of an offload bundler to test for support.

    Return:
        If supported True; otherwise, False.
    """
    return _supportedComponent(bundler, [ToolchainDefaults.OFFLOAD_BUNDLER])


def supportedHip(smi: str) -> bool:
    """Determine if an offload bundler is supported by Tensile.

    Args:
        bundler: The name of an offload bundler to test for support.

    Return:
        If supported True; otherwise, False.
    """
    return _supportedComponent(smi, [ToolchainDefaults.HIP_CONFIG])


def _exeExists(file: Path) -> bool:
    """Check if a file exists and is executable.

    Args:
        file: The file to check.

    Returns:
        If the file exists and is executable, True; otherwise, False
    """
    return True if os.access(file, os.X_OK) else False


def _validateExecutable(file: str, searchPaths: List[Path]) -> str:
    """Validate that the given toolchain component is in the PATH and executable.

    Args:
        file: The executable to validate.
        searchPaths: List of directories to search for the executable.

    Returns:
        The validated executable with an absolute path.
    """
    if not any((
        supportedCxxCompiler(file), supportedCCompiler(file), supportedOffloadBundler(file), supportedHip(file)
    )):
        raise ValueError(f"{file} is not a supported toolchain component for OS: {os.name}")

    if _exeExists(Path(file)): return file
    for path in searchPaths:
        path /= file
        if _exeExists(path): return str(path)
    raise FileNotFoundError(f"`{file}` either not found or not executable in any search path: {':'.join(map(str, searchPaths))}")


def validateToolchain(*args: str):
    """Validate that the given toolchain components are in the PATH and executable.

    Args:
        args: List of executable toolchain components to validate.

    Returns:
        List of validated executables with absolute paths.

    Raises:
        ValueError: If no toolchain components are provided.
        FileNotFoundError: If a toolchain component is not found in the PATH.
    """
    if not args:
        raise ValueError("No toolchain components to validate, at least one argument is required")

    searchPaths = [
        ROCM_BIN_PATH,
        ROCM_LLVM_BIN_PATH,
    ] + [Path(p) for p in os.environ["PATH"].split(os.pathsep)]

    out = (_validateExecutable(x, searchPaths) for x in args)
    return next(out) if len(args) == 1 else tuple(out)


def getVersion(executable: str, versionFlag: str="--version", regex: str=r"version\s+([\d.]+)") -> str:
    """Print the version of a toolchain component.

    Args:
        executable: The toolchain component to check the version of.
        versionFlag: The flag to pass to the executable to get the version.
    """
    args = f'"{executable}" "{versionFlag}"'
    try:
        output = run(args, stdout=PIPE, shell=True).stdout.decode().strip()
        match = re.search(regex, output, re.IGNORECASE)
        return match.group(1) if match else "<unknown>"
    except Exception as e:
        raise RuntimeError(f"Failed to get version when calling {args}: {e}")
