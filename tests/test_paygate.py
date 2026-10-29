import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paygate import bazaar_extension, FakeFacilitator, Paygate, PriceTag, b64json_decode, b64json_encode  # noqa: E402
from paygate.x402 import usd_to_units  # noqa: E402
from paygate.server import build_gate, make_handler  # noqa: E402

PAY_TO = "0x" + "ab" * 20
PAYER = "0x" + "cd" * 20


def word_count(body):
    return {"words": len(str(body.get("text", "")).split())}


def make_gate(fac=None, tool=word_count):
    return Paygate(tool, PriceTag(PAY_TO, "0.001"), fac or FakeFacilitator(), "http://localhost/call", "test")


def signed(gate, nonce="n1", value=None, signature=None, to=PAY_TO, accepted=None):
    req = dict(gate.price.requirements)
    payload = {
        "x402Version": 2,
        "accepted": accepted if accepted is not None else req,
        "payload": {"signature": signature or "ok-" + nonce,
                    "authorization": {"from": PAYER, "to": to, "value": value or req["amount"], "nonce": nonce}},
    }
    return {"PAYMENT-SIGNATURE": b64json_encode(payload)}


class PriceTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(usd_to_units("0.001"), "1000")
        self.assertEqual(usd_to_units("0.01"), "10000")
        self.assertEqual(usd_to_units("1"), "1000000")
        with self.assertRaises(ValueError):
            usd_to_units("0")
        with self.assertRaises(ValueError):
            usd_to_units("0.0000001")

    def test_mainnet_refused_by_default(self):
        with self.assertRaises(ValueError):
            PriceTag(PAY_TO, network="eip155:8453")
        PriceTag(PAY_TO, network="eip155:8453", allow_mainnet=True)  # only on purpose

    def test_pay_to_must_be_address(self):
        for bad in ("", "abc", "0x123", "0x" + "a" * 64):
            with self.assertRaises(ValueError):
                PriceTag(bad)


class GateTests(unittest.TestCase):
    def test_unpaid_gets_402_with_header(self):
        status, headers, body = make_gate().handle({}, {"text": "a b"})
        self.assertEqual(status, 402)
        decoded = b64json_decode(headers["PAYMENT-REQUIRED"])
        self.assertEqual(decoded, body)
        acc = decoded["accepts"][0]
        self.assertEqual(decoded["x402Version"], 2)
        for k in ("scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra"):
            self.assertIn(k, acc)
        self.assertEqual(acc["amount"], "1000")
        self.assertEqual(acc["network"], "eip155:84532")

    def test_paid_gets_result_and_receipt(self):
        fac = FakeFacilitator()
        g = make_gate(fac)
        status, headers, body = g.handle(signed(g), {"text": "one two three"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], {"words": 3})
        self.assertTrue(b64json_decode(headers["PAYMENT-RESPONSE"])["success"])
        self.assertEqual(fac.calls, ["verify", "settle"])

    def test_header_name_is_case_insensitive(self):
        g = make_gate()
        h = {"payment-signature": signed(g)["PAYMENT-SIGNATURE"]}
        self.assertEqual(g.handle(h, {"text": "x"})[0], 200)

    def test_replay_refused(self):
        g = make_gate()
        h = signed(g, nonce="same")
        self.assertEqual(g.handle(h, {"text": "x"})[0], 200)
        status, _, body = g.handle(h, {"text": "x"})
        self.assertEqual(status, 402)
        self.assertEqual(body["error"], "payment_already_used")

    def test_bad_signature_refused_and_tool_not_run(self):
        ran = []

        def tool(b):
            ran.append(1)
            return 1

        fac = FakeFacilitator()
        g = make_gate(fac, tool)
        status, _, body = g.handle(signed(g, signature="forged"), {})
        self.assertEqual(status, 402)
        self.assertEqual(body["error"], "invalid_exact_evm_signature")
        self.assertEqual(ran, [])
        self.assertEqual(fac.calls, ["verify"])

    def test_underpay_refused(self):
        g = make_gate()
        accepted = dict(g.price.requirements, amount="1")
        self.assertEqual(g.handle(signed(g, accepted=accepted), {})[2]["error"], "invalid_payment_requirements")

    def test_wrong_recipient_refused(self):
        g = make_gate()
        accepted = dict(g.price.requirements, payTo="0x" + "ee" * 20)
        self.assertEqual(g.handle(signed(g, accepted=accepted), {})[0], 402)
        status, _, body = g.handle(signed(g, nonce="n9", to="0x" + "ee" * 20), {})
        self.assertEqual(body["error"], "invalid_exact_evm_payload_recipient_mismatch")

    def test_garbage_header(self):
        g = make_gate()
        self.assertEqual(g.handle({"PAYMENT-SIGNATURE": "%%%"}, {})[2]["error"], "invalid_payload")

    def test_settle_failure_returns_no_result(self):
        g = make_gate(FakeFacilitator(settle_fails=True))
        status, headers, body = g.handle(signed(g), {"text": "x"})
        self.assertEqual(status, 402)
        self.assertNotIn("result", body)

    def test_tool_error_does_not_settle_and_frees_nonce(self):
        fac = FakeFacilitator()

        def boom(b):
            raise RuntimeError("bad input")

        g = make_gate(fac, boom)
        h = signed(g, nonce="k")
        self.assertEqual(g.handle(h, {})[0], 400)
        self.assertEqual(fac.calls, ["verify"])
        g.tool = word_count
        self.assertEqual(g.handle(h, {"text": "ok"})[0], 200)  # same payment can be used after a tool error


class HttpTests(unittest.TestCase):
    def test_end_to_end_over_http(self):
        from http.server import ThreadingHTTPServer
        gate = build_gate(word_count, PAY_TO)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gate))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        url = "http://127.0.0.1:%d/call" % srv.server_address[1]
        try:
            data = json.dumps({"text": "a b c d"}).encode()
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=5)
            self.assertEqual(cm.exception.code, 402)
            req_hdr = cm.exception.headers["PAYMENT-REQUIRED"]
            accepted = b64json_decode(req_hdr)["accepts"][0]
            pay = {"x402Version": 2, "accepted": accepted,
                   "payload": {"signature": "ok-h1", "authorization": {"from": PAYER, "to": PAY_TO,
                                                                       "value": accepted["amount"], "nonce": "h1"}}}
            r = urllib.request.Request(url, data=data, method="POST",
                                       headers={"PAYMENT-SIGNATURE": b64json_encode(pay)})
            with urllib.request.urlopen(r, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read())["result"], {"words": 4})
                self.assertIn("PAYMENT-RESPONSE", resp.headers)
        finally:
            srv.shutdown()
            srv.server_close()


class DiscoveryTests(unittest.TestCase):
    def test_no_extension_by_default(self):
        self.assertNotIn("extensions", make_gate().payment_required()[2])

    def test_bazaar_metadata_in_402_header(self):
        ext = bazaar_extension({"text": "a b c"}, {"type": "object", "properties": {"text": {"type": "string"}}},
                               {"words": 3}, service_name="word count", tags=["text"])
        g = Paygate(word_count, PriceTag(PAY_TO, "0.001"), FakeFacilitator(), "http://localhost/call", "t",
                    discovery=ext)
        status, headers, _ = g.payment_required()
        self.assertEqual(status, 402)
        info = b64json_decode(headers["PAYMENT-REQUIRED"])["extensions"]["bazaar"]["info"]
        self.assertEqual(info["input"], {"type": "http", "method": "POST", "body": {"text": "a b c"},
                                         "inputSchema": {"type": "object",
                                                         "properties": {"text": {"type": "string"}}}})
        self.assertEqual(info["output"]["type"], "json")
        self.assertEqual(info["output"]["example"], {"words": 3})
        # the example in the listing really works when paid for
        self.assertEqual(g.handle(signed(g, nonce="d1"), info["input"]["body"])[2]["result"], {"words": 3})

    def test_get_uses_query_params(self):
        self.assertIn("queryParams", bazaar_extension({"q": "x"}, method="GET")["info"]["input"])

    def test_bad_discovery_refused(self):
        for kwargs in ({"input_example": {}}, {"input_example": {"a": 1}, "method": "PUT"},
                       {"input_example": {"a": 1}, "input_schema": {"properties": {}}},
                       {"input_example": {"a": 1}, "input_schema": {"type": "object", "$ref": "http://x/s.json"}},
                       {"input_example": {"a": 1}, "tags": list("abcdef")}):
            with self.assertRaises(ValueError):
                bazaar_extension(**kwargs)

    def test_price_below_facilitator_minimum_refused(self):
        with self.assertRaises(ValueError):
            PriceTag(PAY_TO, "0.0005")


class ConfigTests(unittest.TestCase):
    def test_config_file(self):
        import tempfile
        from paygate.server import load_config
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"_note": "comment", "pay_to": PAY_TO, "tool": "m:f", "port": 9000}, f)
            self.assertEqual(load_config(p), {"pay_to": PAY_TO, "tool": "m:f", "port": 9000})
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"pay_to": PAY_TO, "allow_mainnet": True}, f)
            with self.assertRaises(ValueError):  # mainnet cannot be switched on from a file
                load_config(p)


class RealToolTests(unittest.TestCase):
    def test_wraps_jp_invoice_check_when_available(self):
        sib = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "p012-jp-invoice-check-ai")
        if os.path.isdir(sib) and sib not in sys.path:
            sys.path.insert(0, sib)
        try:
            from jpinvoice import check_invoice
        except ImportError:
            self.skipTest("jpinvoice not installed")
        g = make_gate(tool=check_invoice)
        inv = {"issuer": {"name": "A", "registration_number": "T7123456789012"}, "date": "2026-10-28",
               "buyer": {"name": "B"}, "price_basis": "exclusive", "rounding": "floor",
               "items": [{"description": "x", "amount": 1999, "rate": 10}, {"description": "y", "amount": 2999, "rate": 10},
                         {"description": "z", "amount": 999, "rate": 10}],
               "totals": [{"rate": 10, "amount": 5997, "tax": 597}]}
        self.assertEqual(g.handle({}, inv)[0], 402)
        status, _, body = g.handle(signed(g, nonce="inv"), inv)
        self.assertEqual(status, 200)
        self.assertIn("E_TAX_LINE_ROUNDING", json.dumps(body["result"]))


if __name__ == "__main__":
    unittest.main()
