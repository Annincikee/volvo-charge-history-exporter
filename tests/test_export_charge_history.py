from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


SCRIPT = Path(__file__).parents[1] / "export_charge_history.py"
SPEC = importlib.util.spec_from_file_location("export_charge_history", SCRIPT)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)


class Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class ExportChargeHistoryTests(unittest.TestCase):
    def test_normalize_phone(self) -> None:
        phone = "138" + "0013" + "8000"
        self.assertEqual(exporter.normalize_phone("+86 138-0013-8000"), phone)
        with self.assertRaises(ValueError):
            exporter.normalize_phone("123")

    def test_signature_is_stable_for_fixed_time(self) -> None:
        signature = exporter.sdk_signature(
            "https://apigateway.digitalvolvo.com/app/iam/api/v1/auth",
            "POST",
            "key",
            "secret",
            "20260716T000000Z",
        )
        self.assertEqual(signature["x-sdk-date"], "20260716T000000Z")
        self.assertIn("Access=key", signature["v587sign"])
        self.assertIn("SignedHeaders=host;x-sdk-content-sha256;x-sdk-date", signature["v587sign"])
        self.assertIn("Signature=f0ed7dce8d250826a072de5c7fabea4ec6233c39615dddd59f8915707e543471", signature["v587sign"])

    def test_client_sends_login_then_pile_request(self) -> None:
        phone = "138" + "0013" + "8000"
        requests = []
        payloads = iter(
            [
                b'{"success":true,"data":{"accessToken":"access","jwtToken":"jwt"}}',
                b'{"success":true,"data":{"brandPileList":[{"connectorId":"c-1"}]}}',
            ]
        )

        def opener(request, *, timeout):
            requests.append((request, timeout))
            return Response(next(payloads))

        client = exporter.VolvoClient("key", "secret", opener=opener, retries=0)
        tokens = client.login(phone, "password")
        piles = client.list_piles(tokens, phone)
        self.assertEqual(piles, [{"connectorId": "c-1"}])
        login_body = requests[0][0].data.decode("utf-8")
        self.assertIn(f'"phoneNumber":"0086{phone}"', login_body)
        pile_url = urlparse(requests[1][0].full_url)
        self.assertEqual(parse_qs(pile_url.query), {"phone": [phone]})
        self.assertEqual(requests[1][0].get_header("Authorization"), "Bearer access")

    def test_csv_contains_bom_and_escapes_chinese(self) -> None:
        target = io.StringIO(newline="")
        exporter._write_csv(target, [{"equipmentName": "车桩, A", "connectorId": "c1"}])
        self.assertTrue(target.getvalue().startswith("\ufeffequipmentName"))
        self.assertIn('"车桩, A"', target.getvalue())

    def test_password_cannot_be_passed_on_command_line(self) -> None:
        with self.assertRaises(SystemExit):
            exporter.parse_args(["--password", "do-not-store-in-shell-history"])

    def test_identifier_mask_is_stable_and_non_revealing(self) -> None:
        raw = "connector-private-123"
        masked = exporter.mask_identifier(raw)
        self.assertEqual(masked, exporter.mask_identifier(raw))
        self.assertTrue(masked.startswith("id-"))
        self.assertNotIn(raw, masked)

    def test_raw_json_requires_explicit_acknowledgement(self) -> None:
        args = exporter.parse_args(["--json", "raw.json"])
        with self.assertRaisesRegex(ValueError, "acknowledge-sensitive-json"):
            exporter.run(args)

    def test_http_error_body_is_not_exposed(self) -> None:
        phone = "138" + "0013" + "8000"
        vin = "LVSHFAAL" + "1MF" + "123456"
        secret_body = json.dumps({"phone": phone, "vin": vin}).encode()

        def opener(request, *, timeout):
            raise HTTPError(
                request.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=io.BytesIO(secret_body),
            )

        client = exporter.VolvoClient("key", "secret", opener=opener, retries=0)
        with self.assertRaises(exporter.VolvoApiError) as raised:
            client._request("GET", "/private")

        message = str(raised.exception)
        self.assertIn("HTTP 403", message)
        self.assertNotIn(phone, message)
        self.assertNotIn(vin, message)

    def test_business_error_message_is_not_exposed(self) -> None:
        phone = "138" + "0013" + "8000"
        response = Response(
            json.dumps(
                {"success": False, "errMsg": f"account {phone} rejected"}
            ).encode()
        )
        client = exporter.VolvoClient(
            "key", "secret", opener=lambda request, timeout: response, retries=0
        )

        with self.assertRaises(exporter.VolvoApiError) as raised:
            client._request("GET", "/private")

        self.assertNotIn(phone, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
