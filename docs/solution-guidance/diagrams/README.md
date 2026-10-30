# Architecture diagrams

These figures are **diagrams as code**, authored with the
[mingrammer `diagrams`](https://github.com/mingrammer/diagrams) library and
rendered to PNG by [Graphviz](https://graphviz.org/). Each `diagramN_*.py`
script is the **editable source of truth** for the corresponding
`diagramN-*.png`.

| Script | Output | Used in |
|---|---|---|
| `diagram1_architecture_overview.py` | `diagram1-architecture-overview.png` | blog · reference-architecture (Fig 1) · README |
| `diagram2_store_side_detail.py` | `diagram2-store-side-detail.png` | blog · reference-architecture (Fig 2) |
| `diagram3_tool_invocation_flow.py` | `diagram3-tool-invocation-flow.png` | blog · reference-architecture (Fig 3) |
| `diagram4_proactive_alerting.py` | `diagram4-proactive-alerting.png` | reference-architecture (Fig 4) |

## Regenerate

Prerequisites: Graphviz (`dot`) on `PATH`, plus the `diagrams` package.

```bash
# system dependency (once)
dnf install -y graphviz        # or: brew install graphviz

# python dependency (into the repo venv)
.venv/bin/pip install -e ".[docs]"

# render — run from the REPO ROOT so the output paths resolve
.venv/bin/python docs/solution-guidance/diagrams/diagram1_architecture_overview.py
.venv/bin/python docs/solution-guidance/diagrams/diagram2_store_side_detail.py
.venv/bin/python docs/solution-guidance/diagrams/diagram3_tool_invocation_flow.py
.venv/bin/python docs/solution-guidance/diagrams/diagram4_proactive_alerting.py
```

## Conventions

All four scripts import shared styling from **`_style.py`**, which encodes the
[AWS Architecture Icons](https://aws.amazon.com/architecture/icons/) usage
guidelines we can honor from Graphviz:

- **Groups** get real border treatments — **AWS Cloud** = solid dark-slate
  (`#232F3E`) border; **on-premises / corporate data center** = gray, dashed;
  neutral logical sub-groups = light dashed. Topology nests the way the real
  one does (e.g. IoT Core sits inside the AWS Cloud group; the gateway / MCP /
  simulation sub-groups nest inside the on-premises group).
- **Labels** are always present and styled Arial, dark slate (`#232F3E`), ~12 pt.
- **Connectors** are direct straight segments (`splines="polyline"`) with arrows
  showing flow — not tangled diagonals. (`splines="ortho"` was tried but its
  right-angle routing collided with the cross-group edge labels.)
- **Whitespace** — generous `nodesep` / `ranksep` and group `margin`.
- **AWS icon sets** (`diagrams.aws.*`) are used wherever an official service
  icon exists (IoT Core, Lambda, Bedrock, DynamoDB, EventBridge, Step
  Functions, EC2). Custom on-prem components (store gateway, MCP servers,
  simulation engine) use the neutral `diagrams.aws.general.TraditionalServer` /
  `GenericDatabase` icons — never a borrowed service icon. Icons are the
  official toolkit art, used as-is (never recolored, stretched, or rotated).
- The bold **green** edge marks the store's single, store-initiated outbound
  mTLS connection (port 443 / ALPN, or 8883).
- Diagrams 1 and 3 depict the **current production path** (spec 022
  "Correlation A"): the chat backend runs the AgentCore harness on the
  inline-tool path and executes each tool over an outbound IoT round-trip
  (`iot-data:Publish` + a pooled SigV4 MQTT-over-WebSocket response
  subscription). The alternative durable relay path ("Correlation B":
  tool-relay Lambdas + IoT Topic Rule + `iot-response-handler` + DynamoDB) is
  also deployed and is described in the reference-architecture prose.

> The `.drawio` files in this directory are **legacy artifacts, not sources**.
> `diagram1-architecture-overview.drawio`, `diagram2-store-side-detail.drawio`
> and `diagram3-tool-invocation-flow.drawio` are superseded by the scripts above
> (diagram 4 never had one), and `infrastructure-architecture.drawio` /
> `mqtt-data-flow.drawio` are referenced by nothing. Edit the `.py` scripts, not
> these. A previous version of this note claimed `voice-*.drawio` files "remain
> the source for the voice / proactive proposal docs" — no such files exist, and
> those docs contain no figures at all.
