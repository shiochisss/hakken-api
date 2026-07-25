"""画像デコード・向き補正・メタデータ除去・RGB化・リサイズ・JPEG再エンコード。

処理順序（この順序を変更しないこと）:
  デコード → EXIF Orientation反映 → メタデータ除去 → RGB変換 → JPEG化 →
  本体リサイズ(1600px) → サムネイルリサイズ(400px) → quality=85で再エンコード

メタデータ除去は「タグを選んで消す」のではなく、向き補正後の画素データのみを
新規キャンバスへ描画し直す（Image.new + paste）ことで、EXIF/GPS/XMP/IPTC等の
補助情報を一切引き継がない実装にしている。

出典: hakken-f11 納品物（F11・おかむー）app/imaging/convert.py を無改変で移植。
※pillow-heif は Azure App Service (Linux) 上での動作が未検証。結合テストの最優先確認事項。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - pillow-heif is a required dependency
    pillow_heif = None  # type: ignore[assignment]

from app.imaging.constants import (
    JPEG_QUALITY,
    MAIN_MAX_DIMENSION,
    MAX_MEGAPIXELS,
    THUMBNAIL_MAX_DIMENSION,
)
from app.imaging.validate import ImageKind, UnsupportedImageError, check_pil_format

# Pillowの初期値を明示的に上書きし、警告レンジも含めて安全側(=拒否)に倒す。
Image.MAX_IMAGE_PIXELS = MAX_MEGAPIXELS


class ImageDecodeError(ValueError):
    """デコード不能・破損・画素数超過など、400として扱うべきエラー。"""


@dataclass(frozen=True)
class ProcessedImages:
    main_bytes: bytes
    thumbnail_bytes: bytes


def decode_image(data: bytes, kind: ImageKind) -> Image.Image:
    """バイト列を画像としてデコードする。警告も無視せず例外化する。
    デコード結果の実形式(img.format)が事前判定(kind)と一致するかもここで確認する。
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(BytesIO(data))
            img.load()
    except UnsupportedImageError:
        raise
    except Exception as exc:  # Pillowの各種デコード例外・DecompressionBombWarning由来のerrorを含む
        raise ImageDecodeError(f"failed to decode image: {exc}") from exc

    check_pil_format(img.format, kind)

    width, height = img.size
    if width * height > MAX_MEGAPIXELS:
        raise ImageDecodeError("image exceeds maximum allowed megapixels")

    return img


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """RGB/RGBA/P/LA/L/CMYK を安全にRGBへ変換する。透過は白背景に合成する。"""
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def _resize_within(img: Image.Image, max_dimension: int) -> Image.Image:
    """アスペクト比を維持し、長辺がmax_dimension以内になるよう縮小する。拡大はしない。"""
    width, height = img.size
    longest = max(width, height)
    if longest <= max_dimension:
        return img.copy()
    scale = max_dimension / longest
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def _encode_jpeg(img: Image.Image) -> bytes:
    buffer = BytesIO()
    # exif等は一切渡さない（新規キャンバスへ描画済みのため、そもそも保持していない）。
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def process_image(data: bytes, kind: ImageKind) -> ProcessedImages:
    img = decode_image(data, kind)

    # EXIF Orientationを画素へ反映してから、向き情報を含む全メタデータを破棄する。
    oriented = ImageOps.exif_transpose(img)
    if oriented is None:  # 画像にOrientationが無い場合はNoneを返す仕様のため元画像を使う
        oriented = img

    flattened = _flatten_to_rgb(oriented)

    main_img = _resize_within(flattened, MAIN_MAX_DIMENSION)
    thumbnail_img = _resize_within(flattened, THUMBNAIL_MAX_DIMENSION)

    return ProcessedImages(
        main_bytes=_encode_jpeg(main_img),
        thumbnail_bytes=_encode_jpeg(thumbnail_img),
    )
