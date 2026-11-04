#!/usr/bin/env python3
"""Run the C++20 Orderbook Microbenchmark suite and print performance metrics."""
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "engine" / "build"


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    compiler = os.getenv("CXX", "g++")
    binary = BUILD / ("benchmark.exe" if sys.platform == "win32" else "benchmark")
    cmd = [
        compiler,
        "-std=c++20",
        "-O3",
        "-I", str(ROOT / "engine" / "include"),
        str(ROOT / "engine" / "src" / "orderbook.cpp"),
        str(ROOT / "engine" / "benchmarks" / "benchmark.cpp"),
        "-o", str(binary)
    ]
    print(f"Compiling microbenchmark harness with {compiler}...")
    subprocess.run(cmd, check=True)
    print("Executing benchmark suite...\n")
    subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
