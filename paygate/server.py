"""Tiny HTTP server: POST /call with a JSON body -> the wrapped tool, behind an x402 gate.

Example (testnet only, fake facilitator, no money):
  python -m paygate --tool jpinvoice:check_invoice --pay-to 0xYourTestnetAddress --facilitator fake
"""
import argparse
import importlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .x402 import bazaar_extension, FakeFacilitator, HttpFacilitator, Paygate, PriceTag, TESTNET_NETWORK, TESTNET_USDC

MAX_BODY = 256 * 1024


def load_tool(spec):
    mod, _, fn = spec.partition(":")
    if not mod or not fn:
        raise ValueError("--tool must look like module:function")
    return getattr(importlib.import_module(mod), fn)


def make_handler(gate):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, headers, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path == "/call":
                return self._send(*gate.payment_required())
            return self._send(404, {}, {"error": "not_found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/call":
                return self._send(404, {}, {"error": "not_found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {}, {"error": "body_too_large"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "null")
            except ValueError:
                return self._send(400, {}, {"error": "invalid_json"})
            return self._send(*gate.handle(dict(self.headers.items()), body))

        def log_message(self, *args):  # keep request bodies out of logs
            pass

    return Handler


def build_gate(tool, pay_to, price="0.001", facilitator="fake", network=TESTNET_NETWORK, asset=TESTNET_USDC,
               resource_url="http://127.0.0.1:8402/call", description="", allow_mainnet=False, example=None):
    fac = FakeFacilitator() if facilitator == "fake" else HttpFacilitator(facilitator)
    tag = PriceTag(pay_to, amount_usd=price, network=network, asset=asset, allow_mainnet=allow_mainnet)
    disc = bazaar_extension(example, method="POST") if example else None
    return Paygate(tool, tag, fac, resource_url, description, discovery=disc)


CONFIG_KEYS = ("tool", "pay_to", "price", "facilitator", "network", "asset", "example", "host", "port")


def load_config(path):
    """JSON file with the same names as the options (pay_to, not --pay-to). Unknown keys are refused."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    unknown = sorted(k for k in cfg if k not in CONFIG_KEYS and not k.startswith("_"))
    if unknown:
        raise ValueError("unknown config keys: " + ", ".join(unknown))
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paygate", description="x402 pay-per-call gate for one Python function")
    ap.add_argument("--config", default=None, help="JSON file with the options below (command line wins)")
    ap.add_argument("--tool", help="module:function that takes the JSON body")
    ap.add_argument("--pay-to", help="receiving address (never a private key)")
    ap.add_argument("--price", help="USD per call, default 0.001")
    ap.add_argument("--facilitator", help="'fake' (default) or an https facilitator base URL")
    ap.add_argument("--network", help="default eip155:84532 (Base Sepolia)")
    ap.add_argument("--asset", help="default Base Sepolia USDC")
    ap.add_argument("--example", help="JSON example body; adds Bazaar discovery metadata to the 402")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = vars(ap.parse_args(argv))
    o = {"price": "0.001", "facilitator": "fake", "network": TESTNET_NETWORK, "asset": TESTNET_USDC,
         "example": None, "host": "127.0.0.1", "port": 8402}
    cfg_path = a.pop("config")
    if cfg_path:
        o.update(load_config(cfg_path))
    for k, v in a.items():
        if v is not None:
            o[k] = json.loads(v) if k == "example" else v
    if not o.get("tool") or not o.get("pay_to"):
        ap.error("--tool and --pay-to are required (on the command line or in --config)")
    url = "http://%s:%d/call" % (o["host"], int(o["port"]))
    gate = build_gate(load_tool(o["tool"]), o["pay_to"], str(o["price"]), o["facilitator"], o["network"],
                      o["asset"], url, o["tool"], example=o["example"])
    print("x402 paygate on %s  network=%s  price=$%s  facilitator=%s" % (url, o["network"], o["price"],
                                                                         o["facilitator"]))
    ThreadingHTTPServer((o["host"], int(o["port"])), make_handler(gate)).serve_forever()

if __name__ == "__main__":
    main()
