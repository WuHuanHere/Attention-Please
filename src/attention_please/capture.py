"""分心截图: 把"屏幕截图 + 摄像头画面"拼成一张图存下来。

为什么两张都要:
  - 屏幕截图回答"我当时在看什么"(调参和事后复盘时最有用);
  - 摄像头画面回答"我当时什么姿态"(阈值不对时只有这张能说明问题)。

隐私处理(按你选的方案):
  - 存工作区内的 captures/, 7 天自动清理, 托盘一键清空;
  - 只存 JPEG, 质量 70, 宽度按 config 缩放(默认 960), 不存原始分辨率;
  - 程序只写事件日志和这张图, 图像**不上传任何地方**。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

JPEG_QUALITY = 70
SEPARATOR_PX = 6
LABEL_BAR_PX = 22


def grab_screen() -> Image.Image | None:
    """抓当前屏幕。失败(锁屏/权限/远程桌面)返回 None, 绝不抛异常。"""
    try:
        from PIL import ImageGrab
    except Exception:  # noqa: BLE001
        return None
    for kwargs in ({"all_screens": True}, {}):
        try:
            return ImageGrab.grab(**kwargs).convert("RGB")
        except Exception:  # noqa: BLE001
            continue
    return None


def frame_to_image(frame_bgr) -> Image.Image | None:
    import cv2

    if frame_bgr is None:
        return None
    try:
        return Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    except Exception:  # noqa: BLE001
        return None


def _fit_width(img: Image.Image, width: int) -> Image.Image:
    if img.width == width:
        return img
    height = max(1, round(img.height * width / img.width))
    return img.resize((width, height), Image.LANCZOS)


def _label(img: Image.Image, text: str) -> Image.Image:
    """在图片顶部加一条小黑条写说明(只用 ASCII —— PIL 默认字体画不了中文)。"""
    canvas = Image.new("RGB", (img.width, img.height + LABEL_BAR_PX), (20, 20, 20))
    canvas.paste(img, (0, LABEL_BAR_PX))
    draw = ImageDraw.Draw(canvas)
    draw.text((6, 5), text, fill=(230, 230, 230))
    return canvas


def compose(screen: Image.Image | None, camera: Image.Image | None,
            width: int = 960, when: datetime | None = None,
            note: str = "") -> Image.Image | None:
    """上=屏幕, 下=摄像头, 拼接成一张图。两个都没拿到时返回 None。"""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    parts: list[Image.Image] = []
    if screen is not None:
        parts.append(_label(_fit_width(screen, width), f"SCREEN  {stamp}"))
    if camera is not None:
        parts.append(_label(_fit_width(camera, width), f"CAMERA  {stamp}  {note}"))
    if not parts:
        return None
    total_h = sum(p.height for p in parts) + SEPARATOR_PX * (len(parts) - 1)
    canvas = Image.new("RGB", (width, total_h), (0, 0, 0))
    y = 0
    for part in parts:
        canvas.paste(part, (0, y))
        y += part.height + SEPARATOR_PX
    return canvas


def save_composite(capture_dir: Path, *, at: datetime, frame_bgr, width: int = 960,
                   screen: bool = True, camera: bool = True,
                   note: str = "") -> Path | None:
    """存一张拼接图, 返回路径(失败返回 None —— 截图失败绝不能影响监控)。"""
    try:
        capture_dir.mkdir(parents=True, exist_ok=True)
        screen_img = grab_screen() if screen else None
        cam_img = frame_to_image(frame_bgr) if camera else None
        composed = compose(screen_img, cam_img, width=width, when=at, note=note)
        if composed is None:
            return None
        path = capture_dir / f"{at:%Y-%m-%d_%H-%M-%S}.jpg"
        composed.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return path
    except Exception:  # noqa: BLE001
        return None


def cleanup(capture_dir: Path, retention_days: int = 7) -> int:
    """删掉超过保留期的截图, 返回删除数量。"""
    if retention_days <= 0 or not capture_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in capture_dir.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def clear_all(capture_dir: Path) -> tuple[int, int]:
    """托盘上的"清空所有截图"。返回 `(删掉的张数, 没删掉的张数)`。

    **必须把失败数也返回**: Windows 上文件被看图软件/杀毒/索引器打开着时
    `unlink()` 会抛 OSError。以前这里只数成功的, 于是托盘提示「已清空 2 张」而
    第 3 张还在硬盘上 —— 用户以为隐私数据清干净了, 其实没有, 而且哪儿都没说。
    """
    if not capture_dir.exists():
        return 0, 0
    removed = failed = 0
    for path in capture_dir.glob("*.jpg"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            failed += 1
    return removed, failed


def newest(capture_dir: Path) -> Path | None:
    if not capture_dir.exists():
        return None
    files = sorted(capture_dir.glob("*.jpg"))
    return files[-1] if files else None


def retention_date(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
