#!/usr/bin/env python3
# 使い方
"""
実行方法
$ python3 datadog_export_import_monitor.py export <csv_file>
$ python3 datadog_export_import_monitor.py import <json_pattern>

引数の違い
export : モニター設定をDatadog Organizationから取得し、JSONでエクスポートします
import : モニター設定JSONを読み込み、モニターをDatadog Organizationに新規作成します

    <csv_file>
    id
    123456789
    098765432
    ※2行目以降にエクスポートしたいモニターIDを記載下さい　IDはモニターURLの末尾数字です

    <json_pattern>
    *.jsonや./monitors/??????.jsonでjsonファイルのパスを指定して下さい
    JSONファイルは、モニターのExportを実行したときのJSON形式でファイルを作成して下さい

処理の流れ
1. 実行時に **Datadog API Key** と **Application Key** を対話入力します
2. `export`
   - 指定した CSV から Monitor ID を読み取り
   - `GET /api/v1/monitor/{id}` でモニター定義を取得
   - `<id>.json` という名前で保存
3. `import`
   - 指定パターンに一致する JSON ファイルを逐次読み取り
   - 各 JSON を `POST /api/v1/monitor` で新規モニターとして登録
4. 成功／失敗は `datadog_monitor.log`（INFO レベル以上）に記録されます

前提条件
- Python 3.8 以上
- `datadog_api_client` ライブラリ（公式クライアント v1 系）を `pip3 install datadog-api-client` で導入済みであること
- 実行環境から Datadog API エンドポイントへネットワーク到達できること

Examples
# 監視 ID をまとめた CSV を基にエクスポート
$ python3 datadog_export_import_monitor.py export monitors.csv

# カレントディレクトリ配下の JSON をすべてインポート
$ python3 datadog_export_import_monitor.py import "*.json"
"""

from __future__ import annotations
import argparse
import csv
import glob
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Final, Set

from datadog_api_client import ApiClient, Configuration
from datadog_api_client.v1.api.monitors_api import MonitorsApi

################################################################################
# Constants
################################################################################
# スクリプト全体で共有される **変更されることのない値** をここで宣言します。
# すべて大文字・型ヒントに Final を付けて「定数」であることを明示し、
# 後段の処理を見ただけで意味がわかるようにしています。
#
# * READ_ONLY_KEYS
#   Datadog が内部で管理しており、モニター作成時に送ると
#   エラーになる／無視される「読み取り専用属性」の集合です。
#   export 時には保持していますが、import 時に API へ
#   POST する前に取り除きます。
# * LOG_FILE / DATEFMT
#   logging.basicConfig に渡す値をまとめています。
#   ここを変更するだけでログ設定を一括で切り替え可能です。
################################################################################
READ_ONLY_KEYS: Final[Set[str]] = {
    "id",
    "org_id",
    "created",
    "created_at",
    "modified",
    "deleted",
    "overall_state",
    "overall_state_modified",
    "matching_downtimes",
    "multi",
    "overall_state_transitions",
    "creator",
}

LOG_FILE: Final[str] = "datadog_monitor.log"
DATEFMT: Final[str] = "%Y-%m-%d %H:%M:%S"

# --- Python 標準 logging の初期化 -----------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt=DATEFMT,
)

################################################################################
# Helpers
################################################################################
# メインロジックから **雑多な前処理/後処理** を切り出しておくことで、
# コア部分を読みやすく保ちます。いずれも「純粋関数」的に作られており
# テストもしやすい構成です。
################################################################################

def get_api_keys() -> tuple[str, str]:
    """環境変数または対話入力から (API_KEY, APP_KEY) を取得して返す。"""
    api_key = os.getenv("DD_API_KEY") or input("Datadog API Key: ")
    app_key = os.getenv("DD_APP_KEY") or input("Datadog Application Key: ")
    return api_key.strip(), app_key.strip()


def create_configuration(api_key: str, app_key: str) -> Configuration:
    """Datadog API クライアント用の Configuration を生成。"""
    cfg = Configuration()
    cfg.api_key["apiKeyAuth"] = api_key
    cfg.api_key["appKeyAuth"] = app_key
    return cfg


def sanitize_monitor(payload: Dict[str, Any]) -> Dict[str, Any]:
    """読み取り専用属性を除外して monitor 定義をクリーンアップする。"""
    return {k: v for k, v in payload.items() if k not in READ_ONLY_KEYS}

################################################################################
# Core actions
################################################################################
# export / import の本体処理です。API 呼び出しは公式クライアントの
# コンテキストマネージャを用い、セッション close 漏れを防いでいます。
################################################################################

def export_monitors(csv_file: str) -> None:
    """CSV で指定された monitor を Datadog から取得し JSON で保存。"""
    api_key, app_key = get_api_keys()
    configuration = create_configuration(api_key, app_key)

    with ApiClient(configuration) as api_client:
        api = MonitorsApi(api_client)
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header or header[0].lower() != "id":
                print("CSV first column header must be 'id'", file=sys.stderr)
                return
            for row in reader:
                if not row:
                    continue
                try:
                    monitor_id = int(row[0])
                except ValueError:
                    msg = f"Invalid ID '{row[0]}' – skipping"
                    logging.error(msg)
                    print(msg, file=sys.stderr)
                    continue
                try:
                    monitor = api.get_monitor(monitor_id)
                    data = sanitize_monitor(monitor.to_dict())
                    out_path = Path(f"{monitor_id}.json")
                    with out_path.open("w", encoding="utf-8") as jf:
                        json.dump(data, jf, indent=4, ensure_ascii=False, default=str)
                    logging.info("Exported monitor ID %s", monitor_id)
                except Exception as exc:
                    msg = f"Failed to export monitor ID {monitor_id}: {exc}"
                    logging.error(msg)
                    print(msg, file=sys.stderr)


def import_monitors(pattern: str) -> None:
    """パターンにマッチする JSON ファイルから monitor を新規作成。"""
    files = glob.glob(pattern)
    if not files:
        print(f"No JSON files found for pattern: {pattern}", file=sys.stderr)
        logging.error("No JSON files found for pattern: %s", pattern)
        return

    api_key, app_key = get_api_keys()
    configuration = create_configuration(api_key, app_key)

    with ApiClient(configuration) as api_client:
        api = MonitorsApi(api_client)
        for file_path in files:
            try:
                with open(file_path, encoding="utf-8") as f:
                    payload: Dict[str, Any] = json.load(f)
                body = sanitize_monitor(payload)
                monitor = api.create_monitor(body=body)
                logging.info("Imported %s as new monitor ID %s", file_path, monitor.id)
            except Exception as exc:
                msg = f"Failed to import {file_path}: {exc}"
                logging.error(msg)
                print(msg, file=sys.stderr)

################################################################################
# CLI
################################################################################
# サブコマンド形式で export / import を切り替えられる軽量 CLI を実装。
# argparse は組み込みなので追加依存なしで済みます。
################################################################################

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Datadog monitor export/import helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="Export monitors listed in CSV")
    p_export.add_argument("csv_file", help="CSV file listing monitor IDs")

    p_import = sub.add_parser("import", help="Import monitors from JSON files")
    p_import.add_argument("pattern", help="Glob pattern to JSON files (e.g. '*.json')")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "export":
        export_monitors(args.csv_file)
    elif args.command == "import":
        import_monitors(args.pattern)


if __name__ == "__main__":
    main()
