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
python -m src.main
```

## ☁️ Render'a Deploy Etme

1. [render.com](https://render.com)'da GitHub reposunu bağlayın
2. **New → Web Service** veya **Blueprint** oluşturun
3. `render.yaml` blueprint dosyasını kullanın (otomatik yapılandırma)
4. Environment değişkenlerini ekleyin:

| Değişken | Değer |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | @BotFather'dan aldığınız token |
| `TELEGRAM_CHAT_ID` | Chat ID'niz |
| `NEWSAPI_KEY` | NewsAPI key (opsiyonel) |
| `ALPHA_VANTAGE_KEY` | Alpha Vantage key (opsiyonel) |
| `FINNHUB_KEY` | Finnhub key (opsiyonel) |

### Free Tier Uyku Sorununu Çözme

Render free tier 15 dk inaktiflik sonrası uykuya girer. Çözüm:
- [UptimeRobot](https://uptimerobot.com) veya [cron-job.org](https://cron-job.org) ücretsiz servisi
- Her 10 dk bir `GET /health` isteği gönderin
- Alternatif: render.yaml içindeki `nasdaq-signal-bot-keepalive` web servisini kullanın

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
│   ├── main.py            # Entry point
│   ├── data/              # Veri çekiciler (fiyat, haber)
│   ├── analysis/          # Teknik analiz + sinyal motoru
│   ├── notifier/          # Telegram bildirimleri
│   ├── state/             # State yönetimi (JSON)
│   └── utils/             # Yardımcı fonksiyonlar
├── render.yaml            # Render deploy config
└── requirements.txt
```

## ⚠️ Uyarı

Bu bot yatırım tavsiyesi değildir. Sinyaller otomatik olarak üretilir ve yanlış olabilir. Gerçek para ile işlem yapmadan önce mutlaka paper trading ile test edin.
