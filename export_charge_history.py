#!/usr/bin/env python3
"""Export all Volvo Cars China home-charger sessions to CSV and JSON.

Standalone script: uses only the Python standard library, no dependencies.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from urllib.request import Request, urlopen


API_BASE_URL = "https://apigateway.digitalvolvo.com"
LOGIN_PATH = "/app/iam/api/v1/auth"
PILE_LIST_PATH = "/app/charge-pile/api/v1/api/brandPile/getPileList"
CHARGE_HISTORY_PATH = "/app/charge-pile/api/v1/api/brandHomePile/queryList"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
DEFAULT_TIMEOUT_SECONDS = 20

# Shared API-gateway signing credentials embedded in Volvo's own official
# mobile app (obtained by inspecting the app's network traffic, the same
# value published by the hass-volvooncall-cn Home Assistant integration:
# https://github.com/idreamshen/hass-volvooncall-cn). They identify the
# request as coming from "an official Volvo app", not from any individual
# user or developer account — your Volvo account credentials are what's
# actually personal here. See the README's disclaimer before relying on this.
DEFAULT_APP_KEY = "204114990"
DEFAULT_APP_SECRET = "bjGqb3TvEEZ8W8QhoyhEH4IenwCnc4JQ"

CSV_FIELDS = (
    "equipmentName",
    "connectorId",
    "orderNo",
    "tradeNo",
    "connectorName",
    "startTime",
    "endTime",
    "chargeUseTime",
    "chargeUsePower",
    "chargeUsesPower",
    "mainStatus",
    "stopReason",
    "stopReasonDetailCode",
    "stopFailReason",
)


class VolvoApiError(RuntimeError):
    """An API request completed but Volvo returned an error response."""


def normalize_phone(phone: str) -> str:
    """Return an 11-digit mainland-China mobile number, without country code."""
    normalized = phone.strip().replace(" ", "").replace("-", "")
    if normalized.startswith("+86"):
        normalized = normalized[3:]
    elif normalized.startswith("0086"):
        normalized = normalized[4:]
    elif normalized.startswith("86") and len(normalized) == 13:
        normalized = normalized[2:]
    if not normalized.isdigit() or len(normalized) != 11:
        raise ValueError("手机号格式不正确：请输入 11 位中国大陆手机号")
    return normalized


def redact_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "***"


def json_bytes(value: Any) -> bytes:
    """Match a normal app JSON request while keeping Chinese text readable."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sdk_timestamp(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ")


def sdk_signature(url: str, method: str, app_key: str, app_secret: str,
                  date_stamp: str | None = None) -> dict[str, str]:
    """Create the SDK-HMAC-SHA256 signature used by the China API gateway."""
    parsed = urlparse(url)
    date_stamp = date_stamp or sdk_timestamp()
    canonical_uri = "/".join(quote(part, safe="") for part in parsed.path.split("/"))
    if not canonical_uri.endswith("/"):
        canonical_uri += "/"

    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    canonical_query = "&".join(
        f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in query_pairs
    )
    signing_headers = {
        "host": parsed.hostname or "",
        "x-sdk-content-sha256": UNSIGNED_PAYLOAD,
        "x-sdk-date": date_stamp,
    }
    signed_header_names = sorted(signing_headers)
    canonical_headers = "".join(
        f"{name}:{signing_headers[name].strip()}\n" for name in signed_header_names
    )
    canonical_request = (
        f"{method.upper()}\n{canonical_uri}\n{canonical_query}\n"
        f"{canonical_headers}\n{';'.join(signed_header_names)}\n{UNSIGNED_PAYLOAD}"
    )
    canonical_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"SDK-HMAC-SHA256\n{date_stamp}\n{canonical_hash}"
    signature = hmac.new(
        app_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "x-sdk-date": date_stamp,
        "v587sign": (
            f"SDK-HMAC-SHA256 Access={app_key}, "
            f"SignedHeaders={';'.join(signed_header_names)}, Signature={signature}"
        ),
    }


@dataclass(frozen=True)
class Tokens:
    access_token: str
    jwt_token: str


class VolvoClient:
    """Small, dependency-free client for the endpoints this exporter needs."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        retries: int = 2,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.timeout = timeout
        self.retries = retries
        self.opener = opener

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        tokens: Tokens | None = None,
    ) -> dict[str, Any]:
        url = f"{API_BASE_URL}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        payload = json_bytes(body) if body is not None else None
        signature = sdk_signature(url, method, self.app_key, self.app_secret)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json; charset=utf-8",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "User-Agent": "vca_ios/5.61.1" if tokens else "vca-android",
            "X-Ca-Version": "1.0",
            "x-sdk-content-sha256": UNSIGNED_PAYLOAD,
            "version": "5.53.1",
            **signature,
        }
        if tokens:
            headers.update(
                {
                    "authorization": f"Bearer {tokens.access_token}",
                    "X-Token": tokens.jwt_token,
                    "deviceid": "1d54acf8-51bf-42d3-844a-7e0edd65bc7d",
                    "uuid": "18714cea9d5387cea977ce600edfb7d2ea2e03a13255a6f0486ddc6caf05ea5a",
                    "platform": "iOS",
                    "x-ca-timestamp": str(int(time.time() * 1000)),
                }
            )
        request = Request(url, data=payload, headers=headers, method=method.upper())

        for attempt in range(self.retries + 1):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read()
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise VolvoApiError("接口返回的 JSON 不是对象")
                if not result.get("success"):
                    raise VolvoApiError(
                        str(result.get("errMsg") or result.get("msg") or "接口请求失败")
                    )
                return result
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                if attempt < self.retries and error.code >= 500:
                    time.sleep(2**attempt)
                    continue
                raise VolvoApiError(f"HTTP {error.code}: {detail[:500]}") from error
            except URLError as error:
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise VolvoApiError(f"网络请求失败：{error.reason}") from error
            except json.JSONDecodeError as error:
                raise VolvoApiError("接口返回了无法解析的 JSON") from error

        raise AssertionError("unreachable")

    def login(self, phone: str, password: str) -> Tokens:
        result = self._request(
            "POST",
            LOGIN_PATH,
            body={
                "authType": "password",
                "password": password,
                "phoneNumber": f"0086{normalize_phone(phone)}",
            },
        )
        data = result.get("data") or {}
        access_token = data.get("accessToken")
        jwt_token = data.get("jwtToken")
        if not access_token or not jwt_token:
            raise VolvoApiError("登录成功但响应缺少 accessToken 或 jwtToken")
        return Tokens(access_token=access_token, jwt_token=jwt_token)

    def list_piles(self, tokens: Tokens, phone: str) -> list[dict[str, Any]]:
        result = self._request("GET", PILE_LIST_PATH, query={"phone": phone}, tokens=tokens)
        data = result.get("data") or {}
        piles = data.get("brandPileList") or []
        if not isinstance(piles, list):
            raise VolvoApiError("充电桩列表格式异常")
        return [pile for pile in piles if isinstance(pile, dict)]

    def list_charge_history(self, tokens: Tokens, connector_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            CHARGE_HISTORY_PATH,
            tokens=tokens,
            body={
                "tradeNo": None,
                "orderNo": None,
                "phone": None,
                "memberId": None,
                "vin": None,
                "serviceProvider": None,
                "stationId": None,
                "stationName": None,
                "connectorId": connector_id,
                "startupType": None,
                "startTime": None,
                "endTime": None,
                "mainStatus": None,
            },
        )
        rows = result.get("data") or []
        if not isinstance(rows, list):
            raise VolvoApiError("充电记录列表格式异常")
        return [row for row in rows if isinstance(row, dict)]


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    write_atomically(path, lambda handle: _write_csv(handle, rows))


def _write_csv(handle: Any, rows: Iterable[dict[str, Any]]) -> None:
    # utf-8-sig lets Excel and WPS identify Chinese text correctly.
    handle.write("\ufeff")
    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: csv_value(row.get(field)) for field in CSV_FIELDS})


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    write_atomically(path, lambda handle: json.dump(rows, handle, ensure_ascii=False, indent=2))


def write_atomically(path: Path, write: Callable[[Any], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", delete=False, dir=path.parent, prefix=f".{path.name}."
    ) as handle:
        temporary_path = Path(handle.name)
        try:
            write(handle)
            handle.write("\n")
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    temporary_path.replace(path)


def get_secret(value: str | None, env_var: str, prompt: str) -> str:
    value = value or os.environ.get(env_var)
    if value:
        return value
    return getpass.getpass(f"{prompt}: ")


def get_value(value: str | None, env_var: str, prompt: str) -> str:
    value = value or os.environ.get(env_var)
    if value:
        return value
    return input(f"{prompt}: ").strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="登录沃尔沃汽车中国区账户，导出账号名下所有家充桩的充电记录。"
    )
    parser.add_argument("--phone", help="手机号；默认读取 VOLVO_PHONE")
    parser.add_argument("--password", help="账户密码；默认读取 VOLVO_PASSWORD，未提供时安全地交互输入")
    parser.add_argument(
        "--app-key",
        help="网关 App Key；默认读取 VOLVO_APP_KEY，均未提供时使用内置的官方 App 公共值",
    )
    parser.add_argument(
        "--app-secret",
        help="网关 App Secret；默认读取 VOLVO_APP_SECRET，均未提供时使用内置的官方 App 公共值",
    )
    parser.add_argument("--out", type=Path, default=Path("charge_history.csv"), help="CSV 输出路径（默认：charge_history.csv）")
    parser.add_argument("--json", type=Path, help="可选：原始 JSON 输出路径")
    parser.add_argument("--connector-id", action="append", default=[], help="仅导出指定 connectorId；可重复使用")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help=f"请求超时秒数（默认：{DEFAULT_TIMEOUT_SECONDS}）")
    parser.add_argument("--retries", type=int, default=2, help="网络/5xx 错误重试次数（默认：2）")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.timeout <= 0 or args.retries < 0:
        raise ValueError("--timeout 必须大于 0，--retries 不能小于 0")

    phone = normalize_phone(get_value(args.phone, "VOLVO_PHONE", "手机号"))
    password = get_secret(args.password, "VOLVO_PASSWORD", "账户密码")
    # App Key/Secret are not per-user secrets — they're the fixed gateway
    # signing credentials Volvo's own app ships with, so a built-in default
    # is safe here. --app-key/--app-secret (or the env vars) still let you
    # override them if Volvo ever rotates the value.
    app_key = args.app_key or os.environ.get("VOLVO_APP_KEY") or DEFAULT_APP_KEY
    app_secret = args.app_secret or os.environ.get("VOLVO_APP_SECRET") or DEFAULT_APP_SECRET
    if not password:
        raise ValueError("密码不能为空")

    client = VolvoClient(app_key, app_secret, timeout=args.timeout, retries=args.retries)
    print(f"正在登录 {redact_phone(phone)} …")
    tokens = client.login(phone, password)
    print("正在获取家充桩列表 …")
    piles = client.list_piles(tokens, phone)
    if args.connector_id:
        requested = set(args.connector_id)
        piles = [pile for pile in piles if str(pile.get("connectorId") or "") in requested]
        missing = requested - {str(pile.get("connectorId") or "") for pile in piles}
        if missing:
            raise VolvoApiError(f"未找到指定充电桩：{', '.join(sorted(missing))}")
    if not piles:
        raise VolvoApiError("该账户下没有绑定的家充桩")

    all_rows: list[dict[str, Any]] = []
    for pile in piles:
        connector_id = str(pile.get("connectorId") or "")
        if not connector_id:
            print("跳过缺少 connectorId 的充电桩记录", file=sys.stderr)
            continue
        name = str(pile.get("equipmentName") or pile.get("equipmentUserName") or connector_id)
        print(f"正在导出：{name} ({connector_id}) …")
        history = client.list_charge_history(tokens, connector_id)
        for row in history:
            enriched = dict(row)
            enriched.setdefault("equipmentName", name)
            enriched.setdefault("connectorId", connector_id)
            all_rows.append(enriched)
        print(f"  {len(history)} 条")

    write_csv(args.out, all_rows)
    print(f"已导出 {len(all_rows)} 条记录到 {args.out}")
    if args.json:
        write_json(args.json, all_rows)
        print(f"原始 JSON 已写入 {args.json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except (ValueError, VolvoApiError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
