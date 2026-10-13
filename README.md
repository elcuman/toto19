# Şampiyonlar Tahmin Ligi — Flask (Lokal Sürüm)

> Maç başlamadan konuş. Maç bitince hesabını ver.

Bu, projenin lokalde hızlıca ayağa kaldırıp test edebileceğin Flask + SQLite
sürümüdür.

Gerçek para / bahis / ödeme YOKTUR. Grup sosyal kuralları (örn. "haftanın
sonuncusu kahve ısmarlar") sadece metin olarak saklanır.

## Kurulum ve Çalıştırma

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Tarayıcıda http://127.0.0.1:5000 adresine git. İlk çalıştırmada
`tahmin_ligi.db` (SQLite) otomatik oluşturulur — ek kurulum gerekmez.

## Kullanım Akışı

1. `/kayit` — kullanıcı adı + şifre ile kayıt ol
2. `/gruplar` — yeni grup kur (opsiyonel sosyal kural ekleyebilirsin) ya da
   arkadaşının davet koduyla katıl
3. `/admin/maclar` — test için maç ekle (gerçek projede otomatik fikstür
   senkronizasyonuyla değiştirilecek)
4. Grup sayfasında maça tahmin gir — maç başlama saati geçtiyse tahmin
   giriş alanları kilitlenir (sunucu tarafında da kontrol edilir)
5. `/admin/maclar`'dan maç sonucunu gir → puanlar otomatik hesaplanır
6. Grup sayfasındaki "Sezonluk Sıralama" güncellenir

## Puanlama

- Kesin skor doğru: 5 puan
- Sadece maç sonucu (1/X/2) doğru: 2 puan
- Yanlış: 0 puan

Mantık `app.py` içindeki `mac_puanlarini_hesapla()` fonksiyonunda.

## Bu sürümün sınırları (bilerek basit tutuldu)

- Auth: kullanıcı adı/şifre (magic-link/e-posta doğrulama yok)
- Maç ekleme: manuel admin ekranı (gerçek fikstür API entegrasyonu yok)
- Veritabanı: SQLite (tek dosya — production'da Postgres'e geçilmeli)

## Production'a Geçiş

- Hosting: Render.com veya Railway — `gunicorn app:app`
- Veritabanı: SQLite yerine Postgres
- Auth: e-posta doğrulama veya magic link eklenmeli
- Fikstür verisi: Champions League için football-data.org, Süper Lig için
  API-Football (api-sports.io) — ikisi de ücretsiz plan (API-Football günde
  100 istek limitli). football-data.org'un ücretsiz planı Süper Lig'i (TR1)
  desteklemediği için bu iki sağlayıcılı yapı kullanılıyor.
- `SECRET_KEY`'i ortam değişkeninden oku, kodda sabit bırakma

Hazır olduğunda production adımlarında da yardımcı olabilirim.
