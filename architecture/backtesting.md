# Backtesting Guide

## Yaklaşım: Pipeline Replay

options-m'nin backtest yöntemi **pipeline replay**'dir — stratejiyi sıfırdan
yeniden yazmak yerine, üretim kodunun kendisini geçmiş veriye karşı çalıştırır.

```
run.py
  │
  ├── AsOfMcp (backtests/asof.py)
  │     AlpacaMcp'nin read interface'ini implement eder
  │     MCP çağrıları yerine Alpaca tarihsel API'sinden önbelleğe alınmış
  │     veri döndürür — EvidenceCollector ve strategy_builder bunu bilmez
  │
  ├── frozen_clock (backtests/clock.py)
  │     strategy_builder, risk, evidence ve execution içindeki date.today() /
  │     datetime.now() çağrılarını replay tarihine sabitler
  │
  └── Çalıştırılan üretim kodu (hiçbiri değiştirilmez):
        EvidenceCollector.collect()
        matrix.decide()
        fetch_chain_window()
        strategy_builder.build()
        RiskEngine.evaluate()
```

**Neden bu önemli:** Pipelini yeniden yazan bir backtest, yeniden yazmayı
ölçer. Burada ölçülen şey gerçek üretim kararıdır.

---

## Hızlı Başlangıç

```bash
# 1. Alpaca credentials'larını ayarla
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...

# 2. Var olan replay çalıştır
python backtests/runs/2026-08-30_universe_agent-replay_1Day/run.py
python backtests/runs/2026-08-30_universe_agent-replay_1Day/analyse.py

# 3. Sonuçlar JSON olarak yazılır; analyse.py bunları okur ve tablo basar
```

Ham veriler ve SHA-256 parmak izleri `raw/` ve `data_fingerprint.json`
dosyalarına yazılır. Fetch adımından sonra ağ çağrısı olmaz.

---

## Yeni Bir Replay Çalıştırmak

### Yapı

```
backtests/runs/<YYYY-MM-DD>_<açıklama>/
├── run.py          # veri çekme + pipeline + sonuçları kaydet
├── analyse.py      # kaydedilmiş sonuçları oku + rapor yazdır
└── notes.md        # varsayımlar, sapmalar, yorumlar
```

### run.py'de minimum yapı

```python
from datetime import date
from backtests.asof import AsOfMcp
from backtests.clock import frozen_clock
from options_m.evidence.evidence import EvidenceCollector
from options_m.matrix import decide
from options_m.agents.execution import fetch_chain_window
from options_m import strategy_builder
from options_m.risk import RiskEngine
from options_m.config import Settings
from options_m.models import RegimeRead

REPLAY_DATES = [date(2026, 8, 25), date(2026, 8, 26), ...]
SPREAD_PCT = 0.02  # modellenen bid/ask yarı-yayılım
SETTINGS = Settings()

for replay_date in REPLAY_DATES:
    mcp = AsOfMcp(replay_date, spread_pct=SPREAD_PCT)
    await mcp.fetch(SETTINGS.universe_symbols)   # Alpaca'dan veri çek + cache'le

    with frozen_clock(replay_date):
        evidence = EvidenceCollector(SETTINGS, mcp, store)
        for symbol in SETTINGS.universe_symbols:
            pack = await evidence.collect(symbol, ...)
            regime = RegimeRead(thesis="...", invalidation="...", conviction=0.70)  # LLM stub
            decision = decide(pack, regime, settings=SETTINGS, as_of=replay_date)
            if hasattr(decision, 'strategy'):
                # strategy_builder + RiskEngine + kayıt...
```

### Kritik kurallar

- `AsOfMcp` her replay gününden önce `fetch()` ile ısıtılmalıdır — bu Alpaca'yı çağırır
- `frozen_clock` context manager, strateji ve risk kodu çalışırken aktif olmalıdır
- LLM çağrısı ya sabitlenmeli (üretim-dışı conviction ile) ya da gerçekten çalıştırılmalıdır.
  Sabitlemek kararlılık sağlar ve üretim sonuçlarının üst sınırını verir
- `AsOfMcp` yalnızca read metodlarını implement eder: `place_option_order` yok

---

## Veri Kısıtları ve Etkileri

Bu kısıtlar veri kaynağının doğasından gelir — bunları düzeltmek mümkün değil.

### 1. Tarihsel opsiyon kotasyonu yok — spread modellenmek zorunda

Alpaca tarihsel opsiyon **bar** ve **trade** verisi sağlar ama geçmiş
**quote** verisi yoktur. `AsOfMcp` şunu yapar:

```
mid  = o günkü opsiyon bar kapanışı
bid  = mid × (1 − spread_pct)
ask  = mid × (1 + spread_pct)
```

Her ikisi de gerçek tick grid'e yuvarlanır (3.00$'ın altında $0.01, üstünde $0.05).

**Pratik etki:** `MAX_SPREAD_PCT` kapısı bir varsayımı filtreler. Bu nedenle
çalıştırmalar `spread_pct`'yi sabit bir değer kullanmak yerine **süpürür**:

| `spread_pct` | Yapılan işlem | P&L |
|---|---|---|
| 0.5% | 5 | +$2,057 |
| 2% (başlık) | 5 | +$1,993 |
| 5% | 5 | +$1,755 |
| 10% | 5 | +$404 |

Tablo 0.5%–5% arası kararlıysa sonuç güvenilirdir.

### 2. IV ve delta modellenir, alınmaz

Tarihsel snapshotlar IV veya greeks taşımaz — üretim feed'inin de taşımadığı
gibi. Her ikisi de `None` olarak gelir ve projenin kendi Black-Scholes çözücüsü
devreye girer. Bu, üretim kodu yolunun aynısıdır, backtest'e özel bir kısayol değil.

### 3. Açık faiz nokta-in-zamanlı değil (hafif ileriye bakış)

`open_interest` Alpaca'nın contracts endpoint'inden gelir ve tek bir
`open_interest_date` damgası taşır (birkaç gün öncesinden). Bugün için `OI` filtresi
birkaç günlük ileriye bakış içerir. Açık faiz yavaş hareket ettiği için
distorsiyon küçüktür ama gerçek ve kaldırılabilir değildir.

### 4. İşlem yapılmayan kontrat o günün chain'inde yok

15.684 kontratın 11.888'i pencerede bar taşır. Günde işlem yapılmaması, quote
kaynağı olmadığı anlamına gelir; bu hem ham bir likidite filtresi hem de seçilebilir
kümeyi likit strike'lara doğru önyargılar.

### 5. Günlük granülarite

Çalışma günde bir kez değerlendirme yapar. Gün içi girişler, gün içi kapı
değişiklikleri ve aynı gün çıkışlar günlük barlarla temsil edilemez.

---

## LLM Backtesting Seçenekleri

### Seçenek A — Sabitle (önerilen)

```python
regime = RegimeRead(thesis="stubbed", invalidation="stubbed", conviction=0.70)
```

- **Avantaj:** Tamamen tekrarlanabilir; ağ çağrısı yok; üst sınır ölçülür
- **Dezavantaj:** Conviction veto göz ardı edilir; 0.70 seçimi sonuçları etkiler
- **Ne zaman kullanılır:** Matris mantığını, strateji inşasını veya risk
  limitlerini test ederken

### Seçenek B — Gerçek LLM

`RegimeRead` döndürmek için asıl `StrategistAgent._run()` çağrısını kullanın
(LLM etkin, bir API anahtarıyla).

- **Avantaj:** Conviction dağılımı gerçekçi; sistem daha bütünsel test edilir
- **Dezavantaj:** Token maaliyeti; farklı çalıştırmalarda farklı sonuçlar;
  LLM'nin "bugün" varsayımıyla halüsinasyon riski var
- **Ne zaman kullanılır:** Tam uçtan uca doğrulamada; çıktıları loglayarak

---

## En Etkili Backtest Nasıl Yapılır

### Güvenilir bulgular için minimum çubuk

| Gereksinim | Neden |
|---|---|
| **≥ 50 karar** | İstatistiksel anlam için — birkaç işlem "win rate" değil ögrenme |
| **Birden fazla piyasa rejimi** | Yalnızca trend veya yalnızca yatay; her ikisi de gerekli |
| **`spread_pct` süpürün** | Spread varsayımının sonuçları değiştirip değiştirmediğini anlayın |
| **Sabit LLM conviction** | Belirsizlik kaynaklarını izole edin; önce determinizmi test edin |

### Parametre hassasiyeti

Konfigürasyon değerlerini değiştirirken her değişken için ayrı çalıştırmalar
yapın. Aşağıdakiler en etkili kaldıraçlardır:

```python
# Matris sinyal kalitesi
CONVICTION_FLOOR          # düşürmek → daha fazla işlem, daha gürültülü sinyal
# Yeterince test edilmemiş: IV nötr band henüz yok (bkz. Kısıtlamalar)

# Risk limitleri
MAX_CONCURRENT_POSITIONS  # 5'ten büyük = kötü deselerden kaçınılmaz
MAX_PREMIUM_PCT_PER_TRADE # boyutlandırma etkisi
DAILY_LOSS_HALT_PCT       # ne kadar agresif durduruluyor

# Yapı parametreleri
DTE_TARGET_MIN/MAX        # 21-38 şu an; daha kısa/uzun ne değiştirir
SHORT_DELTA_DEFAULT       # 0.25 şu an; 0.20 vs 0.30
```

### Walk-forward testi

Tek bir tarih aralığı için optimize etmek overfit eder. Bunun yerine:

```
Eğitim penceresi: [tarih_A, tarih_B]  ← parametre seçimi
Test penceresi:   [tarih_B, tarih_C]  ← eğitimde görmeden değerlendirme
```

Mevcut harness bunu doğal olarak destekler — farklı tarih listeleriyle iki
ayrı çalıştırma yapın.

### Çıkış mantığı gerçek bir kontratla çerçevelemenin önünde

Şu anda çalıştırmalar pozisyonları kapanışta işaret eder, çünkü
`PositionManagerAgent` henüz sadece kar hedefi/stop-loss tetikçileri üzerinden
çıkışları yönetiyor. Her çalıştırma **giriş kararlarını** test eder; **sistem
çıkış kararlarını** değil.

Bu, karların hafife alındığı anlamına gelir (kapanış yarı-yayılımı
düşürülmemiştir) ve taşıma süresinin gerçekçi olmadığı anlamına gelir.

---

## Sonuçları Yorumlama

### Her zaman raporlanması gerekenler

| Metrik | Neden |
|---|---|
| Karar sayısı | Gözlem başına istatistik güvenilirliğini sınırlandırır |
| `spread_pct` süpürme tablosu | Spread varsayımı etkisini gösterir |
| Yapı ailesi döküm | Hangi strateji türleri seçildi |
| Reddedilen → neden | Hangi kapılar ateşlendi |
| Ileriye bakış uyarısı | OI ve spread modeli her ikisi de |

### Güvenilir olmayan şeyler

- Kesin P&L rakamları (spread modeli ± komisyonlar)
- Sharpe oranı (günlük granülaritede birkaç gözlemden)
- "Bu parametre daha iyidir" sonuçları tek bir haftalık pencere üzerinden

### Güvenilir olan şeyler

- Hangi kapılar ateşlendi ve ne sıklıkla
- Belirli konfigürasyonlarla kaç işlem yapılır
- Hangi reddetme nedenleri baskın (kötü sinyal kalitesini tanımlar)
- `spread_pct` 0.5%–5% arasında istikrarlıysa, spread modeli dominant değildir

---

## Mevcut Açık Backtest Soruları

Bu konular doğrudan harness üzerinde yanıtlanabilir:

1. **IV nötr band etkisi** — IV/RV'nin 0.85–1.10 arasındaki kararlar için bir
   "hold" bölgesi eklemek `long_strangle` seçimini ne kadar azaltır?

2. **Kanat yakalama toleransı** — Yerel yerine global minimum gap düzeltilirse
   kaç ek yapı inşa edilebilir? (AMD bunu haftalık olarak etkiliyordu)

3. **Conviction eşiği kalibrasyonu** — 0.55 uygun mu yoksa daha yüksek mi
   olmalı? Gerçek LLM conviction dağılımını sabit stub değil, bir aralıkta
   süpürerek ölçün

4. **Çıkış simülasyonu** — Basit kar hedefi/stop (örn. %50 kazanınca çık,
   -%100'de çık) kodu yokken bile harness'ta simüle edilebilir ve giriş
   kararları üzerindeki etkiyi gösterir

---

## Önemli Açıklama

> Bu backtest hipotetik bir tarihsel simülasyondur ve gerçek işlem
> performansını temsil etmez. Geçmişe yönelik sonuçlar gelecekteki sonuçları
> garanti etmez. Sonuçlar; piyasa verisi kalitesi, veri akışı seçimi,
> kurumsal eylem işleme, ücretler, kayma, likidite, vergi, yürütme
> varsayımları ve uygulama ayrıntılarına bağlıdır. Bu materyal yalnızca
> araştırma ve eğitim amaçlıdır.
