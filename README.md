# x402 Paygate

Put a pay-per-call price on any Python function. An unpaid call gets HTTP 402 with the price. A paid call gets the result.
Standard library only. No keys, no signing, no money handling inside: a facilitator checks and settles the payment.

日本語: 任意の Python の関数に「1回いくら」の値段をつける小さなHTTPサーバーです。払っていない呼び出しには 402 と値段を返し、払った呼び出しにだけ結果を返します。標準ライブラリのみ。鍵や署名は扱いません(確認役 facilitator の仕事)。

## Status

Stage 1 of 3. Tested with a fake facilitator only. It has not taken a real payment, on testnet or mainnet.
Mainnet networks are refused unless you pass `allow_mainnet=True` yourself.

## How a call goes (x402 v2)

1. `POST /call` without payment → `402` and header `PAYMENT-REQUIRED` (Base64 JSON with `accepts[]`).
2. The client signs and retries with header `PAYMENT-SIGNATURE`.
3. The gate checks the chosen offer matches its own (scheme, network, asset, payTo, amount), then asks the facilitator `/verify`.
4. It runs your function, then asks the facilitator `/settle`.
5. Only if settle succeeds: `200`, `{"result": ...}` and header `PAYMENT-RESPONSE`.

Rules the tests check:

| Case | Result |
|---|---|
| No payment | 402 with price |
| Valid payment | 200 + result + receipt |
| Same payment sent twice | 402 `payment_already_used` |
| Forged signature | 402, function not run |
| Amount lower than the price, wrong recipient | 402 |
| Settle fails | 402, no result |
| Your function raises | 400, no settle, the payment can be reused |

## Run (testnet, fake facilitator, no money)

```bash
python -m paygate --tool yourmodule:yourfunction --pay-to 0xYOUR_TESTNET_RECEIVING_ADDRESS --facilitator fake
```

- `--price` USD per call (default `0.001` = `"1000"` USDC units)
- `--network` default `eip155:84532` (Base Sepolia); `--asset` default Base Sepolia USDC `0x036CbD53842c5426634e7929541eC2318f3dCF7e`
- `--facilitator https://...` to use a real facilitator. The gate POSTs `{"x402Version":2,"paymentPayload":...,"paymentRequirements":...}` to `/verify` and `/settle`.
- `--config file.json` takes the same options as JSON (`pay_to`, `tool`, `price`, `facilitator`, `network`, `asset`, `example`, `host`, `port`). The command line wins. Mainnet cannot be switched on from a file.
- `--pay-to` is a receiving address. Never put a private key or seed phrase anywhere near this tool.

In Python:

```python
from paygate import Paygate, PriceTag, HttpFacilitator
gate = Paygate(my_function, PriceTag("0xRECEIVING_ADDRESS", "0.001"),
               HttpFacilitator("https://your-facilitator.example"), "https://you.example/call")
status, headers, body = gate.handle(request_headers, request_json)
```

## Get listed (Bazaar discovery)

Agents find x402 tools through facilitators that index the Bazaar extension (`GET {facilitator}/discovery/resources`) and directories built on it, such as Coinbase's Agentic.Market. A tool is indexed only if its 402 carries `extensions.bazaar` with an example call. Give one:

```python
from paygate import bazaar_extension
disc = bazaar_extension({"text": "a b c"},                      # example body an agent can copy
                        {"type": "object", "properties": {"text": {"type": "string"}}},
                        output_example={"words": 3}, service_name="word count", tags=["text"])
gate = Paygate(my_function, price, facilitator, url, "Count words", discovery=disc)
```

or on the command line: `--example '{"text": "a b c"}'`.

- A test pays for the listed example and checks it returns the listed output, so the listing does not lie.
- Not confirmed in the docs: the key name for a POST body example (`body` here). Settling a payment does not guarantee a listing.
- Prices under $0.001 are refused: the CDP facilitator rejects them with `invalid_payload` (reported by the x402station probe, April 2026).

日本語: 402 の応答に Bazaar 拡張(呼び出しの例)を入れると、確認役の索引と Agentic.Market などの一覧に載る候補になります。$0.001 未満の値段は確認役に断られるため受け付けません。

## Limits

- Replay memory is in-process. Restarting the server forgets seen payments; use one process or put a shared store in front.
- Only the `exact` scheme is offered.
- Field names follow docs.x402.org and the Fireblocks x402 reference as read on 2026-10-28. Check the current spec before mainnet.

## Tests

```bash
python -m unittest discover -s tests
```

21 tests. One wraps JP Invoice Check if it sits next to this folder, and is skipped otherwise.

## License

MIT
