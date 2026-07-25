"""画像処理の定数集約。値をコードへ分散させないこと。

出典: hakken-f11 納品物（F11・おかむー）app/imaging/constants.py を無改変で移植。
"""

# 受付上限（バイト）。10MB。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 読み込みチャンクサイズ（バイト）。Content-Lengthを信用せず実バイト数で判定するために使用。
READ_CHUNK_BYTES = 1024 * 1024

# 本体・サムネイルの長辺上限（px）
MAIN_MAX_DIMENSION = 1600
THUMBNAIL_MAX_DIMENSION = 400

# JPEG再エンコード品質
JPEG_QUALITY = 85

# 画像安全対策：最大画素数（50メガピクセル）
MAX_MEGAPIXELS = 50_000_000

# 出力Content-Type / 拡張子
OUTPUT_CONTENT_TYPE = "image/jpeg"
OUTPUT_EXTENSION = ".jpg"
