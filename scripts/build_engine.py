import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "engine" / "build"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    compiler = os.getenv("CXX", "g++")
    suffix = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    library = BUILD / ("polypulse" + suffix)
    flags = ["-static"] if sys.platform == "win32" else []
    if sys.platform == "win32" and library.exists():
        old_lib = library.with_name("polypulse_old" + suffix)
        try:
            if old_lib.exists():
                try: old_lib.unlink()
                except Exception: pass
            library.rename(old_lib)
        except Exception:
            pass
    try:
        subprocess.run([compiler, "-std=c++20", "-O2", "-shared", "-fPIC", *flags, "-I", str(ROOT / "engine/include"), str(ROOT / "engine/src/orderbook.cpp"), "-o", str(library)], check=True)
        print(library)
    except subprocess.CalledProcessError as exc:
        if sys.platform == "win32" and library.exists():
            print(f"Notice: {library} is currently locked by a running process. Existing compiled library retained.")
        else:
            raise exc

    if args.test:
        binary = BUILD / ("orderbook_test.exe" if sys.platform == "win32" else "orderbook_test")
        subprocess.run([compiler, "-std=c++20", "-O2", "-I", str(ROOT / "engine/include"), str(ROOT / "engine/src/orderbook.cpp"), str(ROOT / "engine/tests/orderbook_test.cpp"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        print("C++ tests passed")


if __name__ == "__main__":
    main()
