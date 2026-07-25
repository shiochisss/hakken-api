"""画像形式の受付判定。

受け付けるのは JPEG / PNG / HEIC の3形式のみ（HEIC対応は連携メモにて発注側から依頼済み・
正式合意済み）。「独立したHEIF」は受付対象外のため、HEICブランドのみを許可し、汎用HEIF
ブランド（mif1／msf1等）は拒否する。ブランド判定はマジックナンバー段階で行う。

判定は拡張子・Content-Type・マジックナンバー・実デコード結果の組み合わせで行い、
いずれかが重大に食い違う場合は不正とみなす。

出典: hakken-f11 納品物（F11・おかむー）app/imaging/validate.py を無改変で移植。
※HEICブランドの許可範囲は納品元の独自解釈（資料に明記なし）。実機写真での確認が必要。
"""

from __future__ import annotations

from dataclasses import dataclass

ImageKind = str  # "jpeg" | "png" | "heic"

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# ISO-BMFF/HEIF: bytes[4:8] == b"ftyp", brand at bytes[8:12].
# HEIC系ブランドのみ許可。mif1/msf1等の汎用HEIFブランドは明示的に拒否する。
_HEIC_ALLOWED_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_HEIF_GENERIC_BRANDS = {b"mif1", b"msf1", b"heif"}

_ALLOWED_EXTENSIONS: dict[ImageKind, set[str]] = {
    "jpeg": {".jpg", ".jpeg"},
    "png": {".png"},
    "heic": {".heic"},
}

_ALLOWED_CONTENT_TYPES: dict[ImageKind, set[str]] = {
    "jpeg": {"image/jpeg", "image/jpg"},
    "png": {"image/png"},
    "heic": {"image/heic", "image/heic-sequence"},
}

# PIL が img.format として報告する値との対応
_PIL_FORMAT_FOR_KIND: dict[ImageKind, set[str]] = {
    "jpeg": {"JPEG"},
    "png": {"PNG"},
    "heic": {"HEIF"},  # pillow-heif は HEIC も "HEIF" として報告する
}


class UnsupportedImageError(ValueError):
    """許可されていない画像形式・破損・偽装など、400として扱うべきエラー。"""


@dataclass(frozen=True)
class SniffResult:
    kind: ImageKind


def sniff_magic_bytes(data: bytes) -> SniffResult:
    if data.startswith(_JPEG_MAGIC):
        return SniffResult(kind="jpeg")
    if data.startswith(_PNG_MAGIC):
        return SniffResult(kind="png")
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _HEIC_ALLOWED_BRANDS:
            return SniffResult(kind="heic")
        if brand in _HEIF_GENERIC_BRANDS:
            raise UnsupportedImageError("generic HEIF brand is not accepted; only HEIC is accepted")
        raise UnsupportedImageError(f"unrecognized ISO-BMFF brand: {brand!r}")
    raise UnsupportedImageError("unrecognized file signature")


def check_extension(filename: str | None, kind: ImageKind) -> None:
    if not filename or "." not in filename:
        raise UnsupportedImageError("filename has no extension")
    ext = "." + filename.rsplit(".", 1)[-1].lower()
    if ext not in _ALLOWED_EXTENSIONS[kind]:
        raise UnsupportedImageError(f"extension {ext!r} does not match detected format {kind!r}")


def check_content_type(content_type: str | None, kind: ImageKind) -> None:
    if not content_type:
        raise UnsupportedImageError("missing Content-Type")
    normalized = content_type.split(";")[0].strip().lower()
    if normalized not in _ALLOWED_CONTENT_TYPES[kind]:
        raise UnsupportedImageError(
            f"Content-Type {content_type!r} does not match detected format {kind!r}"
        )


def check_pil_format(pil_format: str | None, kind: ImageKind) -> None:
    if not pil_format or pil_format.upper() not in _PIL_FORMAT_FOR_KIND[kind]:
        raise UnsupportedImageError(
            f"decoded image format {pil_format!r} does not match detected format {kind!r}"
        )


def validate_upload(data: bytes, *, filename: str | None, content_type: str | None) -> ImageKind:
    """拡張子・Content-Type・マジックナンバーの組み合わせ検証を行い、判定したkindを返す。
    実デコード結果とのクロスチェック（check_pil_format）はデコード時（app.imaging.convert）で行う。
    """
    sniff = sniff_magic_bytes(data)
    check_extension(filename, sniff.kind)
    check_content_type(content_type, sniff.kind)
    return sniff.kind
