"""把 playwright 及其依赖装进工作区的 vendor/ 目录。

为什么要自己下 wheel：这台机器上 pip 装不进去（沙箱/AV 锁住了它解压出的临时文件），
而 playwright 自带的 `playwright install` 又会用 Node 的 `fork` 下浏览器（沙箱禁止命名管道 → EPERM）。
wheel 本身就是 zip，直接解压等价且可控。

用法：
    python scripts/bootstrap_playwright.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHEEL_DIR = ROOT / ".wheels"
VENDOR = ROOT / "vendor"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"

PACKAGES = ("playwright", "pyee", "greenlet", "typing_extensions")
MAJ, MIN = sys.version_info.major, sys.version_info.minor


def rank_wheel(filename: str) -> int:
    """越具体越优先：本平台二进制 > 平台无关 wheel。"""
    if not filename.endswith(".whl"):
        return -1
    if f"cp{MAJ}{MIN}-cp{MAJ}{MIN}-win_amd64" in filename:
        return 3
    if "py3-none-win_amd64" in filename:
        return 2
    if "-py3-none-any.whl" in filename or "-py2.py3-none-any.whl" in filename:
        return 1
    return -1


def pick_wheel(data: dict) -> dict:
    best, best_rank = None, -1
    for item in data["urls"]:
        r = rank_wheel(item["filename"])
        if r > best_rank:
            best, best_rank = item, r
    if best is None:
        raise SystemExit(f"没有找到可用的 wheel：{data['info']['name']}")
    return best


def fetch(name: str) -> Path:
    with urllib.request.urlopen(PYPI_JSON.format(name=name), timeout=60) as resp:
        data = json.load(resp)
    wheel = pick_wheel(data)
    target = WHEEL_DIR / wheel["filename"]
    if target.exists():
        print(f"  {name}: 已存在 {target.name}")
        return target
    print(f"  {name}: 下载 {target.name} ({wheel['size'] // 1024} KB)")
    with urllib.request.urlopen(wheel["url"], timeout=300) as resp, target.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
    return target


def main() -> int:
    WHEEL_DIR.mkdir(exist_ok=True)
    VENDOR.mkdir(exist_ok=True)

    print("[1/3] 拉取 wheel")
    wheels = [fetch(name) for name in PACKAGES]

    print(f"[2/3] 解压 → {VENDOR}")
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(VENDOR)

    print("[3/3] 校验")
    sys.path.insert(0, str(VENDOR))
    try:
        from playwright._repo_version import version as pw_version

        print(f"  playwright {pw_version} 就绪")
    except Exception as exc:  # noqa: BLE001
        print(f"  导入失败：{type(exc).__name__}: {exc}")
        return 1

    print(
        "\n浏览器不用下载：直接用系统已装的 Chrome/Edge。\n"
        f'  $env:PYTHONPATH="{VENDOR}"\n'
        f'  $env:PLAYWRIGHT_BROWSERS_PATH="{ROOT / ".browsers"}"\n'
        "  然后用 channel='chrome' 启动。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
