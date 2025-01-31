################################################################################
#
# Copyright (C) 2022-2025 Advanced Micro Devices, Inc. All rights reserved.
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

import functools
import glob
import itertools
import os
import shutil

from pathlib import Path
from timeit import default_timer as timer
from typing import NamedTuple, List, Optional, Sequence, Union

from Tensile import Utils
from Tensile.Toolchain.Assembly import AssemblyToolchain, buildAssemblyCodeObjectFiles
from Tensile.Toolchain.Source import SourceToolchain, buildSourceCodeObjectFiles
from Tensile.Toolchain.Validators import validateToolchain, getVersion, ToolchainDefaults
from Tensile.TensileInstructions import getGfxName, TensileInstructions
from Tensile.Common import globalParameters, HR, print1, print2, printExit, ensurePath, \
                    CHeader, assignGlobalParameters, architectureMap, IsaVersion, ParallelMap2
from Tensile.KernelWriterAssembly import KernelWriterAssembly
from Tensile.KernelWriterBase import KERNEL_HELPER_FILENAME_CPP, KERNEL_HELPER_FILENAME_H
from Tensile import LibraryIO
from Tensile.SolutionLibrary import MasterSolutionLibrary
from Tensile.SolutionStructs import Solution
from Tensile.CustomYamlLoader import load_logic_gfx_arch
from Tensile.Utilities.Profile import profile

from .ParseArguments import parseArguments

def timing(func):
  def wrapper(*args, **kwargs):
    start = timer()
    res = func(*args, **kwargs)
    end = timer()

    if globalParameters["PrintTiming"]:
      print(f'{func.__name__} took {end - start} seconds')

    return res
  return wrapper


class KernelCodeGenResult(NamedTuple):
    err: int
    src: str
    header: Optional[str]
    name: str
    targetObjFilename: str
    isa: IsaVersion
    wavefrontSize: int


def processKernelSource(kernelWriterAssembly, ti, kernel) -> KernelCodeGenResult:
    """
    Generate source for a single kernel.
    Returns (error, source, header, kernelName).
    """
    kernelWriter = kernelWriterAssembly
    kernelWriter.setTensileInstructions(ti)
    asmFilename = kernelWriter.getKernelFileBase(kernel)
    err, src = kernelWriter.getSourceFileString(kernel)
    header = kernelWriter.getHeaderFileString(kernel)
    objFilename = kernel._state.get("codeObjectFile", None)

    return KernelCodeGenResult(err, src, header, asmFilename, objFilename, tuple(kernel["ISA"]), kernel["WavefrontSize"])


def removeInvalidSolutionsAndKernels(results, kernels, solutions, errorTolerant, globalParameters):
    removeKernels = []
    removeKernelNames = []
    removeSolutions = []
    removeResults = []

    for kernIdx, r in Utils.tqdm(enumerate(results)) if globalParameters["PrintLevel"] > 1 else enumerate(results):
        if r.err != 0:
            if not errorTolerant:
                print("\nKernel generation failed for kernel: {}".format(kernels[kernIdx]["SolutionIndex"]))
                print(kernels[kernIdx]["SolutionNameMin"])
            removeKernels.append(kernels[kernIdx])
            kName = Solution.getKeyNoInternalArgs(kernels[kernIdx])
            if kName not in removeKernelNames:
                removeKernelNames.append(kName)
            removeResults.append(results[kernIdx])

    if len(removeKernels) > 0 and not errorTolerant:
        printExit("** kernel generation failure **")

    for kern in removeKernels:
        kernels.remove(kern)

    for solution in Utils.tqdm(solutions, "Finding invalid solutions") if globalParameters["PrintLevel"] > 1 else solutions:
        solutionKernels = solution.getKernels()
        for kernel in solutionKernels:
            kName = Solution.getKeyNoInternalArgs(kernel)
            if kName in removeKernelNames:
                removeSolutions.append(solution)
                break

    for solut in removeSolutions:
        solutions.remove(solut)

    for rel in removeResults:
        results.remove(rel)


def writeAssembly(asmPath: Union[Path, str], result: KernelCodeGenResult):
    if result.err:
      printExit(f"Failed to build kernel {result.name} because it has error code {result.err}")
    path = Path(asmPath) / f"{result.name}.s"
    isa =  result.isa
    wfsize = result.wavefrontSize
    with open(path, "w", encoding="utf-8") as f:
      f.write(result.src)

    # result.src is very large so let garbage collector know to clean up
    del result

    return path, isa, wfsize


def writeHelpers(outputPath, kernelHelperObjs, KERNEL_HELPER_FILENAME_CPP, KERNEL_HELPER_FILENAME_H):
    kernelSourceFilename = os.path.join(os.path.normcase(outputPath), KERNEL_HELPER_FILENAME_CPP)
    kernelHeaderFilename = os.path.join(os.path.normcase(outputPath), KERNEL_HELPER_FILENAME_H)

    with open(kernelHeaderFilename, "w", encoding="utf-8") as kernelHeaderFile, \
          open(kernelSourceFilename, "w", encoding="utf-8") as kernelSourceFile:
        kernelSourceFile.write(CHeader)
        kernelHeaderFile.write(CHeader)
        kernelSourceFile.write("#include \"Kernels.h\"\n")
        kernelHeaderFile.write("#pragma once\n")
        if globalParameters["RuntimeLanguage"] == "HIP":
          kernelHeaderFile.write("#include <hip/hip_runtime.h>\n")
          kernelHeaderFile.write("#include <hip/hip_ext.h>\n\n")
        kernelHeaderFile.write("#include \"KernelHeader.h\"\n\n")
        HeaderText = ""
        for ko in kernelHelperObjs:
            kernelName = ko.getKernelName()
            (err, src) = ko.getSourceFileString()
            kernelSourceFile.write(src)
            if err:
                print("*** warning: invalid kernel#%u" % kernelName)
            HeaderText += ko.getHeaderFileString()
        kernelHeaderFile.write(HeaderText)


def writeSolutionsAndKernels(outputPath, asmToolchain, srcToolchain, solutions, kernels, kernelHelperObjs, \
    kernelWriterAssembly, errorTolerant=False, generateSourcesAndExit=False, compress=True, fromTensile=False):
  codeObjectFiles = []

  outputPath = Path(outputPath)
  destLibPath = ensurePath(outputPath / "library")  # Destination for code object library files (.co)
  buildTmpPath = ensurePath(outputPath / "build_tmp" / outputPath.stem.upper())  #
  assemblyTmpPath = ensurePath(buildTmpPath / "assembly")  # Temp path for generated assembly files (.s)
  objectTmpPath = ensurePath(buildTmpPath / "code_object_tmp")  # Temp path for HSA code object files (.hsaco)

  asmKernels = [k for k in kernels if k['KernelLanguage'] == 'Assembly']

  visited = set()
  duplicates = 0
  for k in asmKernels:
    base = kernelWriterAssembly.getKernelFileBase(k)
    k.duplicate = True if base in visited else False
    duplicates += k.duplicate
    print2(f"Duplicate: {base}")
    visited.add(base)
  print1(f"Number of duplicates: {duplicates}")

  numAsmKernels = len(asmKernels)
  numKernels = len(asmKernels)
  assert numKernels == numAsmKernels, "Only assembly kernels are supported in TensileLite"
  asmIter   = zip(itertools.repeat(kernelWriterAssembly), itertools.repeat(TensileInstructions()), asmKernels)
  asmResults = ParallelMap2(processKernelSource, asmIter, "Generating assembly kernels")
  removeInvalidSolutionsAndKernels(asmResults, asmKernels, solutions, errorTolerant, globalParameters)
  def assemble(ret):
    p, isa, wavefrontsize = ret
    asmToolchain.assemble(str(p), str(p.with_suffix(".o")), getGfxName(isa), wavefrontsize)
  unaryWriteAssembly = functools.partial(writeAssembly, assemblyTmpPath)
  compose = lambda *F: functools.reduce(lambda f, g: lambda x: f(g(x)), F)
  ret = ParallelMap2(compose(assemble, unaryWriteAssembly), asmResults, "Writing assembly kernels", return_as="list", multiArg=False)

  writeHelpers(outputPath, kernelHelperObjs, KERNEL_HELPER_FILENAME_CPP, KERNEL_HELPER_FILENAME_H)
  srcKernelFile = Path(outputPath) / "Kernels.cpp"

  if not generateSourcesAndExit:
      codeObjectFiles += buildAssemblyCodeObjectFiles(asmToolchain, asmKernels, kernelWriterAssembly, destLibPath, assemblyTmpPath, compress)
      buildSourceCodeObjectFiles(srcToolchain, destLibPath, objectTmpPath, outputPath, srcKernelFile, fromTensile)

  return codeObjectFiles, numKernels


def writeSolutionsAndKernelsTCL(outputPath, asmToolchain, srcToolchain, kernels, kernelHelperObjs, \
    kernelWriterAssembly, compress=True, fromTensile=False):

  outputPath = Path(outputPath)
  destLibPath = ensurePath(outputPath / "library")  # Destination for code object library files (.co)
  buildTmpPath = ensurePath(outputPath / "build_tmp" / outputPath.stem.upper())
  assemblyTmpPath = ensurePath(buildTmpPath / "assembly")  # Temp path for generated assembly files (.s)
  objectTmpPath = ensurePath(buildTmpPath / "code_object_tmp")  # Temp path for HSA code object files (.hsaco)

  asmKernels = [k for k in kernels if k['KernelLanguage'] == 'Assembly']

  visited = set()
  duplicates = 0
  for k in asmKernels:
    base = kernelWriterAssembly.getKernelFileBase(k)
    k.duplicate = True if base in visited else False
    duplicates += k.duplicate
    print2(f"Duplicate: {base}")
    visited.add(base)
  print1(f"Number of duplicates: {duplicates}")

  uniqueAsmKernels = [k for k in asmKernels if not k.duplicate]
  numAsmKernels = len(asmKernels)
  numKernels = len(kernels)
  assert numKernels == numAsmKernels, "Only assembly kernels are supported in TensileLite"
  def assemble(ret):
    p, isa, wavefrontsize = ret
    asmToolchain.assemble(str(p), str(p.with_suffix(".o")), getGfxName(isa), wavefrontsize)
  unaryProcessKernelSource = functools.partial(processKernelSource, kernelWriterAssembly, TensileInstructions())
  unaryWriteAssembly = functools.partial(writeAssembly, assemblyTmpPath)
  compose = lambda *F: functools.reduce(lambda f, g: lambda x: f(g(x)), F)
  ret = ParallelMap2(compose(assemble, unaryWriteAssembly, unaryProcessKernelSource), uniqueAsmKernels, "Generating assembly kernels", multiArg=False)
  buildAssemblyCodeObjectFiles(asmToolchain, asmKernels, kernelWriterAssembly, destLibPath, assemblyTmpPath, compress)

  writeHelpers(outputPath, kernelHelperObjs, KERNEL_HELPER_FILENAME_CPP, KERNEL_HELPER_FILENAME_H)
  srcKernelFile = Path(outputPath) / "Kernels.cpp"
  buildSourceCodeObjectFiles(srcToolchain, destLibPath, objectTmpPath, outputPath, srcKernelFile, fromTensile)

  return numKernels


@timing
def getSolutionAndKernelWriters(solutions, kernels, assembler):

  kernelSerialNaming   = Solution.getSerialNaming(kernels)
  solutionMinNaming    = Solution.getMinNaming(solutions)
  kernelMinNaming      = Solution.getMinNaming(kernels)
  kernelWriterAssembly = KernelWriterAssembly(kernelMinNaming, kernelSerialNaming, assembler)

  return (kernelWriterAssembly, kernelMinNaming, solutionMinNaming)


@timing
def copyStaticFiles(outputPath):
  libraryStaticFiles = [
    "TensileTypes.h",
    "tensile_bfloat16.h",
    "tensile_float8_bfloat8.h",
    "tensile_float8_bfloat8_bc.h",
    "KernelHeader.h",
    "ReductionTemplate.h",
    "memory_gfx.h" ]

  for fileName in libraryStaticFiles:
    shutil.copy(os.path.join(globalParameters["SourcePath"], fileName), outputPath)

  return libraryStaticFiles


@timing
def generateKernelObjectsFromSolutions(solutions):
  kernels = []
  kernelHelperObjs = []
  kernelNames = set()
  kernelHelperNames = set()

  for solution in solutions:
    solutionKernels = solution.getKernels()
    for kernel in solutionKernels:
        kName = Solution.getKeyNoInternalArgs(kernel)
        if kName not in kernelNames:
            kernels.append(kernel)
            kernelNames.add(kName)
    solutionHelperKernels = solution.getHelperKernelObjects()
    kernelHelperObjs += solutionHelperKernels
    for ko in solutionHelperKernels:
      kernelHelperNames.add(ko.getKernelName())

  # remove duplicates while preserving order
  kernelHelperObjs = list(dict.fromkeys(kernelHelperObjs))
  return (kernels, kernelHelperObjs, kernelHelperNames)


@timing
def generateLogicDataAndSolutions(logicFiles, args, cxxCompiler):

  if ";" in args["Architecture"]:
    archs = args["Architecture"].split(";") # user arg list format
  else:
    archs = args["Architecture"].split("_") # workaround for cmake list in list issue

  solutions = []
  masterLibraries = {}
  nextSolIndex = 0

  fIter = zip(logicFiles, itertools.repeat(cxxCompiler), itertools.repeat(archs))

  def libraryIter(lib: MasterSolutionLibrary):
    if len(lib.solutions):
      for i, s in enumerate(lib.solutions.items()):
        yield (i, *s)
    else:
      for _, lazyLib in lib.lazyLibraries.items():
        yield from libraryIter(lazyLib)

  for library in ParallelMap2(LibraryIO.parseLibraryLogicFile, fIter, "Loading Logics...", return_as="generator_unordered"):
    _, architectureName, _, _, _, newLibrary = library

    if architectureName == "":
      continue

    if architectureName in masterLibraries:
      nextSolIndex = masterLibraries[architectureName].merge(newLibrary, nextSolIndex)
    else:
      masterLibraries[architectureName] = newLibrary
      masterLibraries[architectureName].version = args["CodeObjectVersion"]

  # Sort masterLibraries to make global soln index values deterministic
  solnReIndex=0
  masterLibraries = dict(sorted(masterLibraries.items()))
  for k,v in masterLibraries.items():
    for _, masterLibrary in masterLibraries.items():
      for _, sol in masterLibrary.solutions.items():
        sol.index = solnReIndex
        solnReIndex += 1
      # Sort masterLibrary to make global soln index values deterministic
      masterLibrary.lazyLibraries = dict(sorted(masterLibrary.lazyLibraries.items()))
      for name, lib in masterLibrary.lazyLibraries.items():
        # Sort solns by the lib logic file they were generated from
        lib.solutions = {k: lib.solutions[k] for k in sorted(lib.solutions, key = lambda idx: lib.solutions[idx].srcName )}
        for _, sol in lib.solutions.items():
          sol.index = solnReIndex
          solnReIndex += 1

  if args["GenSolTable"]:
    matchTable = {}
    # Match yaml file solutions to solution index
    for _,masterLibrary in masterLibraries.items():
      for localIdx, _, s in libraryIter(masterLibrary):
        matchTable[s.index] = [s.srcName, localIdx]
    LibraryIO.write("MatchTable", matchTable)

  if "fallback" in masterLibraries.keys():
    for key, value in masterLibraries.items():
      if key != "fallback":
        value.merge(masterLibraries["fallback"])
    masterLibraries.pop("fallback")
  for _, masterLibrary in masterLibraries.items():
    for _, sol in masterLibrary.solutions.items():
      solutions.append(sol.originalSolution)
    for name, lib in masterLibrary.lazyLibraries.items():
      for _, sol in lib.solutions.items():
        sol.originalSolution._state["codeObjectFile"] = name
        solutions.append(sol.originalSolution)

  # remove duplicates while preserving order
  solutions = dict.fromkeys(solutions).keys()

  return solutions, masterLibraries


################################################################################
# Tensile Create Library
################################################################################
@profile
def run():
  start = timer()
  print1("")
  print1(HR)
  print1("# Tensile Create Library")
  print2(HR)
  print2("")

  arguments = parseArguments()
  outputPath = Path(ensurePath(os.path.abspath(arguments["OutputPath"])))

  cxxCompiler, cCompiler, offloadBundler, assembler, hipconfig = validateToolchain(
      arguments["CxxCompiler"], arguments["CCompiler"], arguments["OffloadBundler"], arguments["Assembler"], ToolchainDefaults.HIP_CONFIG
  )

  print1(f"# HIP Version:         {getVersion(hipconfig, regex=r'(.+)')}")
  print1(f"# Cxx Compiler:        {cxxCompiler} (version {getVersion(cxxCompiler)})")
  print1(f"# C Compiler:          {cCompiler} (version {getVersion(cCompiler)})")
  print1(f"# Assembler:           {assembler} (version {getVersion(assembler)})")
  print1(f"# Offload Bundler:     {offloadBundler} (version {getVersion(offloadBundler)})")
  print1(f"# Code Object Version: {arguments['CodeObjectVersion']}")
  print1(f"# Architecture(s):     {arguments['Architecture']}")
  print1(f"# Library Format:      {arguments['LibraryFormat']}")

  assignGlobalParameters(arguments, cxxCompiler)

  asmToolchain = AssemblyToolchain(assembler, offloadBundler, globalParameters["BuildIdKind"], arguments["CodeObjectVersion"])
  srcToolchain = SourceToolchain(cxxCompiler, offloadBundler, globalParameters["BuildIdKind"], globalParameters["AsanBuild"], globalParameters["SaveTemps"])

  if not os.path.exists(arguments["LogicPath"]):
    printExit(f"LogicPath {arguments['LogicPath']} doesn't exist")

  if ";" in arguments["Architecture"]:
    archs = arguments["Architecture"].split(";")
  else:
    archs = arguments["Architecture"].split("_")
  logicArchs = set()
  for arch in archs:
    if arch in architectureMap:
      logicArchs.add(architectureMap[arch])
    else:
      printExit("Architecture %s not supported" % arch)

  logicExtFormat = ".yaml"
  if arguments["LogicFormat"] == "yaml":
    pass
  elif arguments["LogicFormat"] == "json":
    logicExtFormat = ".json"
  else:
    printExit(f"Unrecognized LogicFormat: {arguments['LogicFormat']}")

  def archMatch(arch: str, archs: List[str]):
    return (arch in archs) or any(a.startswith(arch) for a in archs)

  def validLogicFile(p: Path):
    return p.suffix == logicExtFormat and ("all" in archs or archMatch(load_logic_gfx_arch(p), archs))

  globPattern = os.path.join(arguments["LogicPath"], f"**/{arguments['LogicFilter']}{logicExtFormat}")
  print1(f"# LogicFilter:       {globPattern}")
  logicFiles = (os.path.join(arguments["LogicPath"], file) for file in glob.iglob(globPattern, recursive=True))
  logicFiles = [file for file in logicFiles if validLogicFile(Path(file))]

  print1(f"# Experimental:      {arguments['Experimental']}")
  if not arguments["Experimental"]:
    logicFiles = [file for file in logicFiles if "experimental" not in map(str.lower, Path(file).parts)]

  print2(f"# LibraryLogicFiles: {len(logicFiles)}")
  for logicFile in logicFiles:
    print2("#   %s" % logicFile)

  solutions, masterLibraries = generateLogicDataAndSolutions(logicFiles, arguments, cxxCompiler)
  kernels, kernelHelperObjs, _ = generateKernelObjectsFromSolutions(solutions)
  kernelWriterAssembly, kernelMinNaming, _ = getSolutionAndKernelWriters(solutions, kernels, assembler)

  copyStaticFiles(outputPath)

  numKernels = writeSolutionsAndKernelsTCL(outputPath, asmToolchain, srcToolchain, kernels,
                                           kernelHelperObjs, kernelWriterAssembly, compress=arguments["UseCompression"])

  archs = [getGfxName(arch) for arch in globalParameters['SupportedISA'] \
             if globalParameters["AsmCaps"][arch]["SupportedISA"]]
  newLibraryDir = ensurePath(os.path.join(outputPath, 'library'))

  for archName, newMasterLibrary in masterLibraries.items():
    if archName in archs:
      if globalParameters["LazyLibraryLoading"]:
        masterFile = os.path.join(newLibraryDir, "TensileLibrary_lazy_"+archName)
      else:
        masterFile = os.path.join(newLibraryDir, "TensileLibrary_"+archName)
      newMasterLibrary.applyNaming(kernelMinNaming)
      LibraryIO.write(masterFile, Utils.state(newMasterLibrary), arguments["LibraryFormat"])
      for name, lib in newMasterLibrary.lazyLibraries.items():
        filename = os.path.join(newLibraryDir, name)
        lib.applyNaming(kernelMinNaming)
        LibraryIO.write(filename, Utils.state(lib), arguments["LibraryFormat"])

  if not globalParameters["KeepBuildTmp"]:
    buildTmp = Path(arguments["OutputPath"]).parent / "library" / "build_tmp"
    if buildTmp.exists() and buildTmp.is_dir():
      shutil.rmtree(buildTmp)
    buildTmp = Path(arguments["OutputPath"]) / "build_tmp"
    if buildTmp.exists() and buildTmp.is_dir():
      shutil.rmtree(buildTmp)
    else:
      printWarning(f"Cannot remove build_tmp")

  print1("# Tensile Library Writer DONE")
  print1(HR)
  print1("")

  stop = timer()

  print1(f"Total time (s): {(stop-start):3.2f}")
  print1(f"Total kernels processed: {numKernels}")
  print1(f"Kernels processed per second: {(numKernels/(stop-start)):3.2f}")
