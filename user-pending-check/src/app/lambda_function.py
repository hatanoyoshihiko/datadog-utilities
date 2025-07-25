#!/usr/bin/env python3
"""
Datadog 「invite_pending」ユーザー取得スクリプト (AWS Lambda)
======================================================

Secrets Manager に保存された Datadog 組織ごとの API / Application Key を使い、
各組織で招待保留 (invite_pending) 状態にあるユーザを取得して CloudWatch Logs へ出力、
反映結果を JSON回答する Lambda 関数です。
"""

import json
import os
import logging
from typing import Dict, List, Generator

import boto3
import requests

# --------------------------------------------------
# ログ設定
# --------------------------------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --------------------------------------------------
# 環境変数
# --------------------------------------------------
# Secrets Manager のシークレット名
SECRET_NAME: str = os.environ.get("SECRET_NAME", "ddOrgSecret")
# Datadog サイト (datadoghq.com / datadoghq.eu 等)
DATADOG_SITE: str = os.environ.get("DATADOG_SITE", "datadoghq.com")
# Datadog API v2 の 1 ページ得点数
PAGE_SIZE: int = 100


# --------------------------------------------------
# Secrets Managerから組織情報を取得
# --------------------------------------------------

def get_orgs() -> Dict[str, dict]:
    """Secrets Manager から Datadog 組織情報 (API/App Key) を取得する"""
    sm = boto3.client("secretsmanager")
    secret_string = sm.get_secret_value(SecretId=SECRET_NAME)["SecretString"]
    return json.loads(secret_string)["orgs"]


# --------------------------------------------------
# Datadog API v2 /users エンドポイントのページングを透過的に処理するジェネレータ
# --------------------------------------------------

def list_users(api_key: str, app_key: str) -> Generator[dict, None, None]:
    """ユーザ情報を取得するジェネレータ"""
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Content-Type": "application/json",
    }
    # 初回リクエスト URL
    url: str = f"https://api.{DATADOG_SITE}/api/v2/users"
    params = {"page[size]": PAGE_SIZE, "filter[status]": "Pending"}

    while url:
        # --- API 呼び出し ---
        try:
            res = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=10          # タイムアウトを明示
            )
            res.raise_for_status()
        except requests.RequestException as e:
            logger.error("Datadog API request failed: %s", e)
            logger.error("Response content: %s",
                        getattr(e.response, "text", "N/A"))
            raise

        body = res.json()
        # --- 現在ページの data を yield ---
        yield from body.get("data", [])

        # --- 次ページ URL があれば継続し、なければループ終了 ---
        url = body.get("links", {}).get("next")
        params = None  # 次ページ以降は URL にパラメータが内包される


# --------------------------------------------------
# 各組織で invite_pending 状態のユーザを抽出
# --------------------------------------------------

def fetch_invite_pending(org_name: str, keys: dict) -> List[dict]:
    """招待保留状態 (invite_pending) のユーザ一覧を返す"""
    api, app = keys["apiKey"], keys["appKey"]
    pending: List[dict] = []

    # 全ユーザを走査
    for user in list_users(api, app):
        status = user.get("attributes", {}).get("status", "").lower()
        # Datadog API の値は Pending/Active/Disabled
        if status == "pending":
            # 必要最小限の属性のみ保持
            pending.append(
                {
                    "id": user["id"],
                    "email": user["attributes"].get("email"),
                    "name": user["attributes"].get("name"),
                }
            )
    return pending


# --------------------------------------------------
# Lambda ハンドラ
# --------------------------------------------------

def lambda_handler(event, context):
    """全組織の invite_pending ユーザを集計し、ログ + レスポンス返却"""
    result: Dict[str, List[dict]] = {}

    # --- Secrets Manager で定義されたすべての組織をループ ---
    for org_name, info in get_orgs().items():
        result[org_name] = fetch_invite_pending(org_name, info["keys"])

    # --- CloudWatch Logs へ整形出力 ---
    print("Invite Pending Users")
    for org, users in result.items():
        print(f"=== {org} ===")
        if not users:
            print("招待保留ユーザはありません")
        else:
            for u in users:
                # 左詰め整形: email 35文字・名前25文字・ID
                print(f"{u['email']:<35} {u['name'] or '-':<25} id:{u['id']}")

    # --- API Gateway などへのレスポンス ---
    return {
        "statusCode": 200,
        # ensure_ascii=False で日本語もそのまま出力
        "body": json.dumps(result, ensure_ascii=False),
    }
