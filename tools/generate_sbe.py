#!/usr/bin/env python3
"""Regenerate checked-in C++ codecs with the pinned upstream SBE tool."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

VERSION = "1.41.0"
SHA256 = "729b4271beb577f380380a1793e60f156ff75859b08ea603c4e2ae6b5e9a04ca"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "include/compute_trade/generated"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if checked-in codecs differ")
    parser.add_argument("--jar", type=Path, help="use a local copy of the pinned sbe-all jar")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="compute-trade-sbe-") as tmp:
        tmp = Path(tmp)
        jar = args.jar or tmp / f"sbe-all-{VERSION}.jar"
        if args.jar is None:
            urllib.request.urlretrieve(
                f"https://repo.maven.apache.org/maven2/uk/co/real-logic/sbe-all/{VERSION}/sbe-all-{VERSION}.jar",
                jar,
            )
        if hashlib.sha256(jar.read_bytes()).hexdigest() != SHA256:
            raise SystemExit("SBE generator SHA-256 mismatch")
        generated = tmp / "generated"
        subprocess.run([
            os.environ.get("JAVA", "java"),
            "--add-opens", "java.base/jdk.internal.misc=ALL-UNNAMED",
            "-Dsbe.target.language=Cpp",
            f"-Dsbe.output.dir={generated}",
            "-Dsbe.validation.stop.on.error=true",
            "-jar", str(jar.resolve()), str(ROOT / "schema/compute-trade.xml"),
        ], check=True)
        files = {p.relative_to(generated): p.read_bytes() for p in generated.rglob("*.h")}
        if not files:
            raise SystemExit("SBE generator produced no headers")
        if args.check:
            existing = {p.relative_to(OUTPUT): p.read_bytes() for p in OUTPUT.rglob("*.h")}
            if files != existing:
                raise SystemExit("SBE headers are stale; run python3 tools/generate_sbe.py")
            print("SBE headers match the schema and pinned generator")
        else:
            OUTPUT.mkdir(parents=True, exist_ok=True)
            for old in OUTPUT.rglob("*.h"):
                old.unlink()
            for name in files:
                dest = OUTPUT / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(generated / name, dest)
            print(f"Generated {len(files)} headers in {OUTPUT}")


if __name__ == "__main__":
    main()
