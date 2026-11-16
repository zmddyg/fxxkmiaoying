"""Build a CloudBase upload ZIP with a self-contained Database SDK."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
FUNCTION = ROOT / "function"
FILES = ("index.js", "miaoying.js", "ledger.js", "workflow.js", "plan.json", "black.jpg")
OUTPUT = ROOT / "秒应-CloudBase云函数.zip"


def third_party_notices(lock):
    for module, data in sorted(lock["packages"].items()):
        if not module:
            continue
        for path in sorted((FUNCTION / module).iterdir()):
            if path.is_file() and re.match(r"^(license|copying|notice)(\b|[-_.])", path.name, re.I):
                yield f"{module.removeprefix('node_modules/')} {data.get('version', '')} | {path.name}\n"
                yield path.read_text(encoding="utf-8", errors="replace").strip() + "\n\n"


def main():
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise SystemExit("需要 Node.js 和 npm 来重新打包。")
    lock = json.loads((FUNCTION / "package-lock.json").read_text(encoding="utf-8"))
    sdk_module = "node_modules/@cloudbase/node-sdk"
    sdk = FUNCTION / sdk_module
    if not (sdk / "dist" / "index.js").exists():
        subprocess.run([npm, "ci", "--ignore-scripts", "--omit=dev", "--no-fund"], cwd=FUNCTION, check=True)
    with tempfile.TemporaryDirectory(prefix="miaoying-sdk-") as temp:
        bundle = Path(temp) / "cloudbase-sdk.js"
        subprocess.run([npm, "exec", "--yes", "--package", "esbuild@0.27.1", "--", "esbuild",
            str(FUNCTION / "cloudbase-sdk.js"), "--bundle", "--platform=node", "--format=cjs", "--target=node20",
            "--minify", "--legal-comments=eof", f"--outfile={bundle}"], cwd=ROOT, check=True)
        metadata = json.loads((FUNCTION / "package.json").read_text(encoding="utf-8"))
        metadata.pop("dependencies", None)
        with ZipFile(OUTPUT, "w", ZIP_DEFLATED, compresslevel=9) as archive:
            for name in FILES:
                archive.write(FUNCTION / name, name)
            archive.write(bundle, "cloudbase-sdk.js")
            archive.writestr("package.json", json.dumps(metadata))
            archive.writestr("THIRD_PARTY_LICENSES.txt", "".join(third_party_notices(lock)))
    print(json.dumps({"bytes": OUTPUT.stat().st_size, "file": OUTPUT.name}, ensure_ascii=False))


if __name__ == "__main__":
    main()
