# Wanderwise — Architecture

![Architecture overview](architecture.jpg)

## 1. Component view

```mermaid
flowchart LR
    subgraph Client
      UI[ADK Web / A2A / Agent Runtime API]
      H([🧑 Human reviewer])
    end

    subgraph Runtime["Vertex AI Agent Runtime (us-central1)"]
      direction TB
      subgraph Plugins["App plugins (run on every call, in order)"]
        P1[SafetyGuardrailPlugin<br/>PII redaction · injection block]
        P2[ModelRouterPlugin<br/>tier policy · escalation · failover]
        P3[TravelTelemetryPlugin<br/>JSON logs · OTel metrics · span attrs]
        P4[BigQueryAgentAnalyticsPlugin]
      end
      subgraph Agents["Agent graph"]
        C["🔵 travel_planner<br/>concierge"]
        SEQ["trip_planning_pipeline<br/>Sequential → Parallel → Loop"]
      end
      subgraph Tools
        T1[geocode · weather · holidays · FX]
        T2[estimate / validate budget]
        T3[profile tools · save_trip_plan 🧑?]
        T4[request_booking_hold 🧑]
      end
    end

    subgraph Models["Gemini (global endpoint)"]
      L[🟢 LITE<br/>gemini-3.5-flash-lite]
      F[🔵 FLASH<br/>gemini-3.8-flash]
      PR[🟣 PRO<br/>gemini-3.1-pro-preview]
    end

    subgraph GCP["Google Cloud services"]
      MB[(Memory Bank)]
      SS[(Sessions)]
      GCS[(GCS artifacts)]
      CT[Cloud Trace]
      CL[Cloud Logging + Monitoring]
      BQ[(BigQuery analytics)]
    end

    UI --> Plugins --> Agents
    Agents --> Tools
    P2 -->|llm_request.model| L & F & PR
    T4 -. adk_request_confirmation .-> H
    T3 -. adk_request_confirmation .-> H
    C --> MB
    Agents --> SS
    T3 --> GCS
    P3 --> CT & CL
    P4 --> BQ
```

## 2. Strategic model routing

| Agent | Base tier | Dynamic rule (in `routing.decide`) |
|---|---|---|
| `travel_planner` (concierge) | 🔵 FLASH | → 🟢 LITE for short small-talk turns with no tools needed |
| `trip_request_extractor` | 🟢 LITE | — (structured extraction) |
| `destination_scout` | 🟢 LITE | — (deterministic API lookups) |
| `budget_analyst` | 🟢 LITE | — (deterministic cost tool) |
| `local_insights_scout` | 🔵 FLASH | → 🟣 PRO if complexity ≥ 3 |
| `itinerary_drafter` | 🟣 PRO | → 🔵 FLASH for simple trips ≤ 2 days |
| `budget_auditor` | 🔵 FLASH | → 🟣 PRO if complexity ≥ 3 |
| `itinerary_refiner` | 🔵 FLASH | → 🟣 PRO if > 15 % over budget or complexity ≥ 2 |
| `itinerary_presenter` | 🔵 FLASH | → 🟣 PRO if complexity ≥ 3 |

`trip_complexity` adds one point each for: trip ≥ 7 days, party ≥ 5,
≥ 3 constraints, cost estimate > 90 % of budget (tight).

```mermaid
flowchart TD
    A[before_model_callback] --> B{agent tier<br/>AGENT_TIERS}
    B --> C[score trip_complexity<br/>+ read budget_check]
    C --> D{decide}
    D -->|escalate| E[🟣 PRO]
    D -->|keep| F[base tier]
    D -->|de-escalate| G[🟢 LITE / 🔵 FLASH]
    E & F & G --> H[set llm_request.model<br/>log model_routed · span travel.model.*]
    H --> I[Gemini call]
    I -->|error| J[on_model_error_callback<br/>fail over PRO→FLASH→LITE<br/>log model_failover]
```

## 3. Human-in-the-loop checkpoints

```mermaid
sequenceDiagram
    autonumber
    actor U as Traveler / reviewer
    participant C as travel_planner
    participant T as request_booking_hold
    participant S as user: state
    U->>C: "Book a hold for that trip, cap $900"
    C->>T: request_booking_hold(trip, email, 900)
    T->>T: validate inputs, audited cost present?
    T-->>U: adk_request_confirmation(hint, payload={max_total_usd: 900})
    Note over U: Approve / reject, optionally edit cap
    U->>T: FunctionResponse {confirmed: true, payload: {max_total_usd: 950}}
    T->>T: cap ≥ audited cost?
    T->>S: append user:booking_requests (WW-xxxx)
    T-->>C: {status: submitted, reference}
    C-->>U: "Hold WW-1A2B submitted ✅"
```

The same mechanism guards `save_trip_plan`: when the budget loop ends **still
over budget**, `require_confirmation=needs_budget_override` pauses the save so
a human must explicitly accept the overspend. Unaudited saves are blocked
outright by the `require_budget_approval` policy callback.

All checkpoints emit `hitl_requested`, `hitl_approved`, `hitl_rejected`
audit events (structured logs → log-based metrics), with emails masked.
