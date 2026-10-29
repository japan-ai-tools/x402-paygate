"""Minimal x402 v2 resource-server logic.

What this module does (and does not do):
- It answers an unpaid request with HTTP 402 and a PAYMENT-REQUIRED header (Base64 JSON).
- It reads the client's PAYMENT-SIGNATURE header, checks that the chosen offer matches ours,
  asks a facilitator to /verify it, runs the tool, asks the facilitator to /settle it,
  and only then returns the result with a PAYMENT-RESPONSE header.
- It never signs, holds keys, or moves money itself. Signature and on-chain checks belong to the facilitator.
- It refuses a payment payload it has already seen (replay guard, in memory).
- Mainnet networks are refused unless the operator sets allow_mainnet=True on purpose.

Protocol references: https://docs.x402.org/core-concepts/http-402 (headers),
https://docs.x402.org/core-concepts/facilitator (verify/settle roles).
"""
import base64
import hashlib
import json
import threading
import urllib.request

X402_VERSION = 2
TESTNET_NETWORK = "eip155:84532"  # Base Sepolia (CAIP-2)
TESTNET_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # USDC on Base Sepolia (Circle docs). No real value.
MAINNET_NETWORKS = {"eip155:8453", "eip155:1", "eip155:137", "eip155:42161", "eip155:10"}
USDC_DECIMALS = 6
MIN_AMOUNT_USD = "0.001"  # the CDP facilitator rejects smaller payments with invalid_payload (x402station probe, 2026-04)

H_REQUIRED = "PAYMENT-REQUIRED"
H_SIGNATURE = "PAYMENT-SIGNATURE"
H_RESPONSE = "PAYMENT-RESPONSE"


def b64json_encode(obj):
    return base64.b64encode(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")


def b64json_decode(text):
    try:
        raw = base64.b64decode(text.strip(), validate=True)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("header is not Base64-encoded JSON") from exc


def usd_to_units(usd, decimals=USDC_DECIMALS):
    """'0.001' -> '1000' (USDC has 6 decimals). Uses strings to avoid float error."""
    s = str(usd)
    whole, _, frac = s.partition(".")
    if len(frac) > decimals:
        raise ValueError("price has more decimals than the asset")
    units = int(whole or "0") * 10 ** decimals + int((frac + "0" * decimals)[:decimals] or "0")
    if units <= 0:
        raise ValueError("price must be positive")
    return str(units)


class PriceTag:
    """One offer in accepts[]. amount is in the asset's smallest unit, as a string."""

    def __init__(self, pay_to, amount_usd="0.001", network=TESTNET_NETWORK, asset=TESTNET_USDC,
                 scheme="exact", max_timeout_seconds=60, asset_name="USDC", asset_version="2",
                 allow_mainnet=False):
        if not pay_to or not str(pay_to).startswith("0x") or len(str(pay_to)) != 42:
            raise ValueError("pay_to must be a 0x-prefixed 20-byte address (a receiving address, never a key)")
        if int(usd_to_units(amount_usd)) < int(usd_to_units(MIN_AMOUNT_USD)):
            raise ValueError("price below $0.001: facilitators may reject it as invalid_payload")
        if network in MAINNET_NETWORKS and not allow_mainnet:
            raise ValueError("mainnet network refused: set allow_mainnet=True only after the owner approves real money")
        self.requirements = {
            "scheme": scheme,
            "network": network,
            "amount": usd_to_units(amount_usd),
            "asset": asset,
            "payTo": pay_to,
            "maxTimeoutSeconds": int(max_timeout_seconds),
            "extra": {"name": asset_name, "version": asset_version},
        }
        self.amount_usd = str(amount_usd)

    def matches(self, accepted):
        if not isinstance(accepted, dict):
            return False
        r = self.requirements
        keys = ("scheme", "network", "asset", "payTo")
        if any(str(accepted.get(k, "")).lower() != str(r[k]).lower() for k in keys):
            return False
        return str(accepted.get("amount")) == r["amount"]


class FakeFacilitator:
    """Stand-in facilitator for tests and local demos. Moves no money.

    A payload is 'valid' when payload.payload.signature == 'ok-<nonce>' and authorization.value covers the amount.
    """

    def __init__(self, settle_fails=False):
        self.calls = []
        self.settle_fails = settle_fails

    def _check(self, payment_payload, requirements):
        p = (payment_payload or {}).get("payload") or {}
        auth = p.get("authorization") or {}
        if p.get("signature") != "ok-" + str(auth.get("nonce", "")):
            return False, "invalid_exact_evm_signature"
        if str(auth.get("to", "")).lower() != requirements["payTo"].lower():
            return False, "invalid_exact_evm_payload_recipient_mismatch"
        if int(auth.get("value", 0)) < int(requirements["amount"]):
            return False, "invalid_exact_evm_payload_authorization_value_mismatch"
        return True, None

    def verify(self, payment_payload, requirements):
        self.calls.append("verify")
        ok, reason = self._check(payment_payload, requirements)
        payer = ((payment_payload.get("payload") or {}).get("authorization") or {}).get("from")
        return {"isValid": ok, "invalidReason": reason, "payer": payer}

    def settle(self, payment_payload, requirements):
        self.calls.append("settle")
        payer = ((payment_payload.get("payload") or {}).get("authorization") or {}).get("from")
        if self.settle_fails:
            return {"success": False, "errorReason": "unexpected_settle_error", "transaction": "",
                    "network": requirements["network"], "payer": payer}
        return {"success": True, "transaction": "0xfake" + "0" * 60, "network": requirements["network"],
                "payer": payer}


class HttpFacilitator:
    """Talks to a real facilitator over HTTPS: POST {base}/verify and {base}/settle.

    Body: {"x402Version": 2, "paymentPayload": ..., "paymentRequirements": ...}.
    """

    def __init__(self, base_url, timeout=20):
        if not base_url.startswith("https://"):
            raise ValueError("facilitator must be https")
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path, payment_payload, requirements):
        body = json.dumps({"x402Version": X402_VERSION, "paymentPayload": payment_payload,
                           "paymentRequirements": requirements}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (https only)
            return json.loads(resp.read().decode("utf-8"))

    def verify(self, payment_payload, requirements):
        return self._post("/verify", payment_payload, requirements)

    def settle(self, payment_payload, requirements):
        return self._post("/settle", payment_payload, requirements)


def bazaar_extension(input_example, input_schema=None, output_example=None, output_schema=None,
                     method="POST", service_name=None, tags=None):
    """Discovery metadata for the x402 Bazaar extension (https://docs.x402.org/extensions/bazaar).

    Returned dict goes under extensions.bazaar in the 402 body, so facilitators that index
    the Bazaar (and directories built on it) can list the tool with an example call.
    Not confirmed in the docs: the key that carries a POST body example ('body' here).
    """
    method = str(method).upper()
    if method not in ("GET", "POST"):
        raise ValueError("method must be GET or POST")
    if not isinstance(input_example, dict) or not input_example:
        raise ValueError("input_example must be a non-empty dict: agents copy it to make the first call")
    inp = {"type": "http", "method": method}
    inp["queryParams" if method == "GET" else "body"] = input_example
    if input_schema is not None:
        _check_schema(input_schema)
        inp["inputSchema"] = input_schema
    info = {"input": inp}
    if output_example is not None or output_schema is not None:
        out = {"type": "json"}
        if output_example is not None:
            out["example"] = output_example
        if output_schema is not None:
            _check_schema(output_schema)
            out["schema"] = output_schema
        info["output"] = out
    ext = {"info": info}
    if service_name:
        ext["serviceName"] = str(service_name)[:100]
    if tags:
        tags = [str(t)[:40] for t in tags]
        if len(tags) > 5:
            raise ValueError("at most 5 tags")
        ext["tags"] = tags
    return ext


def _check_schema(schema):
    if not isinstance(schema, dict) or "type" not in schema:
        raise ValueError("schema must be a JSON Schema object with a 'type'")
    for ref in _refs(schema):
        if not ref.startswith("#"):
            raise ValueError("schema $ref must be a same-document pointer starting with '#'")


def _refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


class Paygate:
    """Wrap one tool. handle() returns (status, headers, body_dict)."""

    def __init__(self, tool, price, facilitator, resource_url, description="", mime_type="application/json",
                 discovery=None):
        self.discovery = discovery
        self.tool = tool
        self.price = price
        self.facilitator = facilitator
        self.resource = {"url": resource_url, "description": description, "mimeType": mime_type}
        self._seen = set()
        self._lock = threading.Lock()

    def payment_required(self, error=None):
        body = {"x402Version": X402_VERSION, "resource": self.resource, "accepts": [self.price.requirements]}
        if self.discovery:
            body["extensions"] = {"bazaar": self.discovery}
        if error:
            body["error"] = error
        return 402, {H_REQUIRED: b64json_encode(body)}, body

    @staticmethod
    def _fingerprint(payload):
        inner = payload.get("payload") or {}
        auth = inner.get("authorization") or {}
        key = json.dumps([auth.get("from"), auth.get("nonce"), inner.get("signature")], sort_keys=True)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def handle(self, headers, request_body):
        h = {k.upper(): v for k, v in (headers or {}).items()}
        sig = h.get(H_SIGNATURE) or h.get("X-PAYMENT")
        if not sig:
            return self.payment_required()
        try:
            payload = b64json_decode(sig)
        except ValueError:
            return self.payment_required("invalid_payload")
        if payload.get("x402Version") != X402_VERSION:
            return self.payment_required("unsupported x402Version")
        if not self.price.matches(payload.get("accepted")):
            return self.payment_required("invalid_payment_requirements")
        fp = self._fingerprint(payload)
        with self._lock:
            if fp in self._seen:
                return self.payment_required("payment_already_used")
            self._seen.add(fp)  # reserve before calling out, so two parallel retries cannot both pass
        req = self.price.requirements
        try:
            v = self.facilitator.verify(payload, req)
        except Exception:  # noqa: BLE001
            self._release(fp)
            return self.payment_required("unexpected_verify_error")
        if not v.get("isValid"):
            self._release(fp)
            return self.payment_required(v.get("invalidReason") or "invalid_payload")
        try:
            result = self.tool(request_body)
        except Exception as exc:  # noqa: BLE001
            self._release(fp)  # tool failed: do not settle, the client keeps its money
            return 400, {}, {"error": "tool_error", "detail": str(exc)[:200]}
        try:
            s = self.facilitator.settle(payload, req)
        except Exception:  # noqa: BLE001
            s = {"success": False, "errorReason": "unexpected_settle_error"}
        if not s.get("success"):
            return self.payment_required(s.get("errorReason") or "unexpected_settle_error")
        return 200, {H_RESPONSE: b64json_encode(s)}, {"result": result}

    def _release(self, fp):
        with self._lock:
            self._seen.discard(fp)
