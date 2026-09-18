"""下载 MediaPipe 模型文件(只需跑一次, 之后完全离线)。

为什么不用 PowerShell 的 Invoke-WebRequest: 在受限 shell 里 schannel 拿不到凭据
(SEC_E_NO_CREDENTIALS), curl/iwr 全部 TLS 失败; Python 自带 OpenSSL, 不受影响。

用法(项目根目录):
  .\\.venv\\Scripts\\python.exe scripts\\download_models.py
  python scripts\\download_models.py --force
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
BASE = "https://storage.googleapis.com/mediapipe-models"

TARGETS = [
    {
        "name": "pose_landmarker_lite.task",
        "url": f"{BASE}/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
        "size": 5777746,
    },
    {
        "name": "face_landmarker.task",
        "url": f"{BASE}/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "size": 3758596,
    },
]

socket.setdefaulttimeout(60)


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


def download(url: str, dest: Path, expect: int, retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            t0 = time.time()
            with urllib.request.urlopen(url) as resp, open(tmp, "wb") as fh:
                total = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
            got = tmp.stat().st_size
            if expect and got != expect:
                print(f"  [warn] 体积 {got} != 预期 {expect}")
            tmp.replace(dest)
            print(f"  [ok  ] {dest.name} {human(got)} in {time.time() - t0:.1f}s")
            return True
        except (urllib.error.URLError, OSError) as exc:
            print(f"  [retry {attempt}/{retries}] {type(exc).__name__}: {exc}")
            time.sleep(2 * attempt)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    failed = []
    for t in TARGETS:
        dest = MODELS / t["name"]
        if dest.exists() and not args.force and dest.stat().st_size == t["size"]:
            print(f"  [skip] {dest.name} 已存在且体积正确 ({human(t['size'])})")
            continue
        print(f"  [get ] {dest.name} <- {t['url']}")
        if not download(t["url"], dest, t["size"]):
            failed.append(t["name"])

    print()
    for f in sorted(MODELS.iterdir()):
        print(f"  {f.name:34s} {f.stat().st_size:>10,d} bytes")
    if failed:
        print(f"\n失败: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("\n模型齐了, 之后运行不需要网络。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
