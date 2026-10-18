# -*- coding: utf-8 -*-
"""随包 OCR 运行时的来源固定、清单校验与复制。

为什么单独一个模块：Tesseract 不是 pip 包，发行时必须把官方安装器里的
`tesseract.exe`、`libtesseract-5.dll` 和它的传递 DLL 一起带走，用户不再需要
第二个 OCR 安装器。清单 `scripts/ocr-runtime-manifest.json` 记录固定安装器
URL/SHA-256 和逐文件摘要；构建时只接受逐字节匹配清单的运行时。

一次性生成清单（需要 7-Zip 解 NSIS 安装器）：

    python scripts/ocr_runtime.py prepare \
        --installer .temp/tools/tesseract-setup.exe \
        --sevenzip .temp/tools/7zip/7z.exe \
        --output .temp/ocr-runtime
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "scripts" / "ocr-runtime-manifest.json"
DEFAULT_RUNTIME = ROOT / ".temp" / "ocr-runtime"
MODEL_SOURCE = ROOT / "app" / "recognition" / "models" / "eng.traineddata"
MODEL_PROVENANCE = ROOT / "app" / "recognition" / "models" / "provenance.json"

INSTALLER_NAME = "tesseract-ocr-w64-setup-5.5.3.20260724.exe"
INSTALLER_URL = ("https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/"
                 + INSTALLER_NAME)
INSTALLER_SHA256 = "bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4"


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest(path=MANIFEST_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(runtime, manifest=None) -> dict:
    """逐文件核对大小与 SHA-256；返回清单本身，任何缺失/改动都抛 ValueError。"""
    manifest = manifest or load_manifest()
    runtime = Path(runtime)
    if not runtime.is_dir():
        raise ValueError(f"OCR 运行时目录不存在：{runtime}")
    expected = {row["path"]: row for row in manifest["files"]}
    actual = {path.relative_to(runtime).as_posix() for path in runtime.rglob("*") if path.is_file()}
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    if missing:
        raise ValueError("OCR 运行时缺文件：" + ", ".join(missing[:8]))
    if extra:
        raise ValueError("OCR 运行时含清单外文件：" + ", ".join(extra[:8]))
    for name, row in expected.items():
        body = (runtime / name).read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"OCR 运行时文件校验失败：{name}")
    return manifest


def copy_into(bundle, runtime=None, manifest=None) -> list:
    """把已验证的运行时复制成 `<bundle>/ocr/`；返回复制的相对路径列表。"""
    runtime = Path(runtime or DEFAULT_RUNTIME)
    manifest = validate(runtime, manifest)
    target = Path(bundle) / "ocr"
    copied = []
    for row in manifest["files"]:
        destination = target / row["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime / row["path"], destination)
        copied.append(row["path"])
    return copied


def _import_closure(extracted: Path) -> list:
    """按 PE 导入表求 tesseract.exe 的传递 DLL 闭包（只保留安装器里存在的文件）。"""
    import pefile

    available = {path.name.casefold(): path for path in extracted.iterdir() if path.is_file()}
    pending = [available["tesseract.exe"]]
    wanted = set()
    while pending:
        path = pending.pop()
        key = path.name.casefold()
        if key in wanted:
            continue
        wanted.add(key)
        pe = pefile.PE(str(path), fast_load=True)
        try:
            pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ])
            entries = list(getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or [])
            entries += list(getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []) or [])
            for entry in entries:
                name = entry.dll.decode("ascii", "replace").casefold()
                if name in available:
                    pending.append(available[name])
        finally:
            pe.close()
    return sorted(available[key] for key in wanted if available[key].is_file())


def prepare(installer, sevenzip, output) -> dict:
    """解开发布安装器、生成运行时目录与清单（开发机一次性动作）。"""
    import tempfile

    installer, output = Path(installer), Path(output)
    if output.exists():
        raise ValueError("目标目录已存在；请用 verify 校验，或明确指定一个新目录")
    output.parent.mkdir(parents=True, exist_ok=True)
    if digest(installer) != INSTALLER_SHA256:
        raise ValueError("OCR 安装器校验失败：不是清单固定的官方文件")
    if not MODEL_SOURCE.is_file():
        raise ValueError("缺少 app/recognition/models/eng.traineddata")
    model_sha = digest(MODEL_SOURCE)
    provenance = json.loads(MODEL_PROVENANCE.read_text(encoding="utf-8"))
    if provenance["files"]["eng.traineddata"]["sha256"] != model_sha:
        raise ValueError("eng.traineddata 与模型 provenance.json 记录不一致")
    with tempfile.TemporaryDirectory(prefix="ocr-extract-", dir=output.parent) as temporary:
        extracted = Path(temporary)
        subprocess.run([str(sevenzip), "x", str(installer), f"-o{extracted}", "-y"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        files = _import_closure(extracted)
        output.mkdir(parents=True)
        for path in files:
            shutil.copy2(path, output / path.name)
        (output / "tessdata").mkdir()
        shutil.copy2(MODEL_SOURCE, output / "tessdata" / "eng.traineddata")
    manifest = {
        "installer": {"name": INSTALLER_NAME, "url": INSTALLER_URL, "sha256": INSTALLER_SHA256},
        "runtime": "Tesseract 5.5.3 official Windows x64 release build",
        "selection": "tesseract.exe plus its PE import closure from the installer payload",
        "license": "licenses/tesseract/LICENSE (Apache-2.0) plus upstream notices",
        "files": [],
    }
    for path in sorted(output.rglob("*")):
        if path.is_file():
            body = path.read_bytes()
            manifest["files"].append(dict(path=path.relative_to(output).as_posix(),
                                          bytes=len(body), sha256=hashlib.sha256(body).hexdigest()))
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print(json.dumps(dict(manifest=str(MANIFEST_PATH), files=len(manifest["files"]),
                          bytes=sum(row["bytes"] for row in manifest["files"])),
                     ensure_ascii=False))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare", help="从固定安装器生成运行时和清单")
    prepare_parser.add_argument("--installer", type=Path, required=True)
    prepare_parser.add_argument("--sevenzip", required=True)
    prepare_parser.add_argument("--output", type=Path, default=DEFAULT_RUNTIME)
    verify_parser = sub.add_parser("verify", help="按清单校验一个运行时目录")
    verify_parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.installer, args.sevenzip, args.output)
        return 0
    manifest = validate(args.runtime)
    print(json.dumps(dict(runtime=str(args.runtime), files=len(manifest["files"]),
                          verified=[row["path"] for row in manifest["files"][:3]]),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
