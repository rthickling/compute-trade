# Simple Binary Encoding

The headers in `include/compute_trade/generated/` are generated without manual
edits from `schema/compute-trade.xml` using SBE 1.41.0:

- Upstream: https://github.com/aeron-io/simple-binary-encoding/tree/1.41.0
- Generator: `uk.co.real-logic:sbe-all:1.41.0`
- SHA-256: `729b4271beb577f380380a1793e60f156ff75859b08ea603c4e2ae6b5e9a04ca`
- Upstream license: Apache License 2.0, reproduced in [LICENSE](LICENSE).

Run `python3 tools/generate_sbe.py` to regenerate. Do not edit the generated
headers directly. The generator jar and Java runtime are not vendored.
