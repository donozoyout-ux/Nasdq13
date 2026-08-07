# 🤖 NASDAQ Sinyal Botu

Real-time NASDAQ sinyal botu. Teknik analiz + haber sentiment analizi ile ani piyasa hareketlerini tespit eder ve Telegram'a anlık AL/SAT sinyali gönderir.

## ✨ Özellikler

- 📊 **Çoklu zaman dilimi analizi** (1m, 5m, 15m, 1h)
- 🚀 **Otomatik breakout detection** - Donchian Channel + Bollinger Band kırılımları
- 📰 **Haber sentiment analizi** - Trump, Fed, makro veriler ve şirket haberleri
- ⚡ **Birleşik sinyal skoru** - Teknik + haber = 0-100 puan
- 🎯 **Risk yönetimi** - ATR tabanlı Stop-Loss / Take-Profit hesaplama
- 🔔 **Anlık Telegram bildirimleri**
- ☁️ **7/24 Render'da çalışır**

## 🚀 Kurulum

### 1. Gerekli API Key'ler

| Servis | Nereden | Ücretsiz Limit |
|--------|---------|----------------|
| Telegram Bot Token | [@BotFather](https://t.me/BotFather) | Sınırsız |
| Telegram Chat ID | [@userinfobot](https://t.me/userinfobot) | Sınırsız |
| NewsAPI | [newsapi.org](https://newsapi.org) | 100 req/gün |
| Alpha Vantage | [alphavantage.co](https://www.alphavantage.co) | 25 req/gün |
| Finnhub | [finnhub.io](https://finnhub.io) | 60 req/dk (opsiyonel) |

### 2. Ortam Değişkenleri

```bash
cp .env.example .env
# .env dosyasını düzenleyip API key'lerinizi girin
```

### 3. Çalıştırma (Yerel)

```bash
pip install -r requirements.txt

# Seçenek 1: Web arayüzü + bot (dashboard: http://localhost:8000)
uvicorn src.webapp:app --host 0.0.0.0 --port 8000

# Seçenek 2: Sadece bot (CLI worker)
python -m src.main
```

## ☁️ Render'a Deploy Etme (Tek Tık)

Repo [render.yaml](render.yaml) blueprint'i ile hazır. Render **tek servis** üzerinde hem botu hem web arayüzünü çalıştırır (free tier 750 sa/ay = 1 servis yeter).

1. [render.com](https://render.com) hesabınıza girin → **New → Blueprint**
2. GitHub repo'nuzu bağlayın (`donozoyout-ux/Nasdq13`)
3. Render `render.yaml`'ı otomatik algılar
4. Şu env değişkenlerini **manually** girin:

| Değişken | Değer |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | @BotFather'dan aldığınız token |
| `TELEGRAM_CHAT_ID` | Chat ID'niz |
| `NEWSAPI_KEY` | NewsAPI key (opsiyonel) |
| `ALPHA_VANTAGE_KEY` | Alpha Vantage key (opsiyonel) |
| `FINNHUB_KEY` | Finnhub key (opsiyonel) |

5. **Apply** → Render otomatik deploy eder

Deploy sonrası:
- 🌐 **Dashboard**: `https://nasdaq-signal-bot.onrender.com`
- 🔍 **Sağlık kontrolü**: `https://nasdaq-signal-bot.onrender.com/health`
- 🤖 Telegram sinyalleri otomatik gelir

### Free Tier Uyku Sorununu Çözme

Render free tier 15 dk inaktiflik sonrası uykuya girer. Botun 7/24 çalışması için:
- [UptimeRobot](https://uptimerobot.com) veya [cron-job.org](https://cron-job.org) ücretsiz servisi
- Her 10 dk bir `GET /health` isteği gönderin
- UptimeRobot monitör URL'si: `https://nasdaq-signal-bot.onrender.com/health`

## ⚙️ Yapılandırma

Tüm ayarlar `config/settings.yaml` dosyasında. Önemli ayarlar:

- **Symbols**: Takip edilecek semboller (varsayılan: NQ=F, ES=F, YM=F, RTY=F)
- **Timeframes**: Analiz zaman dilimleri
- **Thresholds**: Sinyal eşikleri (STRONG_BUY=75, BUY=60, vs.)
- **Risk**: ATR bazlı SL/TP çarpanları
- **News**: Haber sentiment yapılandırması

## 📁 Proje Yapısı

```
├── config/settings.yaml    # Tüm ayarlar
├── src/
│   ├── main.py            # CLI worker entry point
│   ├── webapp.py          # Web dashboard + bot (Render)
│   ├── bot.py             # Bot orkestratörü
│   ├── data/              # Veri çekiciler (fiyat, haber)
│   ├── analysis/          # Teknik analiz + sinyal motoru
│   ├── notifier/          # Telegram bildirimleri
│   ├── state/             # State yönetimi (JSON)
│   └── utils/             # Yardımcı fonksiyonlar
├── web/templates/         # Dashboard HTML
├── render.yaml            # Render deploy config
└── requirements.txt
```

## ⚠️ Uyarı

Bu bot yatırım tavsiyesi değildir. Sinyaller otomatik olarak üretilir ve yanlış olabilir. Gerçek para ile işlem yapmadan önce mutlaka paper trading ile test edin.
