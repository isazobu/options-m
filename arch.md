# options-m — Mimari ve Akışlar

> Commit `66befff` · 2026-08-29  
> Bu belge **mevcut çalışan kodu** yansıtır; plan dokümanlarını değil.

---

## 1. Büyük Resim

```
┌──────────────────────────────────────────────────────────────────┐
│                        asyncio process                           │
│                                                                  │
│   MarketPulseAgent    60 s  ──┐                                  │
│   PositionManagerAgent 60 s  ─┤──► Local Postgres (Neon)        │
│   ExecutionAgent       30 s  ─┤       cache tables              │
│   StrategistAgent       5 m  ─┤       proposals / orders        │
│   ReflectionAgent      60 m  ─┘       lessons                   │
│                                                                  │
│   FastAPI (HTTP :8080)  ─────────────────► dashboard / /api/*   │
└──────────────────────────────────────────────────────────────────┘
         │
         │ stdio subprocess
         ▼
   Alpaca MCP Server (alpaca-mcp-server v2.3.0)
         │
         ▼
   Alpaca Paper Account (Trading API)
```

**Tek broker arayüzü:** `AlpacaMcp`. Hiçbir agent ham HTTP client tutmaz.  
**Tek state bus:** Postgres. Agent'lar arası her bağımlılık bir cache tablosu okumasıdır, asla doğrudan çağrı değil.  
**LLM:** Featherless (OpenAI-compatible). Sadece `StrategistAgent` ve `ReflectionAgent` çağırır; ikisi de `httpx` ile doğrudan konuşur, SDK yok.

---

## 2. Agent Tablosu

| Agent | Kadans | LLM | MCP çağrıları | Yazdığı tablolar |
|---|---|---|---|---|
| `MarketPulseAgent` | 60 s | ✗ | `get_account_info`, `get_account_config`, `get_calendar`, `get_stock_snapshot`, `get_stock_bars`, `get_option_chain`, `get_option_contracts`, `get_all_positions`, `get_news` | `account`, `market_calendar`, `evidence`, `candidates`, `equity_curve`, `iv_history` |
| `PositionManagerAgent` | 60 s | ✗ | `get_all_positions` | `positions` |
| `ExecutionAgent` | 30 s | ✗ | `get_account_info`, `get_stock_snapshot`, `get_option_contracts`, `get_option_chain`, `get_open_position`, `place_option_order`, `get_order_by_client_id` | `orders`, `proposals`, `risk_events` |
| `StrategistAgent` | 5 m | ✓ tek çağrı | **sıfır** | `proposals`, `llm_calls` |
| `ReflectionAgent` | 60 m | ✓ lesson başına | **sıfır** | `lessons` |

---

## 3. Local Cache Tabloları

Her tablonun **tek bir writer'ı** vardır. Diğer tüm agent'lar Alpaca'ya gitmek yerine yerel cache'i okur.

| Tablo | Tek writer | Yenileme | Kabul edilen risk |
|---|---|---|---|
| `market_calendar` | `MarketPulseAgent` | Başlangıçta, pencere marginin altına düşünce tekrar | Plansız devre kesici haltını yakalamaz |
| `account` | `MarketPulseAgent` | Her 60 s | ~60 s stale |
| `evidence` | `MarketPulseAgent` | Her 60 s, sembol başına 1 satır, yerinde üzerine yazılır | ~60 s stale; StrategistAgent zaten 5 dk'da bir çalışır |
| `positions` | `PositionManagerAgent` | Her 60 s, unrealized P&L dahil | ~60 s stale |
| `orders` | `ExecutionAgent` | Her durum değişikliğinde write-through | Yok |

---

## 4. MarketPulseAgent Akışı (her 60 s)

```
MarketPulseAgent._run()
│
├── _ensure_calendar_fresh()
│     get_calendar(start, end)          ← ~1 yıl ileri pencere
│     ──► market_calendar UPSERT
│         (margin altına düşene kadar bir daha çağrılmaz)
│
├── get_account_info() + get_account_config()
│     ──► account UPSERT
│         equity, cash, buying_power, options_trading_level
│     ──► equity_curve INSERT (tek nokta)
│
└── [market_is_open(now)?]  ←── market_calendar cache, MCP yok
      │
      NO ──► erken dön (candidates=0, evidence_written=0)
      │
      YES
      │
      └── for symbol in universe:   # SPY QQQ IWM AAPL MSFT NVDA AMD TSLA META GOOGL
            │
            EvidenceCollector.collect(symbol)
            ├── get_stock_snapshot(symbol)
            │     → spot: bid/ask/last/spread/change_pct
            ├── get_stock_bars(symbol, "1Day", limit=252)
            │     → trend: SMA20, SMA50, RSI14, ATR14, RV20d
            │               high/low 52w, pct_from_52w_high/low
            ├── get_option_chain(symbol, exp_gte, exp_lte, strike_band=±15%)
            │     → options: iv_atm, iv_rank, iv_percentile, put_call_skew
            │                term_structure, median_spread_pct
            │                atm_call/put (symbol, strike, delta, greeks)
            ├── get_option_contracts(symbol, ...)
            │     → open_interest per leg
            ├── get_all_positions()
            │     → position: bu underlying'deki açık pozisyon legs
            └── get_news(symbol)
                  → untrusted_news: headline + summary (truncated)
            │
            pack["earnings_blackout"] = is_earnings_blackout(symbol, today)
            pack["options_trading_level"] = account.options_trading_level
            │
            trend block dict mi?
            ├── HAYIR ──► cache write atla (MISSING pack işe yaramaz)
            └── EVET
                  │
                  ──► evidence UPSERT  (symbol primary key)
                  │
                  ──► _score_from_evidence(pack):
                        RSI extremity  = |RSI14 - 50| / 10
                        Realised vol   = min(rv × 2, 1.5)
                        IV/RV edge     = min((iv/rv − 1) × 3, 3.0)   [iv/rv > 1.05 ise]
                        Earnings blackout → skor = 0.0
                  │
                  ──► candidates INSERT (tüm universe, skor desc sıralı)
```

---

## 5. PositionManagerAgent Akışı (her 60 s)

```
PositionManagerAgent._run()
│
└── get_all_positions()
      │
      ├── OCC sembolünden underlying'e grupla
      │     AAPL240119C00150000 → underlying = "AAPL"
      │
      ├── Her underlying için mark-to-market:
      │     unrealized_pl  = Σ legs[*].unrealized_pl
      │     market_value   = Σ |legs[*].market_value|
      │
      └── positions REPLACE
            ├── Açık her underlying → UPSERT  {legs, unrealized_pl, market_value}
            └── Kapanan her underlying → DELETE
```

P&L her tick'te yazılır. Dashboard ve StrategistAgent pre-filter her zaman taze local değer okur.

---

## 6. StrategistAgent Akışı (her 5 m)

> **Sıfır MCP çağrısı.** Tek outbound I/O: tek LLM isteği.

```
StrategistAgent._run()
│
├── market_is_open(now)          ← market_calendar cache
│   KAPALI ──► erken dön
│
├── kill_switch?                 ← kill_switch cache
│   llm.daily_budget_exhausted?  ← in-memory sayaç
│   ──► erken dön
│
├── top_candidates()             ← candidates cache (max_age = 2 × 60 s)
│   Filtrele:
│   ├── is_earnings_blackout(symbol)    ← in-process (evidence okumadan önce!)
│   ├── symbol in open positions        ← positions cache
│   └── symbol in pending proposals     ← proposals tablosu
│   Kalan yok ──► erken dön
│
├── get_cached_evidence(symbol)  ← evidence cache
│   updated_at > 2 × market_pulse_interval? ──► "stale_evidence", erken dön
│   pack boş? ──► erken dön
│
│   ┌────────────────────────────────────────────────────────┐
│   │           TEK LLM ÇAĞRISI  (Featherless)              │
│   │                                                        │
│   │  llm.complete_json(                                    │
│   │    schema  = RegimeRead,                               │
│   │    system  = "quantitative options strategist",        │
│   │    user    = strategist.md.format(evidence_json=pack), │
│   │    max_tokens   = settings.llm_max_tokens,             │
│   │    temperature  = 0.2,                                 │
│   │  )                                                     │
│   │                                                        │
│   │  RegimeRead:                                           │
│   │    thesis:       str   (neden bu setup?)               │
│   │    invalidation: str   (ne olursa thesis çöker?)       │
│   │    conviction:   float (0.0 – 1.0)                     │
│   │                                                        │
│   │  Başarısız → LlmContractError                          │
│   │  → proposals.status = 'llm_failed'  (supervisor'a yok)│
│   └────────────────────────────────────────────────────────┘
│
└── matrix.decide(pack, regime)  ← pure Python, sıfır LLM
      │
      ├─ 1. Earnings gate
      │     is_earnings_blackout? ──► "hold"
      │
      ├─ 2. Trend sınıflandır  (evidence pack'ten, model yeniden hesaplamaz)
      │     SMA20 > SMA50 AND RSI > 55  →  "up"
      │     SMA20 < SMA50 AND RSI < 45  →  "down"
      │     else                         →  "flat"
      │
      ├─ 3. IV rejimi sınıflandır
      │     IV/RV ≥ 1.40  →  "very_expensive"
      │     IV/RV ≥ 1.10  →  "expensive"
      │     else           →  "cheap"
      │
      ├─ 4. Matris arama
      │     ┌─────────────┬──────────────────┬─────────────────┐
      │     │             │  IV pahalı        │  IV ucuz        │
      │     │             │  (≥ 1.10)         │  (< 1.10)       │
      │     ├─────────────┼──────────────────┼─────────────────┤
      │     │ Yukarı      │ put_credit_spread │ call_debit_sp.  │
      │     │ Yatay       │ iron_condor *     │ long_strangle   │
      │     │ Aşağı       │ call_credit_sp.   │ put_debit_sp.   │
      │     └─────────────┴──────────────────┴─────────────────┘
      │     * IV/RV ≥ 1.40 → iron_butterfly
      │
      ├─ 5. Level degradation
      │     effective = min(account.options_trading_level, config.options_level)
      │     effective < 3, debit spread  →  long_call / long_put
      │     effective < 3, credit/condor →  "hold"
      │
      └─ 6. Conviction floor
            regime.conviction < 0.55  →  "hold"

      "hold"          →  proposals INSERT (status='no_action')
      StrategyIntent  →  proposals INSERT (status='pending')
                          llm_read JSONB + matrix_verdict JSONB
```

---

## 7. ExecutionAgent Akışı (her 30 s)

```
ExecutionAgent._run()
│
├── kill_switch?  ──► erken dön
│
└── pending_proposals(limit=5)   ← proposals tablo
      │
      for each proposal:
      │
      ├── StrategyIntent.model_validate(intent)
      │   parse hatası → status='rejected', risk_events INSERT
      │
      ├── intent.action == "hold"  →  status='held', atla
      │
      ├── get_account_info()                          ← live MCP
      ├── get_stock_snapshot(underlying)              ← live MCP
      │     spot yok → status='rejected'
      ├── get_option_contracts(underlying, dte_range) ← live MCP
      ├── get_option_chain(underlying, dte_range)     ← live MCP
      ├── get_open_position(underlying)               ← live MCP
      │
      ├── strategy_builder.build(intent, contracts, snapshots, …)
      │     ├── target_delta'ya en yakın strike seç (Black-Scholes delta)
      │     ├── standard monthly expiry tercihi
      │     ├── liquidity gate: OI, spread_pct, bid > 0
      │     ├── limit_price hesapla (en kötü fill + spread_nudge_pct)
      │     ├── max_loss hesapla (defined risk zorunlu)
      │     └── OrderPlan  veya  Rejection
      │
      ├── build_portfolio_snapshot(…)
      │     get_clock() + get_all_positions()   ← live MCP
      │     high_water_mark = max(equity_curve)
      │
      ├── RiskEngine.evaluate(plan, portfolio)
      │     ├── kill_switch_engaged?
      │     ├── market_is_open? / minutes_to_close < blackout?
      │     ├── already_submitted?  (idempotency)
      │     ├── premium ≤ max_premium_pct × equity?
      │     ├── total_premium ≤ max_total_premium_pct × equity?
      │     ├── concurrent_positions < max_concurrent?
      │     ├── positions_in_underlying < max_per_underlying?
      │     ├── dte_min ≤ dte ≤ dte_max?
      │     ├── open_interest ≥ min_open_interest?
      │     ├── spread_pct ≤ max_spread_pct?
      │     ├── daily_loss < daily_loss_halt_pct × equity?
      │     └── drawdown < drawdown_halt_pct × high_water_mark?
      │     → RiskVerdict {approved, reasons, adjusted_qty}
      │
      ├── dry_run=true  →  status='dry_run_approved'
      │
      └── place_option_order(**request)              ← live MCP
            duplicate client_order_id  →  reconcile (get_order_by_client_id)
            başarı  →  orders INSERT (status='submitted')
                        proposals UPDATE (status='submitted')
            hata    →  orders INSERT (status='failed')
                        proposals UPDATE (status='failed')

      _reconcile():
        orders WHERE status='submitted'
        → get_order_by_client_id()  ← live MCP
        → orders UPDATE (filled_qty, filled_avg_price, status)
```

---

## 8. ReflectionAgent Akışı (her 60 m)

```
ReflectionAgent._run()
│
├── Pass A — Kapanan trade'ler
│     recent_orders(status='filled')
│     │
│     for order in unflected_orders:
│       reflected_key = f"order:{order.id}"
│       llm.chat_completion(
│         "trading post-mortem analyst",
│         filled_qty, filled_avg_price, legs
│       )
│       → 1-2 cümle ders
│       → save_lesson(
│           symbol=underlying,
│           source='closed_trade',
│           reflected_on='order:{id}'   ← idempotency key
│         )
│
└── Pass B — Hold / reject'lenen öneriler
      recent_proposals(status='no_action' | 'rejected')
      │
      for proposal in unreflected_proposals:
        reflected_key = f"proposal:{proposal.id}"
        llm.chat_completion(
          "hold doğru muydu? miss mi, save mi?",
          underlying, thesis, conviction, rejection_reason
        )
        → 1-2 cümle ders
        → save_lesson(
            source='held_proposal' | 'rejected_proposal',
            reflected_on='proposal:{id}'
          )

Dersler geri döngüsü:
  store.recent_lessons(symbol)
    └──► EvidenceCollector._lessons()
           └──► evidence pack'e eklenir
                  └──► StrategistAgent'ın bir sonraki LLM çağrısı görür
```

---

## 9. LLM Katmanı

```
FeatherlessLlm          llm.py
│
├── chat_completion(messages, tools?, max_tokens, temperature)
│     POST https://api.featherless.ai/v1/chat/completions
│     → LlmResult {content, tool_calls}
│     → LlmError (ulaşılamaz veya okunamaz yanıt)
│
└── complete_json(schema: type[T], system, user, max_tokens, temperature)
      │
      ├── Deneme 1:
      │     _extract_json(response.content)
      │       code-fence → ``` ... ``` bloğu
      │       fallback   → ilk { ... } çifti (brace depth)
      │     T.model_validate(data)
      │
      ├── [ValidationError] Onarım denemesi:
      │     mesajlara hatalı çıktı + hata ekle
      │     yeniden sor
      │     T.model_validate(data)
      │
      └── [hâlâ hatalı] → LlmContractError
            StrategistAgent yakalar:
              proposals.status = 'llm_failed'
              supervisor'a yayılmaz
              Asla free text'e düşmez.

Günlük token bütçesi:
  in-memory sayaç, UTC gece sıfırlanır
  Tükendi → StrategistAgent geçer (PositionManagerAgent hiçbir zaman durdurulamaz)
```

---

## 10. Domain Modelleri

```python
# models.py

class StrategyIntent:        # LLM'nin ürettiği tek şey — kontrat adı yok
    action: "open" | "hold" | "close"
    strategy: "long_call" | "long_put" |
              "call_debit_spread" | "put_debit_spread" |
              "put_credit_spread" | "call_credit_spread" |
              "long_strangle" | "iron_condor" | "iron_butterfly" |
              "debit_call_spread" | "debit_put_spread" |    # legacy
              "covered_call" | "cash_secured_put"
    underlying: str           # bare ticker, isalnum zorunlu
    target_delta: float       # 0 < δ ≤ 1
    spread_width: float | None
    dte_min: int
    dte_max: int
    conviction: float         # 0.0 – 1.0
    thesis: str
    invalidation: str

class RegimeRead:             # LLM'nin tek çıktısı (StrategistAgent)
    thesis: str
    invalidation: str
    conviction: float         # 0.0 – 1.0

class Leg:                    # strategy_builder seçer, asla LLM değil
    symbol: str               # gerçek OCC sembolü
    side: "buy" | "sell"
    ratio: int                # 1–4
    strike: float
    expiry: date
    option_type: "call" | "put"
    delta: float | None
    delta_source: "chain" | "black_scholes" | None

class OrderPlan:              # tam fiyatlı, risk hesaplanmış plan
    proposal_id: int
    legs: list[Leg]           # 1–4 leg
    qty: int
    limit_price: float        # pozitif = debit, negatif = credit
    max_loss: float           # finite, pozitif zorunlu
    client_order_id: str      # "om-{proposal_id}"

class Rejection:              # builder veya risk engine reddi
    proposal_id: int
    reason: str
    detail: dict
```

---

## 11. Güvenlik Katmanları

| Katman | Nerede | Ne durdurur |
|---|---|---|
| `dry_run=True` | MCP transport | Tüm write tool'ları subprocess sınırında bloklar; call site'larda değil |
| `FORBIDDEN_TOOLS` | `AlpacaMcp.call()` | `cancel_all_orders`, `close_all_positions`, `exercise_options_position`, `do_not_exercise_options_position` — her modda kalıcı yasak |
| `WRITE_TOOLS` dry-run check | `AlpacaMcp.call()` | `dry_run=True` iken tüm write tool'lar rejected |
| Paper assertion | `AlpacaMcp.connect()` | `ALPACA_PAPER_TRADE` doğrulanmazsa bağlantı kurmaz |
| `kill_switch` | Her agent, her tick | DB flag + env var + `POST /admin/kill`; yeni order'ları anında durdurur |
| Earnings gate | `matrix.decide()` | Matris aramasından önce; blacked-out sembol LLM'e bile ulaşmaz |
| `RiskEngine` | `ExecutionAgent` | Premium cap, pozisyon cap, DTE, spread, earnings, daily loss, drawdown |
| `LlmContractError` | `StrategistAgent` | 2 başarısız deneme → `llm_failed`, supervisor'a yayılmaz, trade yok |
| `client_order_id = "om-{id}"` | `ExecutionAgent` | Broker seviyesinde idempotency; bir proposal iki order açamaz |
| Defined-risk only | `strategy_builder` + `RiskEngine` | Naked short leg iki bağımsız katmanda reject |
| `options_trading_level` check | `matrix.decide()` | Account level yetersizse structure downgrade veya hold |

---

## 12. Veritabanı Şeması

```sql
-- Append-only telemetry
agent_runs        (agent, started_at, duration_ms, ok, error, detail JSONB)
equity_curve      (ts, equity, cash, buying_power, positions_count)
candidates        (ts, symbol, reason, score, payload JSONB)
iv_history        (ts, symbol, iv_atm, dte, spot, put_call_skew, term_structure, …)
risk_events       (ts, proposal_id→, rule, detail JSONB)
llm_calls         (ts, agent, model, prompt_tokens, completion_tokens, latency_ms, ok, error)
lessons           (ts, symbol, lesson, source, reflected_on UNIQUE)

-- Karar izi
proposals         (ts, underlying, status, intent JSONB, evidence JSONB,
                   llm_read JSONB, matrix_verdict JSONB,
                   arguments JSONB, verdict JSONB, plan JSONB, error)
orders            (proposal_id→, client_order_id UNIQUE, submitted_at, status,
                   request JSONB, response JSONB, filled_qty, filled_avg_price, error)

-- Current-state cache (tek writer, yerinde üzerine yazılır)
evidence          (symbol PK, payload JSONB, updated_at)
positions         (symbol PK, payload JSONB, updated_at)
account           (id=1 singleton, equity, cash, buying_power, options_trading_level, updated_at)
market_calendar   (date PK, open TIMESTAMPTZ, close TIMESTAMPTZ, session_type)
kill_switch       (id=1 singleton, engaged, reason, updated_at)
```

---

## 13. Modül Haritası

```
src/options_m/
│
├── agents/
│   ├── __init__.py          Agent protocol · run_agent · build_agents (supervisor)
│   ├── market_pulse.py      MarketPulseAgent
│   ├── position_manager.py  PositionManagerAgent
│   ├── execution.py         ExecutionAgent
│   ├── strategist.py        StrategistAgent
│   └── reflection.py        ReflectionAgent
│
├── evidence/
│   ├── evidence.py          EvidenceCollector — sembol başına pack oluşturur
│   └── occ.py               OCC opsiyonu sembol parser'ı
│
├── prompts/
│   ├── loader.py            Path-escape korumalı template yükleyici
│   └── strategist.md        StrategistAgent LLM prompt şablonu
│
├── matrix.py                Deterministik Strateji Matrisi + earnings gate
├── llm.py                   FeatherlessLlm — chat_completion + complete_json
├── models.py                StrategyIntent · RegimeRead · OrderPlan · Leg · Rejection
├── strategy_builder.py      StrategyIntent → gerçek kontrat seçimi → OrderPlan
├── risk.py                  RiskEngine — sıfır LLM, sıfır MCP
├── store.py                 Postgres repository + in-memory fallback
├── mcp_client.py            AlpacaMcp — tek broker arayüzü
├── earnings.py              Sabit earnings takvimi + is_earnings_blackout()
├── indicators.py            SMA · RSI · ATR · RV · window_extremes
├── volatility.py            iv_rank · iv_percentile · implied_vol (BSM)
├── config.py                Env-driven Settings (pydantic-settings)
├── schema.sql               Idempotent DDL — başlangıçta uygulanır
├── migrate.py               Schema runner
├── api.py                   FastAPI dashboard + /api/* endpoint'leri
├── cli.py                   options-m status | propose | trade | flatten
├── chat.py                  Dashboard için read-only LLM Q&A
└── __main__.py              Process entry point
```

---

## 14. Önemli Konfigürasyonlar

```bash
# Evren
UNIVERSE=SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL

# Güvenlik
DRY_RUN=true              # false yapmadan gerçek paper order gitmez
KILL_SWITCH=false

# Kadanslar (saniye)
MARKET_PULSE_INTERVAL_SECONDS=60
POSITION_MANAGER_INTERVAL_SECONDS=60
EXECUTION_AGENT_INTERVAL_SECONDS=30
STRATEGIST_INTERVAL_SECONDS=300
REFLECTION_INTERVAL_SECONDS=3600

# LLM
FEATHERLESS_API_KEY=...
FEATHERLESS_CHAT_MODEL=...       # dashboard chat
FEATHERLESS_MODEL_DEEP=...       # StrategistAgent (asla kod içinde sabit değil)
LLM_TIMEOUT_SECONDS=30
LLM_MAX_TOKENS=1024
LLM_DAILY_TOKEN_BUDGET=100000

# Strateji matrisi
CONVICTION_FLOOR=0.55            # altında her zaman hold
OPTIONS_LEVEL=3                  # tavan; effective = min(account_level, bu)
SHORT_DELTA_DEFAULT=0.25
SPREAD_WIDTH_DEFAULT=5.0
DTE_TARGET_MIN=21
DTE_TARGET_MAX=38

# Risk limitleri
MAX_PREMIUM_PCT_PER_TRADE=0.02
MAX_TOTAL_PREMIUM_PCT=0.15
MAX_CONCURRENT_POSITIONS=5
MAX_POSITIONS_PER_UNDERLYING=1
RISK_DTE_MIN=7
RISK_DTE_MAX=45
MIN_OPEN_INTEREST=100
MAX_SPREAD_PCT=0.10
DAILY_LOSS_HALT_PCT=0.03
DRAWDOWN_HALT_PCT=0.08
MINUTES_BEFORE_CLOSE_BLACKOUT=15

# Alpaca
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER_TRADE=true          # false ise başlangıçta reject
ALPACA_TOOLSETS=account,trading,assets,options-data,stock-data
```
