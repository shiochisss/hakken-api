"""GTFS-JP の取得・読み取り共通部（stops 以外の取込でも再利用する）。

- ODPT からの zip ダウンロード。取得URLは社ごとに形式が異なるため設定ファイル駆動。
- アクセストークンは環境変数 ODPT_CONSUMER_KEY から読む。URL・ログ・ファイルに残さない
  （ログ出力時は consumerKey をマスクする）。
- zip 内 stops.txt を dict の iterator として返す。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # hakken-api/
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data" / "gtfs"

# --dry-run 単体でも ODPT_CONSUMER_KEY 等が読めるよう、ここで .env をロードする
# （従来は DB書込パスの batch/db.py でしか読まれず dry-run で未ロードだった）
load_dotenv(BASE_DIR / ".env")

# ログ用：URL 中の consumerKey を伏せる
_TOKEN_RE = re.compile(r"(acl:consumerKey=)[^&\s]+")

# --- date 自動リゾルバ（方針A）用 ---
_CKAN_DATASET = "https://ckan.odpt.org/dataset/{slug}"
_CKAN_RESOURCE = "https://ckan.odpt.org/dataset/{slug}/resource/{uuid}"
_UUID_RE = re.compile(r"/resource/([0-9a-f]{8}-[0-9a-f-]{27,})")
_ZIP_URL_RE = re.compile(r"https?://api\.odpt\.org/api/v4/files/[^\s\"'<>]*?\.zip[^\s\"'<>]*")
_DATE_RE = re.compile(r"date=(\d{8})")


def _mask(url: str) -> str:
    return _TOKEN_RE.sub(r"\1***", url)


def load_sources() -> dict:
    """gtfs_sources.json から社→設定 の dict を返す（"_" 始まりのキーは除外）。"""
    with open(CONFIG_DIR / "gtfs_sources.json", encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def get_token() -> str:
    token = os.environ.get("ODPT_CONSUMER_KEY")
    if not token:
        raise RuntimeError("ODPT_CONSUMER_KEY が未設定です（hakken-api/.env を確認）")
    return token


def _http_get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def resolve_latest_date(slug: str) -> str | None:
    """CKAN のリソースページから最新の配信 date(YYYYMMDD) を抽出。失敗時 None。

    ODPT の新形式 GTFS は date が「公開版の日付」で当日ではなく、再配信で変わる。
    dataset ページに実URLは載らないため、resource ページの api.odpt.org zip URL の
    date= を集めて最大値（＝最新版）を返す。ODPT は HEAD 非対応のため GET のみ使用。
    """
    try:
        page = _http_get_text(_CKAN_DATASET.format(slug=slug))
        uuids = sorted(set(_UUID_RE.findall(page)))
        dates: list[str] = []
        for uuid in uuids:
            rp = _http_get_text(_CKAN_RESOURCE.format(slug=slug, uuid=uuid))
            for zurl in _ZIP_URL_RE.findall(rp):
                m = _DATE_RE.search(zurl)
                if m:
                    dates.append(m.group(1))
        return max(dates) if dates else None
    except Exception:
        return None


def resolve_date(operator: str, source: dict) -> str | None:
    """requires_date=true の社が使う date を決める。
    優先順: ①CKAN最新（ckan_slug）→ ②env {OP}_GTFS_DATE → ③config の date → None。
    requires_date=false の社は None（date 不要）。
    """
    if not source.get("requires_date"):
        return None
    slug = source.get("ckan_slug")
    if slug:
        d = resolve_latest_date(slug)
        if d:
            return d
    envd = os.environ.get(f"{operator.upper()}_GTFS_DATE")
    if envd:
        return envd
    if source.get("date"):
        return source["date"]
    return None


def _build_url(source: dict, token: str, date: str | None) -> str:
    params = {"token": token}
    if source.get("requires_date"):
        params["date"] = date
    return source["url_template"].format(**params)


def download(operator: str, source: dict, token: str) -> Path:
    """指定社の GTFS zip を data/gtfs/{operator}/{operator}.zip に取得して返す。

    requires_date の社は resolve_date で date を解決。解決できなければ例外
    （呼び出し側で1社スキップして継続する＝堅牢化）。
    """
    date = resolve_date(operator, source)
    if source.get("requires_date") and not date:
        raise RuntimeError(
            f"{operator}: 配信dateを解決できず（CKAN自動/env/config すべて不可）"
        )
    dest_dir = DATA_DIR / operator
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{operator}.zip"
    url = _build_url(source, token, date)
    label = f"{operator} (date={date})" if date else operator
    print(f"  取得: {label}  <- {_mask(url)}")
    try:
        with urllib.request.urlopen(url, timeout=180) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        hint = "（date有効期間・トークンを確認）" if source.get("requires_date") else "（トークンを確認）"
        raise RuntimeError(
            f"{operator} のGTFS取得に失敗 HTTP {e.code} {hint}: {_mask(url)}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{operator} のGTFS取得に失敗（ネットワーク）: {e.reason}") from e
    dest.write_bytes(data)
    return dest


def _find_member(z: zipfile.ZipFile, filename: str) -> str:
    for n in z.namelist():
        if n == filename or n.endswith("/" + filename):
            return n
    raise RuntimeError(f"{filename} が zip 内に見つかりません: {z.filename}")


def read_table(zip_path: Path, filename: str) -> Iterator[dict]:
    """zip 内の任意 GTFS テキスト（stops.txt / routes.txt / trips.txt /
    stop_times.txt / calendar.txt 等）を1行=dict で返す。GTFS-JP は UTF-8（BOM許容）。

    ファイルが zip 内に無い場合は RuntimeError（呼び出し側で任意/必須を判断）。
    """
    with zipfile.ZipFile(zip_path) as z:
        member = _find_member(z, filename)
        with z.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            yield from csv.DictReader(text)


def read_stops(zip_path: Path) -> Iterator[dict]:
    """zip 内 stops.txt を1行=dict で返す（read_table の薄いラッパ・F9 stops 取込用）。"""
    yield from read_table(zip_path, "stops.txt")
