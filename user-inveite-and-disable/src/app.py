# -*- coding: utf-8 -*-
"""
Datadog 複数 Org へのユーザ招待 Lambda（SDK + 招待メール API）

* ユーザ作成       : POST /api/v2/users  （UsersApi.create_user）
* 招待メール送信   : POST /api/v2/user_invitations （UsersApi.send_invitations）
* ユーザ削除       : DELETE /api/v2/users/{user_id}（UsersApi.disable_user）

S3 に `create_user.csv` / `delete_user.csv` を置くと自動実行されます。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import urllib.parse
from collections import defaultdict
from typing import Dict, Tuple

import boto3
import urllib3
from datadog_api_client import ApiClient, Configuration
from datadog_api_client.v2.api.users_api import UsersApi
from datadog_api_client.v2.model.user_create_attributes import UserCreateAttributes
from datadog_api_client.v2.model.user_create_data import UserCreateData
from datadog_api_client.v2.model.user_create_request import UserCreateRequest
from datadog_api_client.v2.model.users_type import UsersType
from datadog_api_client.v2.model.role_relationships import RoleRelationships
from datadog_api_client.v2.model.relationship_to_role_data import RelationshipToRoleData
from datadog_api_client.v2.model.relationship_to_roles import RelationshipToRoles
from datadog_api_client.v2.model.roles_type import RolesType
from datadog_api_client.v2.model.user_relationships import UserRelationships
from datadog_api_client.v2.model.user_invitation_data import UserInvitationData
from datadog_api_client.v2.model.user_invitation_relationships import UserInvitationRelationships
from datadog_api_client.v2.model.user_invitations_request import UserInvitationsRequest
from datadog_api_client.v2.model.user_invitations_type import UserInvitationsType
from datadog_api_client.v2.model.relationship_to_user import RelationshipToUser
from datadog_api_client.v2.model.relationship_to_user_data import RelationshipToUserData

# ────────────────────────────────────────────────────────────────
# ログ設定
# ────────────────────────────────────────────────────────────────
LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

# ────────────────────────────────────────────────────────────────
# AWS クライアント
# ────────────────────────────────────────────────────────────────
secrets = boto3.client("secretsmanager")
s3 = boto3.client("s3")

# Datadog サイト（US-1）
DD_SITE = "api.datadoghq.com"

# ────────────────────────────────────────────────────────────────
# Datadog ユーザ作成 + 招待メール送信
# ────────────────────────────────────────────────────────────────
def create_and_invite_user(keys: Tuple[str, str], name: str, email: str, role_id: str) -> None:
    """ユーザを作成し、招待メールを即時送信する。"""
    api_key, app_key = keys
    configuration = Configuration(
        host=f"https://{DD_SITE}",
        api_key={
            "apiKeyAuth": api_key,
            "appKeyAuth": app_key,
        },
    )
    #1) ユーザ作成
    body = UserCreateRequest(
        data=UserCreateData(
            type=UsersType.USERS,
            attributes=UserCreateAttributes(name=name, email=email),
            relationships=UserRelationships(
                roles=RelationshipToRoles(
                    data=[RelationshipToRoleData(id=role_id, type=RolesType.ROLES)]
                )
            ),
        )
    )
    with ApiClient(configuration) as api_client:
        users_api = UsersApi(api_client)
        try:
            create_resp = users_api.create_user(body=body)
            user_id = create_resp.data.id
            LOGGER.info("[CreateUser] %s → status=%s", email, create_resp.data.attributes.status)
        except Exception:
            LOGGER.exception("Failed to create Datadog user: %s", email)
            raise
        #2) 招待メール送信
        invite_body = UserInvitationsRequest(
            data=[
                UserInvitationData(
                    type=UserInvitationsType.USER_INVITATIONS,
                    relationships=UserInvitationRelationships(
                        user=RelationshipToUser(
                            data=RelationshipToUserData(type=UsersType.USERS, id=user_id)
                        )
                    ),
                )
            ]
        )
        try:
            invite_resp = users_api.send_invitations(body=invite_body)
            LOGGER.info("[SendInvite] %s → invitations sent=%s", email, invite_resp.meta.pagination.total_count)
        except Exception:
            LOGGER.exception("Failed to send invitation to: %s", email)
            raise

# ────────────────────────────────────────────────────────────────
# Datadog ユーザ削除処理
# ────────────────────────────────────────────────────────────────
def delete_user(keys: Tuple[str, str], email: str) -> None:
    """指定したメールアドレスのユーザを Datadog から削除（disable）する。"""
    api_key, app_key = keys
    configuration = Configuration(
        host=f"https://{DD_SITE}",
        api_key={
            "apiKeyAuth": api_key,
            "appKeyAuth": app_key,
        },
    )
    with ApiClient(configuration) as api_client:
        users_api = UsersApi(api_client)
        try:
            # ユーザ一覧から対象ユーザを検索
            users = list(users_api.list_users_with_pagination())
            user = next((u for u in users if u.attributes.email.lower() == email.lower()), None)
            if not user:
                LOGGER.warning("User not found for deletion: %s", email)
                return
            users_api.disable_user(user_id=user.id)
            LOGGER.info("[DeleteUser] %s → disabled", email)
        except Exception:
            LOGGER.exception("Failed to delete user: %s", email)
            raise

# ────────────────────────────────────────────────────────────────
# Utility: Datadog Role ID 解決
# ────────────────────────────────────────────────────────────────
def _get_role_id(keys: Tuple[str, str], role_name: str) -> str:
    api_key, app_key = keys
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Content-Type": "application/json",
    }
    http = urllib3.PoolManager()
    url = f"https://{DD_SITE}/api/v2/roles"
    resp = http.request("GET", url, headers=headers)
    if resp.status >= 300:
        raise RuntimeError(f"Failed to list roles (status {resp.status})")
    for role in json.loads(resp.data.decode())["data"]:
        if role["attributes"]["name"].lower() == role_name.lower():
            return role["id"]
    raise KeyError(f"Role '{role_name}' not found")

# ────────────────────────────────────────────────────────────────
# Lambda ハンドラ
# ────────────────────────────────────────────────────────────────

# Lambda 関数のメイン処理
# 1. S3イベントからCSVファイルを取得し
# 2. 各行のユーザ情報を読み取り
# 3. Datadogユーザ作成と招待メール送信を実行
# 4. 完了後にCSVを削除


def lambda_handler(event, context):
    # S3イベントから対象レコード取得
    records = event.get("Records", [])
    if not records:
        return

    # Secrets Manager からDatadog Orgごとの API/App Key を取得
    secret_name = os.environ["SECRET_NAME"]
    secret_json = json.loads(secrets.get_secret_value(SecretId=secret_name)["SecretString"])
    org_map: Dict[str, Tuple[str, str]] = {
        org: (data["keys"]["apiKey"], data["keys"]["appKey"]) for org, data in secret_json["orgs"].items()
    }

    role_cache: Dict[Tuple[str, str], str] = defaultdict(str)

    # S3上のファイルごとに処理
    for record in records:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        mode = "create" if key.endswith("create_user.csv") else "delete" if key.endswith("delete_user.csv") else None
        if not mode:
            continue

        # CSVファイルを読み取り
        csv_rows = csv.DictReader(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8").splitlines())
        for row in csv_rows:

            # メールアドレスのバリデーション
            email = row.get("email", "").strip()
            if not email:
                LOGGER.error("[CSV] Missing email column: %s", row)
                continue
            if mode == "create":
                name = (row.get("name") or "").strip() or email

            # 組織とロール名の取得
            org_name = row.get("org", "").strip()
            role_name = row.get("role", "").strip()
            if not org_name or not role_name:
                LOGGER.error("[CSV] Missing org/role: %s", row)
                continue
            if org_name not in org_map:
                LOGGER.error("Unknown org: %s", org_name)
                continue
            keys = org_map[org_name]
            cache_key = (org_name, role_name)
            if not role_cache[cache_key]:
                # Role ID が未キャッシュなら API で取得
                try:
                    role_cache[cache_key] = _get_role_id(keys, role_name)
                except Exception:
                    LOGGER.exception("Role lookup failed → %s / %s", org_name, role_name)
                    continue
            role_id = role_cache[cache_key]
            if mode == "create":
                try:
                    # Datadog にユーザ作成＆招待メール送信
                    create_and_invite_user(keys, name, email, role_id)
                except Exception:
                    LOGGER.exception("[Create+Invite] failed: %s", email)
            elif mode == "delete":
                try:
                    # Datadog からユーザ削除（disable）
                    delete_user(keys, email)
                except Exception:
                    LOGGER.exception("[DeleteUser] failed: %s", email)

        # 処理済み CSV を削除
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:
            LOGGER.exception("Failed to delete processed CSV: %s", key)
