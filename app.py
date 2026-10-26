import io
import calendar
import hashlib
import threading
import os
from datetime import datetime, timezone, timedelta, date
from functools import wraps
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    abort,
    Response,
    session,
)

from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    current_user,
    login_required,
)

from werkzeug.security import generate_password_hash, check_password_hash


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET_KEY_2026"
)

# Render için veritabanı URI ayarı.
# ÖNEMLİ: Render'da disk kalıcı DEĞİLDİR (her deploy/restart'ta sıfırlanır),
# bu yüzden SQLite fallback SADECE lokal geliştirme içindir.
# Render'da mutlaka DATABASE_URL env var'ı bir Render PostgreSQL veritabanına işaret etmelidir.
database_url = os.getenv("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "tahmin_ligi.db"))
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,  # boşta kalan bağlantı kapanmışsa hatayı önler
    "pool_recycle": 280,
    # Supabase ücretsiz planda eşzamanlı bağlantı sınırlı: 2 worker x (4+2) = en fazla 12 bağlantı
    "pool_size": 4,
    "max_overflow": 2,
}

db = SQLAlchemy(app)

# ============================================================
# TÜRKİYE SAATİ
# ============================================================
# Veritabanında tüm maç saatleri (Mac.baslangic) UTC olarak (saat dilimsiz/naive)
# saklanıyor. Türkiye UTC+3 kullandığı için, kullanıcıya HER ZAMAN bu offset
# eklenerek gösterilmeli — aksi halde maçlar olduğundan 3 saat erken görünür.
# Bu filtre şablonlarda `mac.baslangic | tr_saat` şeklinde kullanılır.
TURKIYE_UTC_OFFSET = timedelta(hours=3)


def to_turkiye_saati(dt):
    """Naive UTC bir datetime'ı Türkiye yerel saatine (UTC+3) çevirir."""
    if dt is None:
        return None
    return dt + TURKIYE_UTC_OFFSET


@app.template_filter("tr_saat")
def tr_saat_filter(dt, fmt="%d.%m.%Y %H:%M"):
    """UTC datetime'ı Türkiye saatine çevirip istenen formatta string döner."""
    donusturulmus = to_turkiye_saati(dt)
    if donusturulmus is None:
        return ""
    return donusturulmus.strftime(fmt)

login_manager = LoginManager(app)
login_manager.login_view = "giris"
login_manager.login_message = "Bu sayfayı görmek için giriş yapmalısınız."
login_manager.login_message_category = "warning"


# ============================================================
# FOOTBALL DATA API
# ============================================================

FOOTBALL_DATA_TOKEN = os.getenv(
    "FOOTBALL_DATA_TOKEN",
    ""
).strip()

FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"

CHAMPIONS_LEAGUE_CODE = "CL"
CHAMPIONS_LEAGUE_ID = 2001

# Süper Lig (Galatasaray, Fenerbahçe, Beşiktaş, Trabzonspor'un lig maçları).
# NOT: football-data.org'un ÜCRETSİZ (Free) planı Süper Lig'i (TR1) desteklemez
# (API 403 döner). Bu yüzden Süper Lig, football-data.org yerine ayrı bir
# sağlayıcı olan API-Football (api-sports.io) üzerinden çekiliyor — bu servisin
# ücretsiz planı Süper Lig dahil çoğu ligi destekler (günlük 100 istek limiti).
# SUPER_LIG_ID: uygulama içinde kullanılan SABİT dahili kimlik (Mac.lig_id).
# Sağlayıcı değişse bile bu id aynı kalır ki geçmiş veriler/filtreler bozulmasın.
SUPER_LIG_CODE = "TR1"
SUPER_LIG_ID = 2036

# UEFA Uluslar Ligi — SADECE Türkiye Milli Takımı'nın maçları.
# Veri ESPN'in key gerektirmeyen uç noktasından (slug: uefa.nations) çekilir;
# SportAPI7 KULLANILMAZ. NATIONS_LEAGUE_ID de Mac.lig_id için dahili sabit kimliktir.
NATIONS_LEAGUE_CODE = "UNL"
NATIONS_LEAGUE_ID = 2037

# Avrupa kupaları (Avrupa Ligi + Konferans Ligi) — SADECE Beşiktaş ve
# Trabzonspor'un maçları. ESPN'den (key gerektirmez) çekilir. İki kupa tek
# sekmede ("Avrupa") toplanır; Mac.lig alanında maçın asıl kupası yazar.
# (Şampiyonlar Ligi zaten football-data'dan tümüyle geliyor.)
AVRUPA_CODE = "AVR"
AVRUPA_ID = 2038

# Takip edilen tüm ligler burada tanımlı. "provider" hangi API/senkronizasyon
# fonksiyonunun kullanılacağını belirler. Yeni bir lig eklemek için buraya bir
# satır eklemek yeterli; sync_all_leagues() otomatik olarak hepsini çeker.
LEAGUES = {
    CHAMPIONS_LEAGUE_CODE: {"id": CHAMPIONS_LEAGUE_ID, "ad": "UEFA Champions League", "provider": "football-data"},
    SUPER_LIG_CODE: {"id": SUPER_LIG_ID, "ad": "Süper Lig", "provider": "espn"},  # ücretsiz, kota yok
    NATIONS_LEAGUE_CODE: {"id": NATIONS_LEAGUE_ID, "ad": "UEFA Uluslar Ligi", "provider": "espn-nations"},
    AVRUPA_CODE: {"id": AVRUPA_ID, "ad": "Avrupa Kupaları (BJK & TS)", "provider": "espn-avrupa"},
}

TAKIP_EDILEN_LIG_ID_LISTESI = [lig["id"] for lig in LEAGUES.values()]

# Grup sayfasındaki lig sekmelerinde görünen kısa adlar.
LIG_KISA_ADLARI = {
    CHAMPIONS_LEAGUE_ID: "Şampiyonlar Ligi",
    SUPER_LIG_ID: "Süper Lig",
    NATIONS_LEAGUE_ID: "Uluslar Ligi",
    AVRUPA_ID: "Avp/Konf",
}

# Grup sayfasında "bu hafta" sayılan süre: bir ligin sıradaki maçı bu kadar
# gün içinde değilse o lig için maç listelenmez.
HAFTA_PENCERESI_GUN = 7
# Bir gruba elle eklenen özel maçlar (ör. Real Madrid–Barcelona). Takip edilen
# liglerde olmadığı için genel sayfalara, Robot Ziya'ya ve diğer gruplara karışmaz.
OZEL_LIG_ID = 990001
# Haftalık puan tablosu Türkiye saatiyle bu gün ve saatte sıfırlanır (Cuma 12:00).
HAFTA_BASLANGIC_GUNU = 4      # Pazartesi=0 ... Cuma=4
HAFTA_BASLANGIC_SAATI = 12
# Başlamış ama sonucu gelmemiş bir maç, haftayı en fazla bu kadar saat açık tutar
# (API hiç güncellemezse grup sayfası eski haftada takılı kalmasın diye).
AKTIF_HAFTA_BEKLEME_SAAT = 12
# Haftanın son maçı bittikten sonra o hafta bu kadar saat daha gösterilir.
BITEN_HAFTA_GOSTERIM_SAAT = 24

# Sohbet odası ayarları
MESAJ_MAX_UZUNLUK = 500
SOHBET_SAYFA_BOYU = 50       # sayfa açılınca gösterilen son mesaj sayısı
SOHBET_MIN_ARALIK_SN = 1     # aynı kullanıcının iki mesajı arasındaki en kısa süre

# Şampiyonlar Ligi grup/lig aşaması Kasım 2026'ya kadar oynanmayacak
# (takvimde sadece birkaç maç var). Bu yüzden o tarihe kadar Şampiyonlar
# Ligi maçlarını kullanıcıya GÖSTERMİYORUZ — senkronizasyon arka planda
# yine de çalışabilir, sadece dashboard/grup/geçmiş ekranlarında filtreleniyor.
# Tarih gelince bu satırı silmek ya da tarihi ileri almak yeterli.
CHAMPIONS_LEAGUE_GORUNUM_BASLANGICI = datetime(2026, 10, 1)


# Şimdilik kullanıcıya gösterilmeyen ligler (senkron arka planda sürer).
# Tekrar göstermek için buradan çıkarmak yeterli.
GIZLI_LIG_ID_LISTESI = [NATIONS_LEAGUE_ID]


def gosterilecek_lig_id_listesi():
    """Kullanıcıya gösterilecek lig id listesi.

    Normalde TAKIP_EDILEN_LIG_ID_LISTESI ile aynıdır; tek fark, Şampiyonlar
    Ligi'nin CHAMPIONS_LEAGUE_GORUNUM_BASLANGICI tarihinden önce bu listeden
    çıkarılmasıdır (bkz. yukarıdaki not).
    """
    liste = [lig_id for lig_id in TAKIP_EDILEN_LIG_ID_LISTESI if lig_id not in GIZLI_LIG_ID_LISTESI]
    if datetime.now(timezone.utc).replace(tzinfo=None) < CHAMPIONS_LEAGUE_GORUNUM_BASLANGICI:
        return [lig_id for lig_id in liste if lig_id != CHAMPIONS_LEAGUE_ID]
    return liste


API_TIMEOUT = 30
# Logo çekerken dış servisi en fazla bu kadar bekle (sayfaları tıkamasın)
LOGO_TIMEOUT = 5


# ============================================================
# API-FOOTBALL (api-sports.io) — Süper Lig için
# ============================================================
# football-data.org'un ücretsiz planı Süper Lig'i desteklemediğinden, Süper Lig
# maçları bu ikinci sağlayıcıdan çekilir. Ücretsiz plan: günde 100 istek.
# Anahtar almak için: https://dashboard.api-football.com (ücretsiz kayıt).

API_FOOTBALL_KEY = os.getenv(
    "API_FOOTBALL_KEY",
    ""
).strip()

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"

# API-Football'daki Süper Lig'in KENDİ lig id'si (203). Bizim dahili
# SUPER_LIG_ID (2036) ile karıştırılmamalı — bu sadece API isteklerinde
# kullanılan dış (harici) kimlik.
API_FOOTBALL_SUPER_LIG_ID = 203

# API-Football fikstür id'leri, football-data.org id'leriyle çakışabileceği
# için (ikisi de küçük tam sayılar) Mac.external_id alanında bir ofset
# ekleyerek saklıyoruz. Böylece iki sağlayıcı aynı DB'de güvenle bir arada durur.
API_FOOTBALL_EXTERNAL_ID_OFFSET = 900_000_000


# ============================================================
# ESPN (halka açık, resmi olmayan, ÜCRETSİZ ve API KEY GEREKTİRMEYEN uç nokta)
# — Süper Lig için VARSAYILAN sağlayıcı
# ============================================================
# API-Football'un ücretsiz planı güncel sezona erişim vermediği için
# (sadece eski sezonlar destekleniyor), Süper Lig verisi ESPN'in genel
# kullanıma açık "site.api.espn.com" uç noktasından çekiliyor. Bu uç nokta
# resmi olarak dokümante edilmemiştir (ESPN önceden haber vermeden
# değiştirebilir/kaldırabilir) ama hiçbir API anahtarı veya ücret
# gerektirmez ve güncel sezon dahil çalışır.
ESPN_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_SUPER_LIG_SLUG = "tur.1"
ESPN_NATIONS_LEAGUE_SLUG = "uefa.nations"

# Uluslar Ligi'nde SADECE bu anahtar kelimelerden biri takım adında geçen
# (Türkiye Milli Takımı) maçlar senkronize edilir. ESPN takımı "Türkiye" ya da
# "Turkey" diye adlandırabildiği için ikisi de var; ayrıca ESPN kısaltması "TUR".
NATIONS_LEAGUE_TAKIP_EDILEN_TAKIMLAR = ["türkiye", "turkiye", "turkey"]
NATIONS_LEAGUE_TAKIP_KISALTMALARI = ["TUR"]

# Avrupa kupalarında takip edilen kulüpler (ESPN kısaltmaları: BES, TRAB)
ESPN_AVRUPA_SLUGLARI = {"uefa.europa": "UEFA Avrupa Ligi", "uefa.europa.conf": "UEFA Konferans Ligi"}
# Ön eleme turları (sadece form için; tahmine açılmaz)
ESPN_AVRUPA_ELEME_SLUGLARI = {"uefa.europa_qual": "UEFA Avrupa Ligi ön eleme",
                              "uefa.europa.conf_qual": "UEFA Konferans Ligi ön eleme"}
AVRUPA_TAKIP_EDILEN_TAKIMLAR = ["besiktas", "beşiktaş", "trabzonspor"]
AVRUPA_TAKIP_KISALTMALARI = ["BES", "TRAB"]

# ESPN fikstür id'leri de diğer iki sağlayıcıyla çakışmasın diye kendi
# ofsetini kullanıyor.
ESPN_EXTERNAL_ID_OFFSET = 800_000_000

# ESPN, lig maçları için resmi "kaçıncı hafta" bilgisini vermiyor (sadece
# tarih bazlı takvim var). Bu yüzden haftayı KENDİMİZ hesaplıyoruz: maçları
# tarihe göre sıralayıp aralarında bu kadar günden fazla boşluk olduğunda
# yeni bir "hafta" başladığını varsayıyoruz. Süper Lig genelde Cuma-Pazartesi
# aralığında oynanıp ~5 gün sonra yeni tura geçtiği için bu değer uygun.
ESPN_HAFTA_BOSLUK_GUN = 3


# ============================================================
# SportAPI7 (RapidAPI / Sofascore aynası) — Süper Lig için VARSAYILAN
# sağlayıcı (2. deneme: ESPN'in dokümante olmayan uç noktası zaman zaman
# 400/404 hatası verdiği için buraya geçildi)
# ============================================================
# RapidAPI hesabından ücretsiz "Basic" plana abone olup buradan bir anahtar
# alınır: https://rapidapi.com/rapidsportapi/api/sportapi7
# NOT: Ücretsiz planların çoğu aylık birkaç yüz istekle sınırlıdır, bu yüzden
# senkronizasyon başına sadece 3 istek yapacak şekilde (sezon id + son
# sayfa + gelecek sayfa) tasarlandı — sık sync etmeyin (günde birkaç kez yeterli).
SPORTAPI7_BASE_URL = "https://sportapi7.p.rapidapi.com/api/v1"
SPORTAPI7_HOST = "sportapi7.p.rapidapi.com"

SPORTAPI7_KEY = os.getenv(
    "SPORTAPI7_KEY",
    ""
).strip()

# Sofascore'daki Süper Lig'in (Trendyol Süper Lig) "unique tournament" id'si.
# sofascore.com/football/tournament/turkey/trendyol-super-lig/52 adresindeki
# son sayıdan alınmıştır.
SPORTAPI7_SUPER_LIG_TOURNAMENT_ID = 52

# SportAPI7/Sofascore fikstür id'leri diğer sağlayıcılarla çakışmasın diye
# kendi ofsetini kullanıyor.
SPORTAPI7_EXTERNAL_ID_OFFSET = 700_000_000

# Süper Lig'de SADECE bu takımların maçları senkronize edilir (diğer tüm
# takımların maçları atlanır). İsim karşılaştırması Sofascore'un takım
# adının İÇİNDE bu anahtar kelimelerden biri geçip geçmediğine bakarak
# yapılır (örn. "Galatasaray" adı "Galatasaray FK" içinde geçer).
SUPER_LIG_TAKIP_EDILEN_TAKIMLAR = [
    "fenerbahçe", "fenerbahce",
    "galatasaray",
    "beşiktaş", "besiktas",
    "trabzonspor",
]


def super_lig_takim_filtresi():
    """
    Mac.query sorgularına eklenecek SQLAlchemy koşulu:
      - Süper Lig'de ARTIK takım filtresi YOK — tüm lig gösterilir.
      - Uluslar Ligi maçlarını hâlâ sadece Türkiye Milli Takımı'nın
        maçlarıyla sınırlar.
    Diğer ligler (örn. Şampiyonlar Ligi) bu filtreden ETKİLENMEZ.
    """
    turkiye_kosullari = [
        db.or_(
            Mac.ev_sahibi.ilike(f"%{takim}%"),
            Mac.deplasman.ilike(f"%{takim}%"),
        )
        for takim in NATIONS_LEAGUE_TAKIP_EDILEN_TAKIMLAR
    ]
    return db.or_(Mac.lig_id != NATIONS_LEAGUE_ID, db.or_(*turkiye_kosullari))

# Sofascore'un takım logosu için gereken görseller doğrudan tarayıcıdan
# istendiğinde WAF/hotlink koruması nedeniyle engellenebiliyor. Bu yüzden
# logoları kendi backend'imiz üzerinden (RapidAPI anahtarımızla) çekip
# kısa süreliğine bellekte önbelleğe alarak serviyoruz — bkz. /api/team-logo.
TEAM_LOGO_CACHE = {}
TEAM_LOGO_CACHE_TTL_SANIYE = 7 * 24 * 60 * 60  # 7 gün


# ============================================================
# MODELS
# ============================================================

class Kullanici(UserMixin, db.Model):
    __tablename__ = "kullanicilar"

    id = db.Column(db.Integer, primary_key=True)

    kullanici_adi = db.Column(
        db.String(80),
        unique=True,
        nullable=False
    )

    sifre_hash = db.Column(
        db.String(255),
        nullable=False
    )

    admin = db.Column(
        db.Boolean,
        default=False
    )

    # Yönetici geçici şifre verdiyse True: kullanıcı ilk girişte yeni şifresini belirler
    sifre_degismeli = db.Column(db.Boolean, default=False, nullable=False, server_default=db.false())

    olusturulma = db.Column(
        db.DateTime,
        default=lambda: datetime.utcnow()
    )

    tahminler = db.relationship(
        "Tahmin",
        backref="kullanici",
        lazy=True,
        cascade="all, delete-orphan"
    )

    grup_uyelikleri = db.relationship(
        "GrupUyeligi",
        backref="kullanici",
        lazy=True,
        cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.sifre_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(
            self.sifre_hash,
            password
        )


class Grup(db.Model):
    __tablename__ = "gruplar"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    ad = db.Column(
        db.String(120),
        nullable=False
    )

    davet_kodu = db.Column(
        db.String(30),
        unique=True,
        nullable=False
    )

    sezon = db.Column(
        db.Integer,
        nullable=True
    )

    sosyal_kural = db.Column(
        db.Boolean,
        default=True
    )
    
    aciklama = db.Column(
        db.Text,
        nullable=True
    )

    kurucu_id = db.Column(
        db.Integer,
        db.ForeignKey("kullanicilar.id"),
        nullable=False
    )

    olusturulma = db.Column(
        db.DateTime,
        default=lambda: datetime.utcnow()
    )

    uyeler = db.relationship(
        "GrupUyeligi",
        backref="grup",
        lazy=True,
        cascade="all, delete-orphan"
    )

    tahminler = db.relationship(
        "Tahmin",
        backref="grup",
        lazy=True,
        cascade="all, delete-orphan"
    )


class GrupUyeligi(db.Model):
    __tablename__ = "grup_uyelikleri"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    grup_id = db.Column(
        db.Integer,
        db.ForeignKey("gruplar.id"),
        nullable=False
    )

    kullanici_id = db.Column(
        db.Integer,
        db.ForeignKey("kullanicilar.id"),
        nullable=False
    )

    katilma_tarihi = db.Column(
        db.DateTime,
        default=lambda: datetime.utcnow()
    )

    __table_args__ = (
        db.UniqueConstraint(
            "grup_id",
            "kullanici_id",
            name="unique_grup_kullanici"
        ),
    )


class Mac(db.Model):
    __tablename__ = "maclar"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    external_id = db.Column(
        db.Integer,
        unique=True,
        nullable=False
    )

    ev_sahibi = db.Column(
        db.String(150),
        nullable=False
    )

    deplasman = db.Column(
        db.String(150),
        nullable=False
    )

    ev_logo = db.Column(
        db.String(500)
    )

    deplasman_logo = db.Column(
        db.String(500)
    )

    lig = db.Column(
        db.String(100)
    )

    lig_id = db.Column(
        db.Integer
    )

    sezon = db.Column(
        db.Integer
    )

    hafta = db.Column(
        db.String(50)
    )

    baslangic = db.Column(
        db.DateTime
    )

    durum = db.Column(
        db.String(30)
    )

    api_status = db.Column(
        db.String(30)
    )

    ev_skor = db.Column(
        db.Integer,
        nullable=True
    )

    deplasman_skor = db.Column(
        db.Integer,
        nullable=True
    )

    son_guncelleme = db.Column(
        db.DateTime
    )

    # Doluysa maç SADECE bu gruba aittir (grup kurucusu/admin elle ekledi).
    ozel_grup_id = db.Column(db.Integer, nullable=True, index=True)

    tahminler = db.relationship(
        "Tahmin",
        backref="mac",
        lazy=True,
        cascade="all, delete-orphan"
    )

    def basladi_mi(self):
        if not self.baslangic:
            return False

        return self.baslangic <= utc_now_naive()

    def bitti_mi(self):
        return self.durum == "bitti"

    def oynanabilir_mi(self):
        if not self.baslangic:
            return False

        if self.durum in [
            "bitti",
            "iptal",
            "ertelendi"
        ]:
            return False

        return self.baslangic > utc_now_naive()


class Tahmin(db.Model):
    __tablename__ = "tahminler"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    grup_id = db.Column(
        db.Integer,
        db.ForeignKey("gruplar.id"),
        nullable=False
    )

    mac_id = db.Column(
        db.Integer,
        db.ForeignKey("maclar.id"),
        nullable=False
    )

    kullanici_id = db.Column(
        db.Integer,
        db.ForeignKey("kullanicilar.id"),
        nullable=False
    )

    ev_skor = db.Column(
        db.Integer,
        nullable=False
    )

    deplasman_skor = db.Column(
        db.Integer,
        nullable=False
    )

    puan = db.Column(
        db.Integer,
        default=0
    )

    olusturulma = db.Column(
        db.DateTime,
        default=lambda: datetime.utcnow()
    )

    guncellenme = db.Column(
        db.DateTime,
        default=lambda: datetime.utcnow(),
        onupdate=lambda: datetime.utcnow()
    )

    __table_args__ = (
        db.UniqueConstraint(
            "grup_id",
            "mac_id",
            "kullanici_id",
            name="unique_grup_mac_kullanici"
        ),
    )


class Sistem(db.Model):
    """
    Tek satırlık genel ayar tablosu. Şu an sadece 'puanları sıfırla'
    butonuna en son ne zaman basıldığını tutuyor — recalculate_all_points()
    bu tarihten ÖNCE/O ANDA oynanmış maçların puanını her zaman 0'da
    tutar, böylece yeni bir maç sonucu girildiğinde eski (sıfırlanmış)
    sezonun puanları geri gelmez.
    """
    __tablename__ = "sistem"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    puan_sifirlama_tarihi = db.Column(
        db.DateTime,
        nullable=True
    )


class MacMuafiyet(db.Model):
    """
    Bir maç bu tabloya eklenirse, "Puanları Sıfırla" butonunun kestiği
    tarihten önce oynanmış olsa bile puanı normal şekilde hesaplanmaya
    devam eder (sıfırlamadan MUAF tutulur). Örneğin geçmişte oynanmış ama
    gerçekten sayılması gereken bir maç için admin panelinden işaretlenir.
    """
    __tablename__ = "mac_muafiyetleri"

    mac_id = db.Column(
        db.Integer,
        db.ForeignKey("maclar.id"),
        primary_key=True
    )


class MacSaatDuzeltme(db.Model):
    """
    Admin bir maçın saatini elle değiştirdiğinde (örn. maç ertelendi/öne
    alındı ama API henüz güncellemedi) yeni saat burada saklanır. Senkron
    fonksiyonları maçı API'den her güncellediğinde elle_saat_uygula() ile
    bu saat tekrar uygulanır, böylece API eski saati geri yazamaz.
    Saat, Mac.baslangic gibi naive UTC olarak tutulur.
    """
    __tablename__ = "mac_saat_duzeltmeleri"

    mac_id = db.Column(
        db.Integer,
        db.ForeignKey("maclar.id"),
        primary_key=True
    )

    baslangic = db.Column(
        db.DateTime,
        nullable=False
    )


def elle_saat_uygula(mac):
    """Maç için admin tarafından girilmiş bir saat varsa onu uygular."""
    if mac.id is None:
        return
    duzeltme = db.session.get(MacSaatDuzeltme, mac.id)
    if duzeltme:
        mac.baslangic = duzeltme.baslangic


def sistem_ayarlarini_getir():
    sistem = Sistem.query.first()
    if not sistem:
        sistem = Sistem(puan_sifirlama_tarihi=None)
        db.session.add(sistem)
        db.session.commit()
    return sistem


# ============================================================
# LOGIN
# ============================================================

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(
        Kullanici,
        int(user_id)
    )


# ============================================================
# HELPERS
# ============================================================

class SenkronKaydi(db.Model):
    """Otomatik senkronun en son ne zaman (hangi türde) çalıştığını tutar."""
    __tablename__ = "senkron_kayitlari"

    ad = db.Column(db.String(40), primary_key=True)
    zaman = db.Column(db.DateTime, nullable=False)


class Mesaj(db.Model):
    """Grup sohbet odasındaki tek bir mesaj."""
    __tablename__ = "mesajlar"

    id = db.Column(db.Integer, primary_key=True)
    grup_id = db.Column(db.Integer, db.ForeignKey("gruplar.id", ondelete="CASCADE"), nullable=False, index=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="CASCADE"), nullable=False)
    icerik = db.Column(db.String(MESAJ_MAX_UZUNLUK), nullable=False)
    olusturulma = db.Column(db.DateTime, default=lambda: datetime.utcnow(), nullable=False)
    # Yanıtlanan mesaj (yoksa None). Eski tablolara initialize_database() ekler.
    yanit_id = db.Column(db.Integer, nullable=True)

    kullanici = db.relationship("Kullanici")
    yanitlanan = db.relationship(
        "Mesaj", primaryjoin="Mesaj.yanit_id == Mesaj.id", foreign_keys="Mesaj.yanit_id",
        remote_side="Mesaj.id", uselist=False, viewonly=True, lazy="select",
    )

    def to_dict(self):
        veri = {
            "id": self.id,
            "kullanici_id": self.kullanici_id,
            "kullanici_adi": self.kullanici.kullanici_adi if self.kullanici else "?",
            "icerik": self.icerik,
            "saat": tr_saat_filter(self.olusturulma, "%d.%m %H:%M"),
            "gun": tr_saat_filter(self.olusturulma, "%Y-%m-%d"),
            "hm": tr_saat_filter(self.olusturulma, "%H:%M"),
            "ts": int(calendar.timegm(self.olusturulma.timetuple())) if self.olusturulma else 0,
            "yanit": None,
        }
        if self.yanit_id:
            y = self.yanitlanan
            veri["yanit"] = ({"id": y.id, "kullanici_adi": y.kullanici.kullanici_adi if y.kullanici else "?",
                              "icerik": y.icerik[:120]} if y else
                             {"id": self.yanit_id, "kullanici_adi": "", "icerik": "Bu mesaj silindi."})
        return veri


class YzAnaliz(db.Model):
    """Robot Ziya'nın maç başına yazdığı kısa analiz (maç başlayınca gösterilir)."""
    __tablename__ = "yz_analizleri"
    mac_id = db.Column(db.Integer, db.ForeignKey("maclar.id"), primary_key=True)
    metin = db.Column(db.Text, nullable=False)
    kaynak = db.Column(db.String(40), nullable=True)
    olusturulma = db.Column(db.DateTime, default=lambda: datetime.utcnow())


class ZiyaretGunluk(db.Model):
    """Günlük tekil ziyaretçi kaydı. Çerez kullanılmaz: ziyaretçi kimliği,
    o güne özel tuzla IP + tarayıcı bilgisinden üretilen geri çevrilemez bir özettir
    (ertesi gün aynı kişi farklı bir özet alır, kimse geriye doğru izlenemez)."""
    __tablename__ = "ziyaret_gunluk"
    id = db.Column(db.Integer, primary_key=True)
    gun = db.Column(db.Date, nullable=False, index=True)
    ziyaretci = db.Column(db.String(16), nullable=False)
    kullanici_id = db.Column(db.Integer, nullable=True)
    sayfa_sayisi = db.Column(db.Integer, nullable=False, default=1)
    ilk_sayfa = db.Column(db.String(120))
    kaynak = db.Column(db.String(100))
    cihaz = db.Column(db.String(10))
    tarayici = db.Column(db.String(40))
    ilk_saat = db.Column(db.DateTime, default=lambda: datetime.utcnow())
    son_saat = db.Column(db.DateTime, default=lambda: datetime.utcnow())
    __table_args__ = (db.UniqueConstraint("gun", "ziyaretci", name="uq_ziyaret_gun_ziyaretci"),)


class SayfaGunluk(db.Model):
    """Gün bazında sayfa görüntülenme sayıları."""
    __tablename__ = "sayfa_gunluk"
    id = db.Column(db.Integer, primary_key=True)
    gun = db.Column(db.Date, nullable=False, index=True)
    yol = db.Column(db.String(120), nullable=False)
    sayi = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint("gun", "yol", name="uq_sayfa_gun_yol"),)


class Haber(db.Model):
    """Türk spor sitelerinin RSS akışlarından gelen, Türk takımlarıyla ilgili haber başlıkları."""
    __tablename__ = "haberler"
    id = db.Column(db.Integer, primary_key=True)
    link = db.Column(db.String(600), unique=True, nullable=False)
    baslik = db.Column(db.String(400), nullable=False)
    kaynak = db.Column(db.String(80), nullable=True)
    gorsel = db.Column(db.String(800), nullable=True)
    yayin = db.Column(db.DateTime, nullable=True, index=True)
    eklenme = db.Column(db.DateTime, default=lambda: datetime.utcnow())


class DestekKonusma(db.Model):
    """Kullanıcı (ya da giriş yapamayan misafir) ile admin arasındaki birebir yazışma."""
    __tablename__ = "destek_konusmalari"
    id = db.Column(db.Integer, primary_key=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="CASCADE"), nullable=True, index=True)
    misafir_ad = db.Column(db.String(80))
    anahtar_hash = db.Column(db.String(64), unique=True)     # misafir için tarayıcıdaki gizli anahtar
    olusturulma = db.Column(db.DateTime, default=lambda: datetime.utcnow())
    son_mesaj = db.Column(db.DateTime, default=lambda: datetime.utcnow(), index=True)
    admin_okumadi = db.Column(db.Boolean, nullable=False, default=False)
    kullanici_okumadi = db.Column(db.Boolean, nullable=False, default=False)
    kullanici = db.relationship("Kullanici")
    mesajlar = db.relationship("DestekMesaj", backref="konusma", order_by="DestekMesaj.id",
                               cascade="all, delete-orphan")

    def gorunen_ad(self):
        if self.kullanici:
            return self.kullanici.kullanici_adi
        return (self.misafir_ad or "Misafir") + " (giriş yapmadan)"


class DestekMesaj(db.Model):
    __tablename__ = "destek_mesajlari"
    id = db.Column(db.Integer, primary_key=True)
    konusma_id = db.Column(db.Integer, db.ForeignKey("destek_konusmalari.id", ondelete="CASCADE"), nullable=False, index=True)
    admin_mi = db.Column(db.Boolean, nullable=False, default=False)
    metin = db.Column(db.Text, nullable=False)
    zaman = db.Column(db.DateTime, default=lambda: datetime.utcnow())


class KelimeOyunu(db.Model):
    """Günlük kelime oyunu (Wordle). Tahmin ligi puanlarından tamamen bağımsız."""
    __tablename__ = "kelime_oyunlari"
    __table_args__ = (db.UniqueConstraint("kullanici_id", "gun", name="uq_kelime_kullanici_gun"),)
    id = db.Column(db.Integer, primary_key=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="CASCADE"), nullable=False, index=True)
    gun = db.Column(db.Date, nullable=False, index=True)
    tahminler = db.Column(db.Text, nullable=False, default="")      # virgülle ayrılmış
    bitti = db.Column(db.Boolean, nullable=False, default=False)
    kazandi = db.Column(db.Boolean, nullable=False, default=False)
    baslangic = db.Column(db.DateTime)
    bitis = db.Column(db.DateTime)
    kullanici = db.relationship("Kullanici")

    def liste(self):
        return [t for t in (self.tahminler or "").split(",") if t]


class FormMaci(db.Model):
    """Sadece "son maçlar" formu için tutulan, tahmine açılmayan maç sonuçları
    (örn. Avrupa kupalarında Beşiktaş/Trabzonspor'un rakiplerinin diğer maçları)."""
    __tablename__ = "form_maclari"
    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.BigInteger, unique=True, nullable=False)
    lig_id = db.Column(db.Integer, nullable=False, index=True)
    lig = db.Column(db.String(80))
    baslangic = db.Column(db.DateTime, nullable=False, index=True)
    ev_sahibi = db.Column(db.String(120), nullable=False)
    deplasman = db.Column(db.String(120), nullable=False)
    ev_skor = db.Column(db.Integer, nullable=False)
    deplasman_skor = db.Column(db.Integer, nullable=False)


class Anket(db.Model):
    """Admin'in kullanıcılara sorduğu açık (isimli) anket."""
    __tablename__ = "anketler"
    id = db.Column(db.Integer, primary_key=True)
    soru = db.Column(db.String(300), nullable=False)
    secenekler = db.Column(db.Text, nullable=False)          # satır satır seçenekler
    bitis = db.Column(db.DateTime, nullable=False)           # UTC
    kapali = db.Column(db.Boolean, nullable=False, default=False)
    olusturulma = db.Column(db.DateTime, default=lambda: datetime.utcnow())

    def secenek_listesi(self):
        return [x.strip() for x in (self.secenekler or "").split("\n") if x.strip()]

    def acik_mi(self):
        return not self.kapali and utc_now_naive() < self.bitis


class AnketOyu(db.Model):
    __tablename__ = "anket_oylari"
    id = db.Column(db.Integer, primary_key=True)
    anket_id = db.Column(db.Integer, db.ForeignKey("anketler.id", ondelete="CASCADE"), nullable=False, index=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="CASCADE"), nullable=False)
    secenek = db.Column(db.Integer, nullable=False)
    zaman = db.Column(db.DateTime, default=lambda: datetime.utcnow())
    kullanici = db.relationship("Kullanici")
    __table_args__ = (db.UniqueConstraint("anket_id", "kullanici_id", name="uq_anket_oy_kullanici"),)


class TahminCezasi(db.Model):
    """Tahmin girilmeyen maç için ceza puanı (grup bazında, Robot Ziya hariç)."""
    __tablename__ = "tahmin_cezalari"
    id = db.Column(db.Integer, primary_key=True)
    grup_id = db.Column(db.Integer, db.ForeignKey("gruplar.id", ondelete="CASCADE"), nullable=False, index=True)
    mac_id = db.Column(db.Integer, db.ForeignKey("maclar.id", ondelete="CASCADE"), nullable=False, index=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="CASCADE"), nullable=False, index=True)
    puan = db.Column(db.Integer, nullable=False, default=-1)
    olusturulma = db.Column(db.DateTime, default=lambda: datetime.utcnow())
    __table_args__ = (db.UniqueConstraint("grup_id", "mac_id", "kullanici_id", name="uq_ceza_grup_mac_kullanici"),)


class GeriBildirim(db.Model):
    """Kullanıcıların yöneticiye ilettiği fikir, öneri ve hata bildirimleri."""
    __tablename__ = "geri_bildirimler"
    id = db.Column(db.Integer, primary_key=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="SET NULL"), nullable=True)
    ad = db.Column(db.String(80))           # girişsiz gönderenin yazdığı isim (isteğe bağlı)
    tur = db.Column(db.String(20), nullable=False, default="oneri")   # oneri / hata / diger
    metin = db.Column(db.Text, nullable=False)
    sayfa = db.Column(db.String(200))       # nereden geldiği (referrer yolu)
    okundu = db.Column(db.Boolean, nullable=False, default=False)
    olusturulma = db.Column(db.DateTime, default=lambda: datetime.utcnow())
    kullanici = db.relationship("Kullanici")


class SifreTalebi(db.Model):
    """Şifresini unutan kullanıcının yöneticiye ilettiği sıfırlama talebi.
    Talep eden tarayıcıya gizli bir anahtar verilir; yönetici onaylayınca
    yeni şifre yalnızca o anahtarla açılan sayfadan belirlenebilir."""
    __tablename__ = "sifre_talepleri"
    id = db.Column(db.Integer, primary_key=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="CASCADE"), nullable=False, index=True)
    anahtar_hash = db.Column(db.String(64), nullable=False, unique=True)
    notu = db.Column(db.String(300))
    durum = db.Column(db.String(20), nullable=False, default="bekliyor")  # bekliyor/onaylandi/reddedildi/kullanildi/iptal
    olusturulma = db.Column(db.DateTime, default=lambda: datetime.utcnow())
    karar_zamani = db.Column(db.DateTime)
    kullanici = db.relationship("Kullanici")


class SohbetOkuma(db.Model):
    """Kullanıcının bir grubun sohbetinde en son gördüğü mesaj (okunmamış sayacı için)."""
    __tablename__ = "sohbet_okumalari"

    grup_id = db.Column(db.Integer, db.ForeignKey("gruplar.id", ondelete="CASCADE"), primary_key=True)
    kullanici_id = db.Column(db.Integer, db.ForeignKey("kullanicilar.id", ondelete="CASCADE"), primary_key=True)
    son_okunan_id = db.Column(db.Integer, nullable=False, default=0)


def sohbet_okundu_isaretle(grup_id, kullanici_id, mesaj_id):
    if not mesaj_id:
        return
    kayit = db.session.get(SohbetOkuma, (grup_id, kullanici_id))
    if kayit is None:
        db.session.add(SohbetOkuma(grup_id=grup_id, kullanici_id=kullanici_id, son_okunan_id=mesaj_id))
    elif mesaj_id > kayit.son_okunan_id:
        kayit.son_okunan_id = mesaj_id
    db.session.commit()


def okunmamis_mesaj_sayisi(grup_id, kullanici_id):
    kayit = db.session.get(SohbetOkuma, (grup_id, kullanici_id))
    son = kayit.son_okunan_id if kayit else 0
    return Mesaj.query.filter(Mesaj.grup_id == grup_id, Mesaj.id > son,
                              Mesaj.kullanici_id != kullanici_id).count()


def gruplara_gore_okunmamis(kullanici_id):
    """{grup_id: okunmamış mesaj sayısı} — kullanıcının tüm grupları için TEK sorgu."""
    satirlar = db.session.execute(db.text("""
        SELECT gu.grup_id, COUNT(m.id)
        FROM grup_uyelikleri gu
        LEFT JOIN sohbet_okumalari so ON so.grup_id = gu.grup_id AND so.kullanici_id = gu.kullanici_id
        LEFT JOIN mesajlar m ON m.grup_id = gu.grup_id
             AND m.id > COALESCE(so.son_okunan_id, 0)
             AND m.kullanici_id <> gu.kullanici_id
        WHERE gu.kullanici_id = :uid
        GROUP BY gu.grup_id
    """), {"uid": kullanici_id}).all()
    return {gid: int(n) for gid, n in satirlar}


def utc_now_naive():
    return datetime.now(
        timezone.utc
    ).replace(
        tzinfo=None
    )


def parse_api_date(value):
    if not value:
        return None
    try:
        value = str(value)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception as e:
        print("Tarih parse hatası:", value, e)
        return None


def generate_invite_code(length=8):
    import secrets
    import string
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(chars) for _ in range(length))
        if not Grup.query.filter_by(davet_kodu=code).first():
            return code


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("giris"))
        if not current_user.admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ============================================================
# FOOTBALL API & SYNC
# ============================================================

def football_data_get(endpoint, params=None):
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_TOKEN bulunamadı.")
    url = FOOTBALL_DATA_BASE_URL.rstrip("/") + "/" + endpoint.lstrip("/")
    headers = {"X-Auth-Token": FOOTBALL_DATA_TOKEN, "Accept": "application/json"}
    response = requests.get(url, headers=headers, params=params or {}, timeout=API_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"Football-Data API Hatası {response.status_code}: {response.text[:500]}")
    return response.json()


def map_match_status(api_status):
    mapping = {
        "FINISHED": "bitti",
        "AWARDED": "bitti",
        "CANCELLED": "iptal",
        "POSTPONED": "ertelendi",
        "SUSPENDED": "ertelendi",
        "IN_PLAY": "oynaniyor",
        "PAUSED": "oynaniyor",
        "TIMED": "planlandi",
        "SCHEDULED": "planlandi",
    }
    return mapping.get(api_status, "planlandi")


def football_match_to_db(match, competition_id=None, active_season=None):
    external_id = match.get("id")
    if not external_id:
        return None

    home_team = match.get("homeTeam") or {}
    away_team = match.get("awayTeam") or {}
    competition = match.get("competition") or {}
    season_data = match.get("season") or {}
    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}

    season = active_season
    if season_data.get("startDate"):
        try:
            season = int(str(season_data["startDate"])[:4])
        except Exception:
            pass

    lig_id = competition_id
    if competition.get("id"):
        lig_id = competition.get("id")

    mac = Mac.query.filter_by(external_id=int(external_id)).first()
    if not mac:
        mac = Mac(external_id=int(external_id))
        db.session.add(mac)

    mac.ev_sahibi = home_team.get("name") or "Bilinmeyen Takım"
    mac.deplasman = away_team.get("name") or "Bilinmeyen Takım"
    mac.ev_logo = home_team.get("crest")
    mac.deplasman_logo = away_team.get("crest")
    mac.lig = competition.get("name") or "UEFA Champions League"
    mac.lig_id = lig_id
    mac.sezon = season
    mac.hafta = str(match.get("matchday")) if match.get("matchday") else None
    mac.baslangic = parse_api_date(match.get("utcDate"))
    elle_saat_uygula(mac)
    api_status = match.get("status")
    home_score = full_time.get("home")
    away_score = full_time.get("away")

    # Admin sonucu elle girdiyse, API maçı "bitti" demeden onu ezme
    if mac.api_status == "MANUEL" and api_status not in ("FINISHED", "AWARDED"):
        mac.son_guncelleme = utc_now_naive()
        return mac

    mac.api_status = api_status
    mac.durum = map_match_status(api_status)
    if home_score is not None:
        mac.ev_skor = home_score
    if away_score is not None:
        mac.deplasman_skor = away_score

    # API maçı geç "bitti" yapsa bile en geç PUANLAMA_EN_GEC_DAKIKA'da puanla
    _gecikmis_maci_bitir(mac)

    mac.son_guncelleme = utc_now_naive()
    return mac


def sync_league(competition_code, requested_season=None, aylar=None):
    """
    Verilen lig kodu için doğru sağlayıcıyı seçip senkronizasyonu yapar.
    LEAGUES sözlüğündeki "provider" alanına göre football-data.org, ESPN
    veya API-Football fonksiyonuna yönlendirir. Her lig için ayrı ayrı
    çağrılabilir.
    """
    lig_bilgisi = LEAGUES.get(competition_code, {})
    provider = lig_bilgisi.get("provider", "football-data")

    if provider == "sportapi7":
        return sync_super_lig_sportapi7(requested_season=requested_season)

    if provider == "espn":
        return sync_super_lig_espn(requested_season=requested_season, aylar=aylar)

    if provider == "espn-nations":
        return sync_nations_league_espn(requested_season=requested_season, aylar=aylar)

    if provider == "espn-avrupa":
        return sync_avrupa_espn(requested_season=requested_season, aylar=aylar)

    if provider == "api-football":
        return sync_super_lig_api_football(requested_season=requested_season)

    return sync_league_football_data(competition_code, requested_season=requested_season)


def sync_league_football_data(competition_code, requested_season=None):
    """
    Verilen lig koduna (örn. 'CL') ait maçları football-data.org'dan
    çekip veritabanına yazar.
    """
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_TOKEN bulunamadı.")

    lig_bilgisi = LEAGUES.get(competition_code, {})
    fallback_id = lig_bilgisi.get("id")

    info = football_data_get(f"competitions/{competition_code}")
    competition_id = info.get("id") or fallback_id
    current_season = info.get("currentSeason") or {}
    api_season = None

    if current_season.get("startDate"):
        try:
            api_season = int(str(current_season["startDate"])[:4])
        except Exception:
            pass

    # ÖNEMLİ: dateFrom=today ile çekmek geçmişte kalan (oynanmış) maçları
    # bir daha hiç API'den döndürmediği için, o maçların skoru/puanı asla
    # güncellenmiyordu. Bunun yerine tüm sezonu "season" parametresiyle
    # çekiyoruz; böylece geçmiş, oynanan ve gelecek tüm maçlar her
    # senkronizasyonda güncellenir.
    season_to_use = requested_season or api_season
    response = football_data_get(
        f"competitions/{competition_code}/matches",
        params={"season": season_to_use} if season_to_use else None,
    )
    matches = response.get("matches", [])

    inserted = 0
    updated = 0
    skipped = 0

    for match in matches:
        competition = match.get("competition") or {}
        if competition.get("code") != competition_code:
            skipped += 1
            continue

        external_id = match.get("id")
        if not external_id:
            skipped += 1
            continue

        existing = Mac.query.filter_by(external_id=int(external_id)).first()
        season_data = match.get("season") or {}
        season = None
        if season_data.get("startDate"):
            try:
                season = int(str(season_data["startDate"])[:4])
            except Exception:
                pass

        result = football_match_to_db(match, competition_id=competition_id, active_season=season)
        if not result:
            skipped += 1
            continue

        if existing:
            updated += 1
        else:
            inserted += 1

    db.session.commit()
    recalculate_all_points()

    return {
        "success": True,
        "lig_kodu": competition_code,
        "competition_id": competition_id,
        "api_matches": len(matches),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped
    }


def sportapi7_get(path, params=None):
    if not SPORTAPI7_KEY:
        raise RuntimeError("SPORTAPI7_KEY bulunamadı.")
    url = f"{SPORTAPI7_BASE_URL}{path}"
    headers = {"X-RapidAPI-Key": SPORTAPI7_KEY, "X-RapidAPI-Host": SPORTAPI7_HOST}
    response = requests.get(url, headers=headers, params=params or {}, timeout=API_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"SportAPI7 Hatası {response.status_code}: {response.text[:500]}")
    return response.json()


def sportapi7_guncel_sezon_id():
    data = sportapi7_get(f"/unique-tournament/{SPORTAPI7_SUPER_LIG_TOURNAMENT_ID}/seasons")
    sezonlar = data.get("seasons", [])
    if not sezonlar:
        raise RuntimeError("SportAPI7: hiç sezon bulunamadı.")
    # Liste en güncel sezon en başta olacak şekilde sıralı gelir.
    return sezonlar[0].get("id")


def map_sportapi7_status(status):
    tur = (status or {}).get("type")
    mapping = {
        "finished": "bitti",
        "inprogress": "oynaniyor",
        "notstarted": "planlandi",
        "postponed": "ertelendi",
        "suspended": "ertelendi",
        "interrupted": "ertelendi",
        "canceled": "iptal",
        "cancelled": "iptal",
        "abandoned": "iptal",
    }
    return mapping.get(tur, "planlandi")


def sportapi7_takip_edilen_mi(event):
    """
    ARTIK takım bazlı filtre YOK — Süper Lig'deki tüm maçlar kabul edilir.
    (Fonksiyon adı geriye dönük uyumluluk için korunuyor.) Bu fonksiyon
    sadece DAHA ÖNCE ZATEN ÇEKİLMİŞ olan event listesi üzerinde çalışır;
    /unique-tournament/.../events/last/0 ve /next/0 uç noktaları zaten TÜM
    Süper Lig maçlarını tek seferde döndürüyor, yani filtreyi kaldırmak
    EK bir API isteği YARATMAZ.
    """
    return True


def sportapi7_event_to_db(event, season_year):
    raw_external_id = event.get("id")
    if not raw_external_id:
        return None
    external_id = SPORTAPI7_EXTERNAL_ID_OFFSET + int(raw_external_id)

    home_team = event.get("homeTeam") or {}
    away_team = event.get("awayTeam") or {}
    status = event.get("status") or {}
    round_info = event.get("roundInfo") or {}
    home_score = (event.get("homeScore") or {}).get("current")
    away_score = (event.get("awayScore") or {}).get("current")
    start_ts = event.get("startTimestamp")

    mac = Mac.query.filter_by(external_id=external_id).first()
    if not mac:
        mac = Mac(external_id=external_id)
        db.session.add(mac)

    mac.ev_sahibi = home_team.get("name") or "Bilinmeyen Takım"
    mac.deplasman = away_team.get("name") or "Bilinmeyen Takım"
    # Takımın GERÇEK amblemini önce dener: /api/team-logo proxy'si sunucu
    # tarafında sırayla SportAPI7 (Sofascore) ve TheSportsDB'yi dener; ikisi
    # de başarısız olursa proxy'nin kendisi zaten en son çare olarak statik
    # SVG'ye yönlendiriyor (bkz. team_logo()). team_id yoksa (nadiren) direkt
    # statik SVG kullanılır. Ayrıca şablonlarda takim_logo_url() ile
    # istemci tarafında da (onerror) aynı statik dosyaya düşülüyor — yani
    # gerçek logo görünmeyi dener, hiç görünmezse takım rozeti HER ZAMAN
    # garanti çalışır.
    if home_team.get("id"):
        mac.ev_logo = f"/api/team-logo/{home_team['id']}?name={quote(mac.ev_sahibi)}"
    else:
        ev_statik_dosya = _statik_logo_dosyasi(mac.ev_sahibi)
        if ev_statik_dosya:
            mac.ev_logo = url_for("static", filename=f"logos/{ev_statik_dosya}")

    if away_team.get("id"):
        mac.deplasman_logo = f"/api/team-logo/{away_team['id']}?name={quote(mac.deplasman)}"
    else:
        deplasman_statik_dosya = _statik_logo_dosyasi(mac.deplasman)
        if deplasman_statik_dosya:
            mac.deplasman_logo = url_for("static", filename=f"logos/{deplasman_statik_dosya}")
    mac.lig = "Süper Lig"
    mac.lig_id = SUPER_LIG_ID
    mac.sezon = season_year
    mac.hafta = str(round_info.get("round")) if round_info.get("round") else None
    if start_ts:
        mac.baslangic = datetime.fromtimestamp(start_ts, tz=timezone.utc).replace(tzinfo=None)
    elle_saat_uygula(mac)
    mac.api_status = status.get("description")
    mac.durum = map_sportapi7_status(status)

    if home_score is not None:
        mac.ev_skor = home_score
    if away_score is not None:
        mac.deplasman_skor = away_score

    mac.son_guncelleme = utc_now_naive()
    return mac


def sync_super_lig_sportapi7(requested_season=None):
    """
    Süper Lig maçlarını SportAPI7 (RapidAPI üzerinden Sofascore aynası)
    kullanarak çeker. Ücretsiz RapidAPI planları genelde ayda birkaç yüz
    istekle sınırlı olduğundan, her senkronizasyonda sadece 3 istek yapılır:
    güncel sezon id'si + en son oynanan sayfa (skorlar için) + gelecek sayfa
    (yaklaşan maçlar için).
    """
    if not SPORTAPI7_KEY:
        raise RuntimeError("SPORTAPI7_KEY bulunamadı.")

    season_id = sportapi7_guncel_sezon_id()

    simdi = datetime.now(timezone.utc)
    sezon_baslangic_yili = requested_season or (simdi.year if simdi.month >= 7 else simdi.year - 1)

    events = []
    for yon in ("last", "next"):
        try:
            data = sportapi7_get(
                f"/unique-tournament/{SPORTAPI7_SUPER_LIG_TOURNAMENT_ID}/season/{season_id}/events/{yon}/0"
            )
        except RuntimeError:
            continue
        events.extend(data.get("events", []))

    inserted = 0
    updated = 0
    skipped = 0

    for event in events:
        if not sportapi7_takip_edilen_mi(event):
            skipped += 1
            continue

        raw_external_id = event.get("id")
        if not raw_external_id:
            skipped += 1
            continue

        existing = Mac.query.filter_by(
            external_id=SPORTAPI7_EXTERNAL_ID_OFFSET + int(raw_external_id)
        ).first()

        result = sportapi7_event_to_db(event, sezon_baslangic_yili)
        if not result:
            skipped += 1
            continue

        if existing:
            updated += 1
        else:
            inserted += 1

    db.session.commit()
    recalculate_all_points()

    return {
        "success": True,
        "lig_kodu": SUPER_LIG_CODE,
        "competition_id": SUPER_LIG_ID,
        "api_matches": len(events),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped
    }


def espn_get(slug, resource, params=None):
    url = f"{ESPN_BASE_URL}/{slug}/{resource}"
    headers = {"Accept": "application/json"}
    response = requests.get(url, headers=headers, params=params or {}, timeout=API_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"ESPN API Hatası {response.status_code}: {response.text[:500]}")
    return response.json()


def map_espn_status(status_type):
    """
    ESPN'in status.type.state alanı: 'pre' (henüz oynanmadı), 'in' (devam
    ediyor), 'post' (bitti). completed=False + state='post' ertelenmiş/iptal
    anlamına gelebilir; description alanına da bakıyoruz.
    """
    state = (status_type or {}).get("state")
    completed = (status_type or {}).get("completed")
    description = ((status_type or {}).get("description") or "").lower()

    if "postponed" in description or "ertelen" in description:
        return "ertelendi"
    if "cancel" in description or "abandon" in description:
        return "iptal"
    if state == "post" and completed:
        return "bitti"
    if state == "in":
        return "oynaniyor"
    return "planlandi"


def espn_events_to_maclar(events, bosluk_gun=None):
    """
    ESPN scoreboard cevabındaki "events" listesini Mac kayıtlarına
    dönüştürür. Önce tüm eventleri tarihe göre sıralayıp aralarındaki
    boşluğa göre KENDİ "hafta" numaramızı hesaplıyoruz (ESPN resmi bir
    matchday/round numarası vermiyor).
    """
    ayristirilan = []
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        tarih = parse_api_date(event.get("date"))
        if tarih is None:
            continue
        ayristirilan.append((tarih, event))

    ayristirilan.sort(key=lambda x: x[0])

    # Yeni hafta: maçlar arasında uzun boşluk varsa YA DA bu haftada zaten
    # oynamış bir takım tekrar sahaya çıkıyorsa (her takım haftada bir maç
    # oynar). Sadece boşluğa bakmak, Pazartesi maçından 4 gün sonraki Cuma
    # maçını aynı haftaya katıyordu.
    hafta_no = 0
    onceki_tarih = None
    haftadaki_takimlar = set()
    sonuc = []
    for tarih, event in ayristirilan:
        takimlar = {((c.get("team") or {}).get("id") or (c.get("team") or {}).get("displayName"))
                    for c in ((event.get("competitions") or [{}])[0].get("competitors") or [])}
        takimlar.discard(None)
        if onceki_tarih is None or (tarih - onceki_tarih).days > (
            ESPN_HAFTA_BOSLUK_GUN if bosluk_gun is None else bosluk_gun
        ) or (takimlar & haftadaki_takimlar):
            hafta_no += 1
            haftadaki_takimlar = set()
        haftadaki_takimlar |= takimlar
        onceki_tarih = tarih
        sonuc.append((event, hafta_no))
    return sonuc


def espn_event_to_db(event, hafta_no, season_year, lig_adi="Süper Lig", lig_id=None):
    raw_external_id = event.get("id")
    if not raw_external_id:
        return None

    external_id = ESPN_EXTERNAL_ID_OFFSET + int(raw_external_id)

    competitions = event.get("competitions") or []
    competition = competitions[0] if competitions else {}
    competitors = competition.get("competitors") or []

    ev_takim = next((c for c in competitors if c.get("homeAway") == "home"), {})
    deplasman_takim = next((c for c in competitors if c.get("homeAway") == "away"), {})

    status_type = (event.get("status") or competition.get("status") or {}).get("type") or {}

    mac = Mac.query.filter_by(external_id=external_id).first()
    if not mac:
        mac = Mac(external_id=external_id)
        db.session.add(mac)

    mac.ev_sahibi = (ev_takim.get("team") or {}).get("displayName") or "Bilinmeyen Takım"
    mac.deplasman = (deplasman_takim.get("team") or {}).get("displayName") or "Bilinmeyen Takım"
    mac.ev_logo = (ev_takim.get("team") or {}).get("logo")
    mac.deplasman_logo = (deplasman_takim.get("team") or {}).get("logo")
    mac.lig = lig_adi
    mac.lig_id = lig_id if lig_id is not None else SUPER_LIG_ID
    mac.sezon = season_year
    mac.hafta = str(hafta_no)
    mac.baslangic = parse_api_date(event.get("date"))
    elle_saat_uygula(mac)
    mac.api_status = status_type.get("name")
    mac.durum = map_espn_status(status_type)

    home_score = ev_takim.get("score")
    away_score = deplasman_takim.get("score")
    if home_score not in (None, ""):
        try:
            mac.ev_skor = int(home_score)
        except (TypeError, ValueError):
            pass
    if away_score not in (None, ""):
        try:
            mac.deplasman_skor = int(away_score)
        except (TypeError, ValueError):
            pass

    mac.son_guncelleme = utc_now_naive()
    return mac


_TR_HARF = str.maketrans("çğıöşüâîûÇĞİÖŞÜÂÎÛ", "cgiosuaiucgiosuaiu")
_TAKIM_GEREKSIZ_KELIMELER = {"fk", "jk", "sk", "as", "a.s.", "kulubu", "spor", "istanbul",
                             "sportif", "faaliyetler", "club", "fc", "1965"}


def takim_adi_normalize(ad):
    """'Beşiktaş JK', 'Besiktas', 'Başakşehir FK', 'Istanbul Basaksehir' gibi
    farklı yazımları karşılaştırılabilir hale getirir."""
    ad = (ad or "").translate(_TR_HARF).lower().replace("-", " ").replace(".", " ")
    kelimeler = [k for k in ad.split() if k and k not in _TAKIM_GEREKSIZ_KELIMELER]
    return " ".join(kelimeler) or ad.strip()


def takimlar_ayni_mi(a, b):
    na, nb = takim_adi_normalize(a), takim_adi_normalize(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return na.split()[0] == nb.split()[0] and len(na.split()[0]) >= 4


def _espn_takim(event, taraf):
    competitions = event.get("competitions") or []
    competition = competitions[0] if competitions else {}
    for c in competition.get("competitors") or []:
        if c.get("homeAway") == taraf:
            return c
    return {}


def sync_super_lig_espn(requested_season=None, aylar=None):
    """
    Süper Lig maçlarını ESPN'in ücretsiz, anahtar gerektirmeyen uç noktasından
    çeker (kota yok). Gelen her maçı ÖNCE veritabanındaki mevcut maçla
    (başlama saati + takım adları) eşleştirir; böylece başka bir sağlayıcıdan
    (SportAPI7 vb.) gelmiş maçlar ÇİFT kayıt olmaz ve üzerlerindeki tahminler
    kaybolmaz. Eşleşen maçta takım adları ve hafta numarası korunur; skor,
    durum, saat ve logo ESPN'den güncellenir.
    Admin panelinden ELLE girilmiş sonuçlara (api_status == 'MANUEL') dokunulmaz.
    """
    simdi = datetime.now(timezone.utc)
    if requested_season:
        sezon_baslangic_yili = requested_season
    else:
        sezon_baslangic_yili = simdi.year if simdi.month >= 7 else simdi.year - 1

    # ESPN "dates=BASLANGIC-BITIS" aralık formunu 400 ile reddediyor; AY (YYYYMM)
    # formu çalışıyor. Sezonu ay ay (Temmuz-Haziran, 12 istek) çekip event
    # id'ye göre tekilleştiriyoruz. Tek bir ay hata verirse diğerleri devam eder.
    if not aylar:  # belirli aylar verilmediyse tüm sezon
        aylar = [(sezon_baslangic_yili, a) for a in range(7, 13)] + [
            (sezon_baslangic_yili + 1, a) for a in range(1, 7)
        ]
    eventler_by_id = {}
    hatalar = []
    basarili_istek = 0
    for yil, ay in aylar:
        try:
            yanit = espn_get(ESPN_SUPER_LIG_SLUG, "scoreboard",
                             params={"dates": f"{yil}{ay:02d}", "limit": 500})
            basarili_istek += 1
        except Exception as e:
            hatalar.append(f"{yil}-{ay:02d}: {e}")
            continue
        for e in yanit.get("events", []) or []:
            if e.get("id"):
                eventler_by_id[e["id"]] = e
    if basarili_istek == 0:
        raise RuntimeError("ESPN Süper Lig istekleri başarısız: " + " | ".join(hatalar[:2]))
    events = list(eventler_by_id.values())
    event_hafta_ciftleri = espn_events_to_maclar(events)

    mevcut_maclar = Mac.query.filter(Mac.lig_id == SUPER_LIG_ID).all()
    kullanilan_idler = set()

    # ESPN adı -> sitede kullanılan Türkçe ad (ör. 'Besiktas' -> 'Beşiktaş JK')
    bilinen_adlar = {}
    for m in mevcut_maclar:
        for ad in (m.ev_sahibi, m.deplasman):
            if ad:
                bilinen_adlar.setdefault(takim_adi_normalize(ad), ad)

    def turkce_ad(espn_adi):
        n = takim_adi_normalize(espn_adi)
        if n in bilinen_adlar:
            return bilinen_adlar[n]
        for norm, ad in bilinen_adlar.items():
            if takimlar_ayni_mi(espn_adi, ad):
                return ad
        return espn_adi

    def eslesen_mac(event, ev_adi, dep_adi, tarih):
        external_id = ESPN_EXTERNAL_ID_OFFSET + int(event["id"])
        for m in mevcut_maclar:
            if m.external_id == external_id:
                return m
        for m in mevcut_maclar:
            if m.id in kullanilan_idler or not m.baslangic or tarih is None:
                continue
            if abs((m.baslangic - tarih).total_seconds()) > 3 * 24 * 3600:
                continue
            if takimlar_ayni_mi(m.ev_sahibi, ev_adi) and takimlar_ayni_mi(m.deplasman, dep_adi):
                return m
        return None

    # 1. tur: eşleştir ve ESPN hafta no ile sitedeki hafta no arasındaki farkı bul
    eslesmeler = []
    farklar = {}
    for event, espn_hafta in event_hafta_ciftleri:
        if not event.get("id"):
            continue
        ev = (_espn_takim(event, "home").get("team") or {}).get("displayName") or ""
        dep = (_espn_takim(event, "away").get("team") or {}).get("displayName") or ""
        tarih = parse_api_date(event.get("date"))
        mac = eslesen_mac(event, ev, dep, tarih)
        if mac is not None:
            kullanilan_idler.add(mac.id)
            if mac.hafta and str(mac.hafta).isdigit():
                fark = int(mac.hafta) - espn_hafta
                farklar[fark] = farklar.get(fark, 0) + 1
        eslesmeler.append((event, espn_hafta, ev, dep, tarih, mac))
    hafta_farki = max(farklar, key=farklar.get) if farklar else 0

    # 2. tur: yaz
    inserted = updated = skipped = manuel_korunan = 0
    for event, espn_hafta, ev, dep, tarih, mac in eslesmeler:
        ev_c, dep_c = _espn_takim(event, "home"), _espn_takim(event, "away")
        status_type = ((event.get("status") or {}).get("type")) or {}
        yeni = mac is None
        if yeni:
            mac = Mac(external_id=ESPN_EXTERNAL_ID_OFFSET + int(event["id"]))
            db.session.add(mac)
            mac.ev_sahibi = turkce_ad(ev) or "Bilinmeyen Takım"
            mac.deplasman = turkce_ad(dep) or "Bilinmeyen Takım"
            mac.lig = "Süper Lig"
            mac.lig_id = SUPER_LIG_ID
            mac.hafta = str(espn_hafta + hafta_farki)
            inserted += 1
        else:
            mac.external_id = ESPN_EXTERNAL_ID_OFFSET + int(event["id"])
            updated += 1

        mac.sezon = sezon_baslangic_yili
        mac.ev_logo = (ev_c.get("team") or {}).get("logo") or mac.ev_logo
        mac.deplasman_logo = (dep_c.get("team") or {}).get("logo") or mac.deplasman_logo
        if tarih is not None:
            mac.baslangic = tarih
        elle_saat_uygula(mac)

        if mac.api_status == "MANUEL":
            manuel_korunan += 1          # elle girilen sonuç her zaman geçerli
        else:
            yeni_durum = map_espn_status(status_type)
            if not (mac.durum == "bitti" and yeni_durum != "bitti" and not yeni):
                mac.durum = yeni_durum
            mac.api_status = status_type.get("name")
            for alan, c in (("ev_skor", ev_c), ("deplasman_skor", dep_c)):
                skor = c.get("score")
                if skor not in (None, "") and mac.durum in ("bitti", "oynaniyor"):
                    try:
                        setattr(mac, alan, int(skor))
                    except (TypeError, ValueError):
                        pass
        mac.son_guncelleme = utc_now_naive()

    db.session.commit()
    recalculate_all_points()

    return {
        "success": True,
        "lig_kodu": SUPER_LIG_CODE,
        "competition_id": SUPER_LIG_ID,
        "api_matches": len(events),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "manuel_korunan": manuel_korunan,
        "hafta_farki": hafta_farki,
    }


def espn_turkiye_maci_mi(event):
    """ESPN eventinde takımlardan biri Türkiye Milli Takımı mı?"""
    competitions = event.get("competitions") or []
    competition = competitions[0] if competitions else {}
    for c in competition.get("competitors") or []:
        team = c.get("team") or {}
        if (team.get("abbreviation") or "").upper() in NATIONS_LEAGUE_TAKIP_KISALTMALARI:
            return True
        for alan in ("displayName", "name", "shortDisplayName", "location"):
            deger = (team.get(alan) or "").lower()
            if any(anahtar in deger for anahtar in NATIONS_LEAGUE_TAKIP_EDILEN_TAKIMLAR):
                return True
    return False


def sync_nations_league_espn(requested_season=None, aylar=None):
    """
    UEFA Uluslar Ligi'nde SADECE Türkiye'nin maçlarını ESPN'in ücretsiz, key
    gerektirmeyen uç noktasından (slug: uefa.nations) çekip veritabanına yazar.
    Uluslar Ligi sezonu Eylül'de başlayıp ertesi yıl Haziran'da biter; bu
    yüzden Ağustos-Temmuz aylarının hepsi taranır. Diğer takımların
    maçları hiç DB'ye yazılmaz.
    """
    simdi = datetime.now(timezone.utc)
    if requested_season:
        sezon_baslangic_yili = requested_season
    else:
        sezon_baslangic_yili = simdi.year if simdi.month >= 8 else simdi.year - 1

    # ESPN 2026-09'dan beri "dates=BASLANGIC-BITIS" aralık formunu 400 ile
    # reddediyor; tek gün (YYYYMMDD) ve AY (YYYYMM) formları çalışıyor. Bu
    # yüzden sezonu ay ay (Ağustos-Temmuz, 12 istek) çekip event id'ye göre
    # tekilleştiriyoruz. Tek bir ay hata verirse diğerleri devam eder; hepsi
    # hata verirse hata fırlatılır.
    if not aylar:  # belirli aylar verilmediyse tüm sezon
        aylar = [(sezon_baslangic_yili, m) for m in range(8, 13)] + [
            (sezon_baslangic_yili + 1, m) for m in range(1, 8)
        ]
    eventler_by_id = {}
    hatalar = []
    basarili_istek = 0
    for yil, ay in aylar:
        try:
            yanit = espn_get(
                ESPN_NATIONS_LEAGUE_SLUG,
                "scoreboard",
                params={"dates": f"{yil}{ay:02d}", "limit": 500},
            )
            basarili_istek += 1
        except Exception as e:
            hatalar.append(f"{yil}-{ay:02d}: {e}")
            continue
        for e in yanit.get("events", []) or []:
            if e.get("id"):
                eventler_by_id[e["id"]] = e
    if basarili_istek == 0:
        raise RuntimeError("ESPN Uluslar Ligi istekleri başarısız: " + " | ".join(hatalar[:2]))
    tum_eventler = list(eventler_by_id.values())
    turkiye_eventleri = [e for e in tum_eventler if espn_turkiye_maci_mi(e)]

    # Milli maçlar "aralar" halinde oynanır (Eylül, Ekim, Kasım, Mart, Haziran);
    # aynı aradaki maçlar 7 günden az fark ettiği için 7 gün boşluk = yeni "hafta".
    event_hafta_ciftleri = espn_events_to_maclar(turkiye_eventleri, bosluk_gun=7)

    inserted = 0
    updated = 0
    skipped = 0

    for event, hafta_no in event_hafta_ciftleri:
        raw_external_id = event.get("id")
        if not raw_external_id:
            skipped += 1
            continue

        existing = Mac.query.filter_by(
            external_id=ESPN_EXTERNAL_ID_OFFSET + int(raw_external_id)
        ).first()

        result = espn_event_to_db(
            event, hafta_no, sezon_baslangic_yili,
            lig_adi="UEFA Uluslar Ligi", lig_id=NATIONS_LEAGUE_ID,
        )
        if not result:
            skipped += 1
            continue

        if existing:
            updated += 1
        else:
            inserted += 1

    db.session.commit()
    recalculate_all_points()

    return {
        "success": True,
        "lig_kodu": NATIONS_LEAGUE_CODE,
        "competition_id": NATIONS_LEAGUE_ID,
        "api_matches": len(turkiye_eventleri),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped
    }


def espn_avrupa_takip_maci_mi(event):
    """ESPN eventinde takımlardan biri Beşiktaş ya da Trabzonspor mu?"""
    competitions = event.get("competitions") or []
    competition = competitions[0] if competitions else {}
    for c in competition.get("competitors") or []:
        team = c.get("team") or {}
        if (team.get("abbreviation") or "").upper() in AVRUPA_TAKIP_KISALTMALARI:
            return True
        for alan in ("displayName", "name", "shortDisplayName", "location"):
            deger = (team.get(alan) or "").lower()
            if any(anahtar in deger for anahtar in AVRUPA_TAKIP_EDILEN_TAKIMLAR):
                return True
    return False


def _form_maclarini_kaydet(eventler, lig_id):
    """Biten ESPN eventlerini (skoruyla) FormMaci tablosuna yazar/günceller."""
    mevcut = {f.external_id: f for f in FormMaci.query.filter(
        FormMaci.external_id.in_([ESPN_EXTERNAL_ID_OFFSET + int(i) for i in eventler] or [-1])).all()}
    sayi = 0
    for event_id, (event, kupa_adi) in eventler.items():
        comp = (event.get("competitions") or [{}])[0]
        durum = ((comp.get("status") or event.get("status") or {}).get("type") or {})
        if not durum.get("completed"):
            continue
        ev = dep = None
        for c in comp.get("competitors") or []:
            if c.get("homeAway") == "home":
                ev = c
            elif c.get("homeAway") == "away":
                dep = c
        tarih = parse_api_date(event.get("date"))
        if not ev or not dep or tarih is None:
            continue
        try:
            ev_skor, dep_skor = int(ev.get("score")), int(dep.get("score"))
        except (TypeError, ValueError):
            continue
        ev_ad = (ev.get("team") or {}).get("displayName") or "?"
        dep_ad = (dep.get("team") or {}).get("displayName") or "?"
        eid = ESPN_EXTERNAL_ID_OFFSET + int(event_id)
        f = mevcut.get(eid)
        if not f:
            f = FormMaci(external_id=eid)
            db.session.add(f)
        f.lig_id, f.lig, f.baslangic = lig_id, kupa_adi, tarih
        f.ev_sahibi, f.deplasman, f.ev_skor, f.deplasman_skor = ev_ad, dep_ad, ev_skor, dep_skor
        sayi += 1
    db.session.commit()
    return sayi


def sync_avrupa_espn(requested_season=None, aylar=None):
    """
    Avrupa Ligi ve Konferans Ligi'nde SADECE Beşiktaş ve Trabzonspor'un
    maçlarını ESPN'den çekip AVRUPA_ID altında kaydeder. Sezon Temmuz'da
    (ön eleme) başlar, Mayıs'ta biter. Diğer takımların maçları DB'ye yazılmaz.
    "hafta" alanı takvim haftası (YYYY-HH) olarak tutulur ki aynı Perşembe
    oynanan BJK ve TS maçları grup sayfasında birlikte görünsün.
    """
    simdi = datetime.now(timezone.utc)
    sezon_baslangic_yili = requested_season or (simdi.year if simdi.month >= 7 else simdi.year - 1)
    if not aylar:
        aylar = [(sezon_baslangic_yili, m) for m in range(7, 13)] + [
            (sezon_baslangic_yili + 1, m) for m in range(1, 7)
        ]

    eventler = {}   # event id -> (event, kupa adı)
    diger_eventler = {}   # takip edilmeyen maçlar: sadece rakiplerin formu için
    hatalar, basarili_istek = [], 0
    for slug, kupa_adi in ESPN_AVRUPA_SLUGLARI.items():
        for yil, ay in aylar:
            try:
                yanit = espn_get(slug, "scoreboard", params={"dates": f"{yil}{ay:02d}", "limit": 500})
                basarili_istek += 1
            except Exception as e:
                hatalar.append(f"{slug} {yil}-{ay:02d}: {e}")
                continue
            for e in yanit.get("events", []) or []:
                if e.get("id") and espn_avrupa_takip_maci_mi(e):
                    eventler[e["id"]] = (e, kupa_adi)
                elif e.get("id"):
                    diger_eventler[e["id"]] = (e, kupa_adi)
    if basarili_istek == 0:
        raise RuntimeError("ESPN Avrupa kupaları istekleri başarısız: " + " | ".join(hatalar[:2]))

    # Ön eleme turları ayrı ESPN liglerinde; sadece rakiplerin formu için çekilir (hata olursa geçilir)
    for slug, kupa_adi in ESPN_AVRUPA_ELEME_SLUGLARI.items():
        for yil, ay in aylar:
            if ay not in (7, 8):
                continue
            try:
                yanit = espn_get(slug, "scoreboard", params={"dates": f"{yil}{ay:02d}", "limit": 500})
            except Exception:
                continue
            for e in yanit.get("events", []) or []:
                if e.get("id"):
                    diger_eventler[e["id"]] = (e, kupa_adi)

    inserted = updated = skipped = 0
    for event_id, (event, kupa_adi) in eventler.items():
        tarih = parse_api_date(event.get("date"))
        if tarih is None:
            skipped += 1
            continue
        tr = tarih + timedelta(hours=3)
        yil_hafta = tr.isocalendar()
        hafta = f"{yil_hafta[0]}-{yil_hafta[1]:02d}"
        existing = Mac.query.filter_by(external_id=ESPN_EXTERNAL_ID_OFFSET + int(event_id)).first()
        if existing and existing.api_status == "MANUEL":
            skipped += 1
            continue
        if not espn_event_to_db(event, hafta, sezon_baslangic_yili, lig_adi=kupa_adi, lig_id=AVRUPA_ID):
            skipped += 1
            continue
        if existing:
            updated += 1
        else:
            inserted += 1

    db.session.commit()
    try:
        form_sayisi = _form_maclarini_kaydet(diger_eventler, AVRUPA_ID)
    except Exception as e:
        db.session.rollback()
        form_sayisi = 0
        print("[form-maclari] hata:", e)
    recalculate_all_points()
    return {
        "success": True,
        "form_maclari": form_sayisi,
        "lig_kodu": AVRUPA_CODE,
        "competition_id": AVRUPA_ID,
        "api_matches": len(eventler),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def api_football_get(endpoint, params=None):
    if not API_FOOTBALL_KEY:
        raise RuntimeError("API_FOOTBALL_KEY bulunamadı.")
    url = API_FOOTBALL_BASE_URL.rstrip("/") + "/" + endpoint.lstrip("/")
    headers = {"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"}
    response = requests.get(url, headers=headers, params=params or {}, timeout=API_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"API-Football Hatası {response.status_code}: {response.text[:500]}")
    data = response.json()
    # API-Football, HTTP 200 dönse bile hata/limit aşımı mesajlarını
    # "errors" alanında verebilir (örn. günlük istek limiti dolduysa).
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"API-Football Hatası: {errors}")
    return data


def map_api_football_status(short_status):
    mapping = {
        "FT": "bitti",
        "AET": "bitti",
        "PEN": "bitti",
        "CANC": "iptal",
        "ABD": "iptal",
        "PST": "ertelendi",
        "SUSP": "ertelendi",
        "INT": "ertelendi",
        "1H": "oynaniyor",
        "2H": "oynaniyor",
        "HT": "oynaniyor",
        "ET": "oynaniyor",
        "LIVE": "oynaniyor",
        "NS": "planlandi",
        "TBD": "planlandi",
    }
    return mapping.get(short_status, "planlandi")


def api_football_fixture_to_db(fixture):
    """
    API-Football'un /fixtures cevabındaki tek bir eleman şu şekildedir:
    { "fixture": {...}, "league": {...}, "teams": {...}, "goals": {...} }
    football-data.org'unkinden tamamen farklı bir şema olduğu için ayrı bir
    dönüştürücü kullanıyoruz. lig_id her zaman bizim dahili SUPER_LIG_ID'imiz
    olarak sabitlenir (API-Football'un kendi lig id'si sadece istek atarken
    kullanılır, DB'de saklanmaz).
    """
    fixture_info = fixture.get("fixture") or {}
    raw_external_id = fixture_info.get("id")
    if not raw_external_id:
        return None

    # Çakışmayı önlemek için ofsetli, ayırt edici bir external_id kullanıyoruz.
    external_id = API_FOOTBALL_EXTERNAL_ID_OFFSET + int(raw_external_id)

    teams = fixture.get("teams") or {}
    home_team = teams.get("home") or {}
    away_team = teams.get("away") or {}
    league_info = fixture.get("league") or {}
    goals = fixture.get("goals") or {}
    status = fixture_info.get("status") or {}

    mac = Mac.query.filter_by(external_id=external_id).first()
    if not mac:
        mac = Mac(external_id=external_id)
        db.session.add(mac)

    mac.ev_sahibi = home_team.get("name") or "Bilinmeyen Takım"
    mac.deplasman = away_team.get("name") or "Bilinmeyen Takım"
    mac.ev_logo = home_team.get("logo")
    mac.deplasman_logo = away_team.get("logo")
    mac.lig = "Süper Lig"
    mac.lig_id = SUPER_LIG_ID
    mac.sezon = league_info.get("season")
    mac.hafta = (league_info.get("round") or "").replace("Regular Season - ", "") or None
    mac.baslangic = parse_api_date(fixture_info.get("date"))
    elle_saat_uygula(mac)
    mac.api_status = status.get("short")
    mac.durum = map_api_football_status(status.get("short"))

    home_score = goals.get("home")
    away_score = goals.get("away")
    if home_score is not None:
        mac.ev_skor = home_score
    if away_score is not None:
        mac.deplasman_skor = away_score

    mac.son_guncelleme = utc_now_naive()
    return mac


def sync_super_lig_api_football(requested_season=None):
    """
    Süper Lig maçlarını API-Football'dan çekip veritabanına yazar.
    football-data.org'un ücretsiz planı TR1'i desteklemediği için Süper Lig
    burada ayrı bir sağlayıcı üzerinden senkronize edilir.
    """
    if not API_FOOTBALL_KEY:
        raise RuntimeError("API_FOOTBALL_KEY bulunamadı.")

    season_to_use = requested_season or datetime.now(timezone.utc).year

    response = api_football_get(
        "fixtures",
        params={"league": API_FOOTBALL_SUPER_LIG_ID, "season": season_to_use},
    )
    fixtures = response.get("response", [])

    inserted = 0
    updated = 0
    skipped = 0

    for fixture in fixtures:
        fixture_info = fixture.get("fixture") or {}
        raw_external_id = fixture_info.get("id")
        if not raw_external_id:
            skipped += 1
            continue

        existing = Mac.query.filter_by(
            external_id=API_FOOTBALL_EXTERNAL_ID_OFFSET + int(raw_external_id)
        ).first()

        result = api_football_fixture_to_db(fixture)
        if not result:
            skipped += 1
            continue

        if existing:
            updated += 1
        else:
            inserted += 1

    db.session.commit()
    recalculate_all_points()

    return {
        "success": True,
        "lig_kodu": SUPER_LIG_CODE,
        "competition_id": SUPER_LIG_ID,
        "api_matches": len(fixtures),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped
    }


def sync_champions_league(requested_season=None):
    """Geriye dönük uyumluluk için: sadece Şampiyonlar Ligi'ni senkronize eder."""
    return sync_league(CHAMPIONS_LEAGUE_CODE, requested_season=requested_season)


def sync_all_leagues():
    """
    LEAGUES sözlüğündeki tüm ligleri (şu an: Şampiyonlar Ligi + Süper Lig)
    tek tek senkronize eder. Bir ligde hata olursa (örn. Süper Lig ücretsiz
    planda desteklenmiyorsa) diğer ligi engellemez, hatayı sonuçta bildirir.
    """
    sonuclar = {}
    for kod in LEAGUES:
        try:
            sonuclar[kod] = sync_league(kod)
        except Exception as e:
            db.session.rollback()
            sonuclar[kod] = {"success": False, "error": str(e)}
    return sonuclar


TAM_SKOR_PUANI = 5
SONUC_PUANI = 3
# Tek bilen kuralı: 20 Ekim 2026 12:00 (TR) ve sonrasında başlayan maçlarda, bir grupta skoru
# Robot Ziya hariç yalnızca bir kişi tam bilirse o kişi 5 yerine 10 puan alır.
TEK_BILEN_PUANI = 10
TEK_BILEN_BASLANGIC_UTC = datetime(2026, 10, 20, 9, 0)    # 20.10.2026 12:00 Türkiye saati
# Tek sonuç bilen kuralı: bir grupta maçın sonucunu (kazanan / beraberlik) Robot Ziya hariç
# yalnızca bir kişi bildiyse ve skoru tam tutmadıysa 3 yerine 8 puan alır.
TEK_SONUC_PUANI = 8
TEK_SONUC_BASLANGIC_UTC = datetime(2026, 10, 20, 16, 0)   # 20.10.2026 19:00 Türkiye saati
# Beraberlik kuralı (anket sonucu): beraberliği bilene 3 yerine 4, beraberliği skoruyla
# tam bilene 5 yerine 6 puan. 21.10.2026 18:00 (TR) sonrası başlayan maçlarda.
BERABERLIK_SONUC_PUANI = 4
BERABERLIK_TAM_PUANI = 6
BERABERLIK_BASLANGIC_UTC = datetime(2026, 10, 21, 15, 0)   # 21.10.2026 18:00 Türkiye saati
# Tahmin girmeme cezası: bu tarihten sonra başlayan ve biten maçlarda tahmini olmayan
# üyeden (Robot Ziya hariç, maçtan önce gruba katılmış olanlar) 1 puan düşülür.
TAHMIN_YOK_CEZASI = -1
TAHMIN_YOK_CEZA_BASLANGIC_UTC = datetime(2026, 10, 20, 9, 0)   # 20.10.2026 12:00 Türkiye saati


def calculate_points(prediction_home, prediction_away, actual_home, actual_away):
    if actual_home is None or actual_away is None:
        return 0
    if prediction_home == actual_home and prediction_away == actual_away:
        return 5

    prediction_result = 1 if prediction_home > prediction_away else (-1 if prediction_home < prediction_away else 0)
    actual_result = 1 if actual_home > actual_away else (-1 if actual_home < actual_away else 0)

    if prediction_result == actual_result:
        return 3
    return 0


# Maç başladıktan en geç bu kadar dakika sonra, API hâlâ "bitti" demese de
# skor gelmişse maç bitmiş sayılır ve puanlar hesaplanır. Sonradan API skoru
# düzeltirse (geç gol vb.) bir sonraki senkronda puanlar kendiliğinden düzelir.
PUANLAMA_EN_GEC_DAKIKA = 120


def _gecikmis_maci_bitir(mac, simdi=None):
    simdi = simdi or utc_now_naive()
    if (mac.durum in ("planlandi", "oynaniyor") and mac.api_status != "MANUEL"
            and mac.ev_skor is not None and mac.deplasman_skor is not None
            and mac.baslangic and simdi >= mac.baslangic + timedelta(minutes=PUANLAMA_EN_GEC_DAKIKA)):
        mac.durum = "bitti"
        return True
    return False


def gecikmis_maclari_bitir():
    """Skoru gelmiş, başlamasının üzerinden PUANLAMA_EN_GEC_DAKIKA geçmiş maçları bitti say."""
    simdi = utc_now_naive()
    adaylar = Mac.query.filter(
        Mac.durum.in_(["planlandi", "oynaniyor"]),
        Mac.ev_skor.isnot(None), Mac.deplasman_skor.isnot(None),
        Mac.baslangic.isnot(None),
        Mac.baslangic <= simdi - timedelta(minutes=PUANLAMA_EN_GEC_DAKIKA),
    ).all()
    degisti = [m for m in adaylar if _gecikmis_maci_bitir(m, simdi)]
    if degisti:
        db.session.commit()
    return len(degisti)


def recalculate_all_points():
    gecikmis_maclari_bitir()
    sistem = Sistem.query.first()
    sifirlama_tarihi = sistem.puan_sifirlama_tarihi if sistem else None
    muaf_mac_id_seti = {m.mac_id for m in MacMuafiyet.query.all()}

    predictions = Tahmin.query.all()
    changed = False
    yeni_puanlar, tam_bilenler, sonuc_bilenler = {}, {}, {}
    tam_kategori, sonuc_kategori = set(), set()
    for tahmin in predictions:
        mac = tahmin.mac
        if not mac or not mac.bitti_mi() or mac.ev_skor is None or mac.deplasman_skor is None:
            continue

        # "Puanları sıfırla" butonuna basıldıktan ÖNCE (ya da tam o anda)
        # oynanmış maçlar için puan hep 0 kalmalı — yoksa yeni bir maç
        # sonucu girildiğinde bu fonksiyon tekrar çalışıp eski/sıfırlanmış
        # sezonun puanlarını geri getiriyordu. Ancak admin bir maçı MUAF
        # olarak işaretlediyse (mac_muaf_mi), bu kurala takılmadan her
        # zaman normal şekilde puanlanır.
        mac_tarihi = mac.baslangic or mac.son_guncelleme
        muaf_mi = mac.id in muaf_mac_id_seti
        if sifirlama_tarihi and mac_tarihi and mac_tarihi <= sifirlama_tarihi and not muaf_mi:
            new_points = 0
        else:
            new_points = calculate_points(tahmin.ev_skor, tahmin.deplasman_skor, mac.ev_skor, mac.deplasman_skor)

        tam_mi = new_points == TAM_SKOR_PUANI
        sonuc_mu = new_points == SONUC_PUANI
        # Beraberlik maçlarında temel puan 5→6 (tam) ve 3→4 (sonuç)
        if (mac.ev_skor == mac.deplasman_skor and mac.baslangic and mac.baslangic >= BERABERLIK_BASLANGIC_UTC):
            if tam_mi:
                new_points = BERABERLIK_TAM_PUANI
            elif sonuc_mu:
                new_points = BERABERLIK_SONUC_PUANI
        yeni_puanlar[tahmin.id] = new_points
        if tam_mi:
            tam_kategori.add(tahmin.id)
        if sonuc_mu:
            sonuc_kategori.add(tahmin.id)
        if tam_mi and mac.baslangic and mac.baslangic >= TEK_BILEN_BASLANGIC_UTC:
            tam_bilenler.setdefault((tahmin.grup_id, tahmin.mac_id), []).append(tahmin)
        if (tam_mi or sonuc_mu) and mac.baslangic and mac.baslangic >= TEK_SONUC_BASLANGIC_UTC:
            sonuc_bilenler.setdefault((tahmin.grup_id, tahmin.mac_id), []).append(tahmin)

    # Tek bilen kuralı: bir grupta skoru Robot Ziya dışında yalnızca bir kişi tam bildiyse 10 puan
    yz = yz_kullanici_getir(olustur=False)
    yz_id = yz.id if yz else None
    for liste in tam_bilenler.values():
        insanlar = [t for t in liste if t.kullanici_id != yz_id]
        if len(insanlar) == 1:
            yeni_puanlar[insanlar[0].id] = TEK_BILEN_PUANI
    # Tek sonuç bilen: sonucu (tam skor dahil) Ziya hariç tek kişi bildiyse ve o kişi
    # skoru tam tutmadıysa 8 puan (tam tutturduysa zaten yukarıda 10 aldı)
    for liste in sonuc_bilenler.values():
        insanlar = [t for t in liste if t.kullanici_id != yz_id]
        if len(insanlar) == 1 and insanlar[0].id in sonuc_kategori:
            yeni_puanlar[insanlar[0].id] = TEK_SONUC_PUANI

    for tahmin in predictions:
        if tahmin.id in yeni_puanlar and tahmin.puan != yeni_puanlar[tahmin.id]:
            tahmin.puan = yeni_puanlar[tahmin.id]
            changed = True
    if changed:
        db.session.commit()
    try:
        tahmin_cezalarini_guncelle(predictions, yz_id)
    except Exception as e:
        db.session.rollback()
        print("[ceza] hata:", e)


def tahmin_cezalarini_guncelle(tahminler=None, yz_id=None):
    """Biten maçlarda tahmin girmeyen üyelere -1 ceza yazar; sonradan tahmini
    bulunan (ya da maçı artık bitmiş sayılmayan) kayıtların cezasını kaldırır."""
    maclar = Mac.query.filter(
        Mac.durum == "bitti", Mac.ev_skor.isnot(None), Mac.deplasman_skor.isnot(None),
        Mac.baslangic.isnot(None), Mac.baslangic >= TAHMIN_YOK_CEZA_BASLANGIC_UTC,
        db.or_(db.and_(Mac.lig_id.in_(gosterilecek_lig_id_listesi()), super_lig_takim_filtresi()),
               Mac.ozel_grup_id.isnot(None)),
    ).all()
    mac_map = {m.id: m for m in maclar}
    if tahminler is None:
        tahminler = Tahmin.query.filter(Tahmin.mac_id.in_(list(mac_map) or [-1])).all()
    var_olan = {(t.grup_id, t.mac_id, t.kullanici_id) for t in tahminler if t.mac_id in mac_map}
    gerekli = set()
    if mac_map:
        uyelikler = GrupUyeligi.query.all()
        for m in maclar:
            for u in uyelikler:
                if u.kullanici_id == yz_id:
                    continue
                if m.ozel_grup_id and u.grup_id != m.ozel_grup_id:
                    continue   # özel maç sadece kendi grubunu ilgilendirir
                if u.katilma_tarihi and u.katilma_tarihi > m.baslangic:
                    continue   # maçtan sonra katılan cezalandırılmaz
                if (u.grup_id, m.id, u.kullanici_id) not in var_olan:
                    gerekli.add((u.grup_id, m.id, u.kullanici_id))
    mevcut = {(c.grup_id, c.mac_id, c.kullanici_id): c for c in TahminCezasi.query.all()}
    degisti = False
    for anahtar in gerekli - set(mevcut):
        g, mid, kid = anahtar
        db.session.add(TahminCezasi(grup_id=g, mac_id=mid, kullanici_id=kid, puan=TAHMIN_YOK_CEZASI))
        degisti = True
    for anahtar, c in mevcut.items():
        if anahtar not in gerekli:
            db.session.delete(c)
            degisti = True
    if degisti:
        db.session.commit()


from flask import g as _g


def ceza_kumesi(grup_id):
    """Şablonlar için: bu gruptaki (mac_id, kullanici_id) ceza çiftleri (istek başına bir kez)."""
    onbellek = getattr(_g, "ceza_kumeleri", None)
    if onbellek is None:
        onbellek = _g.ceza_kumeleri = {}
    if grup_id not in onbellek:
        try:
            onbellek[grup_id] = {(m, k) for m, k in db.session.query(TahminCezasi.mac_id, TahminCezasi.kullanici_id)
                                 .filter(TahminCezasi.grup_id == grup_id).all()}
        except Exception:
            db.session.rollback()
            onbellek[grup_id] = set()
    return onbellek[grup_id]


def ceza_var(grup_id, mac_id, kullanici_id):
    return (mac_id, kullanici_id) in ceza_kumesi(grup_id)


app.jinja_env.globals["ceza_var"] = ceza_var


def kullanici_ceza_maclari(kullanici_id):
    """Kullanıcının herhangi bir grubunda ceza yediği maç id'leri (istek başına bir kez)."""
    onbellek = getattr(_g, "kullanici_ceza_maclari", None)
    if onbellek is None:
        onbellek = _g.kullanici_ceza_maclari = {}
    if kullanici_id not in onbellek:
        try:
            onbellek[kullanici_id] = {m for (m,) in db.session.query(TahminCezasi.mac_id)
                                      .filter(TahminCezasi.kullanici_id == kullanici_id).distinct()}
        except Exception:
            db.session.rollback()
            onbellek[kullanici_id] = set()
    return onbellek[kullanici_id]


app.jinja_env.globals["kullanici_ceza_maclari"] = kullanici_ceza_maclari


def ceza_toplamlari(grup_id=None, kullanici_id=None, bas=None, son=None, sadece_biten=False):
    """{kullanici_id: toplam ceza} — puan toplamlarına eklenir."""
    q = db.session.query(TahminCezasi.kullanici_id, db.func.coalesce(db.func.sum(TahminCezasi.puan), 0))
    if bas is not None or son is not None:
        q = q.join(Mac, Mac.id == TahminCezasi.mac_id)
        if bas is not None:
            q = q.filter(Mac.baslangic >= bas)
        if son is not None:
            q = q.filter(Mac.baslangic < son)
    if grup_id is not None:
        q = q.filter(TahminCezasi.grup_id == grup_id)
    if kullanici_id is not None:
        q = q.filter(TahminCezasi.kullanici_id == kullanici_id)
    try:
        return {k: int(v) for k, v in q.group_by(TahminCezasi.kullanici_id).all()}
    except Exception:
        db.session.rollback()
        return {}


# ============================================================
# DATABASE INITIALIZATION (Auto-run safely for Vercel)
# ============================================================

def initialize_database():
    with app.app_context():
        db.create_all()
        # Sonradan eklenen kolonlar (create_all var olan tabloya kolon eklemez)
        try:
            kolonlar = {k["name"] for k in db.inspect(db.engine).get_columns("mesajlar")}
            if "yanit_id" not in kolonlar:
                db.session.execute(db.text("ALTER TABLE mesajlar ADD COLUMN yanit_id INTEGER"))
                db.session.commit()
        except Exception:
            db.session.rollback()
        try:
            kolonlar = {k["name"] for k in db.inspect(db.engine).get_columns("ziyaret_gunluk")}
            if "tarayici" not in kolonlar:
                db.session.execute(db.text("ALTER TABLE ziyaret_gunluk ADD COLUMN tarayici VARCHAR(40)"))
                # İlk sayaç sürümü aynı kişiyi IP değişince tekrar sayıyordu; o veriler atılır
                db.session.execute(db.text("DELETE FROM ziyaret_gunluk"))
                db.session.execute(db.text("DELETE FROM sayfa_gunluk"))
                db.session.commit()
        except Exception:
            db.session.rollback()
        try:
            kolonlar = {k["name"] for k in db.inspect(db.engine).get_columns("kullanicilar")}
            mac_kolonlari = {k["name"] for k in db.inspect(db.engine).get_columns("maclar")}
            if "ozel_grup_id" not in mac_kolonlari:
                db.session.execute(db.text("ALTER TABLE maclar ADD COLUMN ozel_grup_id INTEGER"))
                db.session.execute(db.text("CREATE INDEX IF NOT EXISTS ix_maclar_ozel_grup_id ON maclar (ozel_grup_id)"))
                db.session.commit()
            if "sifre_degismeli" not in kolonlar:
                db.session.execute(db.text(
                    "ALTER TABLE kullanicilar ADD COLUMN sifre_degismeli BOOLEAN NOT NULL DEFAULT FALSE"))
                db.session.commit()
        except Exception:
            db.session.rollback()
        if db.engine.dialect.name == "postgresql":
            try:
                db.session.execute(db.text("ALTER TABLE mesajlar ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE sohbet_okumalari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE senkron_kayitlari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE yz_analizleri ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE haberler ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE sifre_talepleri ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE geri_bildirimler ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE tahmin_cezalari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE anketler ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE form_maclari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE destek_konusmalari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE destek_mesajlari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE kelime_oyunlari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE anket_oylari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE ziyaret_gunluk ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE sayfa_gunluk ENABLE ROW LEVEL SECURITY"))
                db.session.commit()
            except Exception:
                db.session.rollback()
        admin_username = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "Admin123!")

        admin_user = Kullanici.query.filter_by(kullanici_adi=admin_username).first()
        if not admin_user:
            admin_user = Kullanici(kullanici_adi=admin_username, admin=True)
            admin_user.set_password(admin_password)
            db.session.add(admin_user)
            db.session.commit()
        elif not admin_user.admin:
            admin_user.admin = True
            db.session.commit()

# Her soğuk başlangıçta (cold start) veritabanı tablolarının var olduğundan emin olalım
initialize_database()


# ============================================================
# ROUTES (Rotalar)
# ============================================================

# Kendi domain'in (örn. skorbilen.com) çalışınca Render'da ANA_DOMAIN=skorbilen.com
# tanımlanırsa, eski *.onrender.com adresine gelenler kalıcı olarak yeni adrese
# yönlendirilir. Cron adresi (/cron/...) yönlendirilmez ki zamanlayıcı bozulmasın.
ANA_DOMAIN = os.getenv("ANA_DOMAIN", "").strip().lower()


@app.before_request
def ana_domaine_yonlendir():
    if not ANA_DOMAIN:
        return None
    host = (request.host or "").split(":")[0].lower()
    if host.endswith(".onrender.com") and not request.path.startswith("/cron"):
        hedef = f"https://{ANA_DOMAIN}{request.full_path if request.query_string else request.path}"
        return redirect(hedef, code=301)
    return None


@app.route("/google84ae3ba8f3a9de04.html")
def google_site_dogrulama():
    """Google Search Console sahiplik doğrulama dosyası (HTML dosyası yöntemi)."""
    return Response("google-site-verification: google84ae3ba8f3a9de04.html", mimetype="text/html")


@app.route("/ads.txt")
def ads_txt():
    """Google AdSense siteyi onaylamak için bu dosyayı domain kökünde arar.
    Yayıncı ID'si (pub-3716423648799781) AdSense hesabına ait olduğu için
    bu satır sabit — AdSense panelinde farklı bir satır gösteriliyorsa
    (nadiren olur) onu buraya birebir kopyalamak yeterli."""
    return Response(
        "google.com, pub-3716423648799781, DIRECT, f08c47fec0942fa0",
        mimetype="text/plain",
    )


@app.route("/")
def index():
    """Girişli kullanıcı panele gider; girişsiz ziyaretçi herkese açık tanıtım sayfasını görür."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    try:
        ozet = lig_puan_tablosu_hesapla(SUPER_LIG_ID)[:6]
    except Exception:
        db.session.rollback()
        ozet = []
    now = utc_now_naive()
    yaklasan = Mac.query.filter(
        Mac.lig_id.in_(gosterilecek_lig_id_listesi()), Mac.baslangic.isnot(None), Mac.baslangic > now,
        ~Mac.durum.in_(["bitti", "iptal", "ertelendi"]),
    ).order_by(Mac.baslangic.asc()).limit(5).all()
    return render_template("anasayfa.html", ozet=ozet, yaklasan=yaklasan, haberler=son_haberler())


# ------------------------------------------------------------
# HERKESE AÇIK BİLGİ SAYFALARI
# ------------------------------------------------------------
ILETISIM_EPOSTA = os.getenv("ILETISIM_EPOSTA", "").strip()


@app.route("/nasil-oynanir")
def nasil_oynanir():
    return render_template("nasil_oynanir.html")


@app.route("/gizlilik")
def gizlilik():
    return render_template("gizlilik.html", iletisim_eposta=ILETISIM_EPOSTA)


@app.route("/iletisim")
def iletisim():
    return render_template("iletisim.html", iletisim_eposta=ILETISIM_EPOSTA)


@app.route("/robots.txt")
def robots_txt():
    satirlar = [
        "User-agent: *",
        "Allow: /$", "Allow: /kelime", "Allow: /tlogo/", "Allow: /robot-ziya-tahminleri", "Allow: /puan-durumu", "Allow: /nasil-oynanir", "Allow: /gizlilik", "Allow: /iletisim",
        "Disallow: /admin", "Disallow: /cron", "Disallow: /sifremi-unuttum", "Disallow: /geri-bildirim", "Disallow: /destek", "Disallow: /paylas", "Disallow: /z", "Disallow: /grup", "Disallow: /sohbet", "Disallow: /api",
        "", f"Sitemap: {request.url_root.rstrip('/')}/sitemap.xml",
    ]
    return Response("\n".join(satirlar) + "\n", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    kok = request.url_root.rstrip("/")
    sayfalar = ["/", "/kelime", "/robot-ziya-tahminleri", "/puan-durumu", "/nasil-oynanir", "/gizlilik", "/iletisim"]
    govde = "".join(f"<url><loc>{kok}{s}</loc></url>" for s in sayfalar)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{govde}</urlset>'
    return Response(xml, mimetype="application/xml")


@app.route("/kayit", methods=["GET", "POST"])
def kayit():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    next_url = request.args.get("next") or request.form.get("next") or ""
    if request.method == "POST":
        kullanici_adi = request.form.get("kullanici_adi", "").strip()
        sifre = request.form.get("sifre", "")
        sifre_tekrar = request.form.get("sifre_tekrar", "")

        if not kullanici_adi or len(sifre) < 6 or sifre != sifre_tekrar:
            flash("Form bilgilerini kontrol edin (Şifre en az 6 karakter olmalı ve uyuşmalı).", "danger")
            return redirect(url_for("kayit", next=next_url) if next_url else url_for("kayit"))

        if _yz_adi_rezerve_mi(kullanici_adi) or Kullanici.query.filter_by(kullanici_adi=kullanici_adi).first():
            flash("Bu kullanıcı adı zaten kullanılıyor.", "danger")
            return redirect(url_for("kayit", next=next_url) if next_url else url_for("kayit"))

        user = Kullanici(kullanici_adi=kullanici_adi)
        user.set_password(sifre)
        db.session.add(user)
        db.session.commit()
        flash("Kayıt başarılı. Şimdi giriş yapabilirsiniz.", "success")
        return redirect(url_for("giris", next=next_url) if next_url else url_for("giris"))
    return render_template("kayit.html", next_url=next_url)


@app.route("/giris", methods=["GET", "POST"])
def giris():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        kullanici_adi = request.form.get("kullanici_adi", "").strip()
        sifre = request.form.get("sifre", "")
        user = Kullanici.query.filter_by(kullanici_adi=kullanici_adi).first()
        if user and not yz_mi(user) and user.check_password(sifre):
            login_user(user)
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Kullanıcı adı veya şifre hatalı.", "danger")
    return render_template("giris.html")


# ------------------------------------------------------------
# Şifremi unuttum: talep yöneticiye gider, onaylanınca kullanıcı
# talebi açtığı sayfadan yeni şifresini belirler.
# ------------------------------------------------------------
SIFRE_TALEBI_GECERLILIK = timedelta(days=3)       # bekleyen talep bu sürede düşer
SIFRE_ONAY_GECERLILIK = timedelta(hours=24)       # onaydan sonra şifre belirleme süresi


def _anahtar_hash(anahtar):
    return hashlib.sha256((anahtar or "").encode()).hexdigest()


def _sifre_talebi_bul(anahtar):
    if not anahtar or len(anahtar) > 100:
        return None
    return SifreTalebi.query.filter_by(anahtar_hash=_anahtar_hash(anahtar)).first()


def _talep_suresi_doldu_mu(t):
    simdi = datetime.utcnow()
    if t.durum == "bekliyor":
        return t.olusturulma and simdi - t.olusturulma > SIFRE_TALEBI_GECERLILIK
    if t.durum == "onaylandi":
        return t.karar_zamani and simdi - t.karar_zamani > SIFRE_ONAY_GECERLILIK
    return False


def bekleyen_sifre_talepleri():
    sinir = datetime.utcnow() - SIFRE_TALEBI_GECERLILIK
    return (SifreTalebi.query.options(db.joinedload(SifreTalebi.kullanici))
            .filter(SifreTalebi.durum == "bekliyor", SifreTalebi.olusturulma > sinir)
            .order_by(SifreTalebi.olusturulma.asc()).all())


@app.route("/sifremi-unuttum", methods=["GET", "POST"])
def sifremi_unuttum():
    if current_user.is_authenticated:
        return redirect(url_for("sifre_degistir"))
    # Bu tarayıcıda açık bir talep varsa doğrudan durumuna götür
    if request.method == "GET" and session.get("sifre_talebi"):
        t = _sifre_talebi_bul(session["sifre_talebi"])
        if t and t.durum in ("bekliyor", "onaylandi") and not _talep_suresi_doldu_mu(t):
            return redirect(url_for("sifre_talebi_durum", anahtar=session["sifre_talebi"]))
    if request.method == "POST":
        kullanici_adi = request.form.get("kullanici_adi", "").strip()
        notu = request.form.get("notu", "").strip()[:300]
        user = Kullanici.query.filter(db.func.lower(Kullanici.kullanici_adi) == kullanici_adi.lower()).first() if kullanici_adi else None
        if not user or yz_mi(user):
            flash("Bu kullanıcı adıyla bir hesap bulunamadı. Büyük/küçük harfe dikkat ederek tekrar dene.", "danger")
            return render_template("sifremi_unuttum.html", kullanici_adi=kullanici_adi, notu=notu)
        # Aynı kullanıcının önceki bekleyen talepleri iptal; en fazla bir açık talep
        son_saat = SifreTalebi.query.filter(SifreTalebi.kullanici_id == user.id,
                                            SifreTalebi.olusturulma > datetime.utcnow() - timedelta(hours=1)).count()
        if son_saat >= 3:
            flash("Kısa sürede çok fazla talep gönderildi. Biraz sonra tekrar dene.", "danger")
            return render_template("sifremi_unuttum.html", kullanici_adi=kullanici_adi, notu=notu)
        SifreTalebi.query.filter(SifreTalebi.kullanici_id == user.id,
                                 SifreTalebi.durum.in_(["bekliyor", "onaylandi"])).update(
            {"durum": "iptal"}, synchronize_session=False)
        import secrets
        anahtar = secrets.token_urlsafe(24)
        db.session.add(SifreTalebi(kullanici_id=user.id, anahtar_hash=_anahtar_hash(anahtar), notu=notu or None))
        db.session.commit()
        session["sifre_talebi"] = anahtar
        session.permanent = True
        return redirect(url_for("sifre_talebi_durum", anahtar=anahtar))
    return render_template("sifremi_unuttum.html", kullanici_adi="", notu="")


@app.route("/sifremi-unuttum/<anahtar>", methods=["GET", "POST"])
def sifre_talebi_durum(anahtar):
    t = _sifre_talebi_bul(anahtar)
    if not t or t.durum == "iptal":
        flash("Bu şifre talebi bulunamadı veya yerine yenisi açıldı.", "danger")
        return redirect(url_for("sifremi_unuttum"))
    if _talep_suresi_doldu_mu(t):
        durum = "suresi_doldu"
    else:
        durum = t.durum
    if request.method == "POST" and durum == "onaylandi":
        yeni = request.form.get("yeni_sifre", "")
        tekrar = request.form.get("yeni_sifre_tekrar", "")
        if len(yeni) < 6:
            flash("Yeni şifre en az 6 karakter olmalı.", "danger")
        elif yeni != tekrar:
            flash("Şifreler birbirini tutmuyor.", "danger")
        else:
            user = t.kullanici
            user.set_password(yeni)
            user.sifre_degismeli = False
            t.durum = "kullanildi"
            db.session.commit()
            session.pop("sifre_talebi", None)
            login_user(user)
            flash("Yeni şifren kaydedildi, giriş yaptın. 🎉", "success")
            return redirect(url_for("dashboard"))
    if request.headers.get("Accept") == "application/json":
        return jsonify({"durum": durum})
    return render_template("sifre_talebi_durum.html", talep=t, durum=durum, anahtar=anahtar)


@app.route("/admin/sifre-talebi/<int:talep_id>/<karar>", methods=["POST"])
@admin_required
def admin_sifre_talebi_karar(talep_id, karar):
    t = db.session.get(SifreTalebi, talep_id)
    if not t or t.durum != "bekliyor" or karar not in ("onayla", "reddet"):
        flash("Talep bulunamadı ya da zaten karara bağlanmış.", "warning")
        return redirect(url_for("admin"))
    t.durum = "onaylandi" if karar == "onayla" else "reddedildi"
    t.karar_zamani = datetime.utcnow()
    db.session.commit()
    ad = t.kullanici.kullanici_adi if t.kullanici else "?"
    if karar == "onayla":
        flash(f"'{ad}' için talep onaylandı. Kullanıcı talep sayfasından 24 saat içinde yeni şifresini belirleyebilir.", "success")
    else:
        flash(f"'{ad}' için talep reddedildi.", "success")
    return redirect(url_for("admin"))


@app.context_processor
def admin_sifre_rozeti():
    """Admin menüsündeki rozet: bekleyen şifre talepleri + okunmamış geri bildirimler."""
    if not current_user.is_authenticated or not getattr(current_user, "admin", False):
        return {}
    try:
        sifre = len(bekleyen_sifre_talepleri())
        gb = GeriBildirim.query.filter_by(okundu=False).count()
        destek = DestekKonusma.query.filter_by(admin_okumadi=True).count()
        return {"bekleyen_sifre_sayisi": sifre, "okunmamis_geri_bildirim": gb, "okunmamis_destek": destek,
                "admin_rozet": sifre + gb + destek}
    except Exception:
        db.session.rollback()
        return {"bekleyen_sifre_sayisi": 0, "okunmamis_geri_bildirim": 0, "okunmamis_destek": 0, "admin_rozet": 0}


GERI_BILDIRIM_TURLERI = {"oneri": "💡 Fikir / öneri", "hata": "🐞 Hata", "diger": "💬 Diğer"}


@app.route("/geri-bildirim", methods=["GET", "POST"])
def geri_bildirim():
    if request.method == "POST":
        metin = request.form.get("metin", "").strip()[:2000]
        tur = request.form.get("tur", "oneri")
        tur = tur if tur in GERI_BILDIRIM_TURLERI else "diger"
        ad = request.form.get("ad", "").strip()[:80] or None
        sayfa = request.form.get("sayfa", "")[:200] or None
        # Bot tuzağı: gerçek kullanıcı bu gizli alanı boş bırakır
        if request.form.get("web_sitesi"):
            return redirect(url_for("geri_bildirim", tamam=1))
        if len(metin) < 3:
            flash("Biraz daha yazar mısın? 🙂", "warning")
            return render_template("geri_bildirim.html", turler=GERI_BILDIRIM_TURLERI, metin=metin, secili_tur=tur, ad=ad or "", sayfa=sayfa or "")
        # Basit sınır: oturum başına 10 dakikada en fazla 5 mesaj
        simdi = utc_now_naive().timestamp()
        gecmis = [t for t in session.get("gb_zamanlar", []) if simdi - t < 600]
        if len(gecmis) >= 5:
            flash("Kısa sürede çok mesaj gönderdin, biraz sonra tekrar dene.", "warning")
            return redirect(url_for("geri_bildirim"))
        session["gb_zamanlar"] = gecmis + [simdi]
        db.session.add(GeriBildirim(
            kullanici_id=current_user.id if current_user.is_authenticated else None,
            ad=None if current_user.is_authenticated else ad,
            tur=tur, metin=metin, sayfa=sayfa))
        db.session.commit()
        return redirect(url_for("geri_bildirim", tamam=1))
    onceki = request.args.get("sayfa") or ""
    if not onceki and request.referrer:
        from urllib.parse import urlparse
        r = urlparse(request.referrer)
        if r.netloc == request.host:
            onceki = r.path
    return render_template("geri_bildirim.html", turler=GERI_BILDIRIM_TURLERI, metin="",
                           secili_tur=request.args.get("tur", "oneri"), ad="", sayfa=onceki,
                           tamam=request.args.get("tamam") == "1")


# ------------------------------------------------------------
# Ziyaretçi sayacı (admin için). Harici servis ve çerez yok.
# Sadece başarılı HTML sayfa açılışları sayılır; botlar, statik dosyalar,
# cron, admin sayfaları ve sohbet yoklamaları sayılmaz.
# ------------------------------------------------------------
import re as _re

_BOT_RE = _re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|telegram|discord|"
                      r"curl|wget|python|go-http|java/|okhttp|headless|lighthouse|render|uptime|cron-job|monitor", _re.I)
_SAYAC_HARIC = ("/static", "/cron", "/admin", "/favicon", "/ads.txt", "/robots.txt", "/sitemap", "/api")


def _yol_normallestir(yol):
    if yol.startswith("/paylas/"):
        return "/paylas/kral"
    if yol.startswith("/katil/"):
        return "/katil"
    if yol.startswith("/sifremi-unuttum/"):
        return "/sifremi-unuttum/durum"
    parcalar = ["<id>" if p.isdigit() else p for p in yol.split("/")]
    return ("/".join(parcalar) or "/")[:120]


def _istemci_ip():
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "")


def _tarayici_ozeti(ua):
    ua = ua or ""
    os_ = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
           else "Windows" if "Windows" in ua else "Mac" if "Mac OS" in ua else "Linux" if "Linux" in ua else "?")
    tr = ("Samsung" if "SamsungBrowser" in ua else "Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua
          else "Instagram" if "Instagram" in ua else "Chrome" if ("Chrome/" in ua or "CriOS" in ua)
          else "Firefox" if ("Firefox/" in ua or "FxiOS" in ua) else "Safari" if "Safari/" in ua else "?")
    return f"{tr} · {os_}"[:40]


_BOT_RE2 = _re.compile(r"mediapartners|adsbot|-google|google-|googleother|inspectiontool|yandex|bingpreview|"
                       r"ahrefs|semrush|petal|bytespider|gptbot|chatgpt|claude|perplexity|applebot|"
                       r"amazonbot|duckduck|baidu|mj12|dotbot|seznam|qwant", _re.I)


@app.route("/z", methods=["POST"])
def ziyaret_isareti():
    """Sayfa tarayıcıda gerçekten açılınca küçük bir JS işaretiyle sayılır.
    JS çalıştırmayan botlar, önizleme robotları ve ön yüklemeler böylece sayılmaz.
    Kimlik: girişliyse kullanıcı; değilse tarayıcıda tutulan rastgele anonim kimliğin
    o güne özel tuzla alınmış özeti (IP değişse de aynı kişi tek sayılır)."""
    try:
        if current_user.is_authenticated and getattr(current_user, "admin", False):
            return ("", 204)   # yöneticinin kendi gezintisi sayılmaz
        ua = request.headers.get("User-Agent", "")
        if not ua or _BOT_RE.search(ua) or _BOT_RE2.search(ua):
            return ("", 204)
        import json as _json
        try:
            veri = _json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            veri = {}
        yol_ham = str(veri.get("yol") or "/")[:300]
        if not yol_ham.startswith("/") or yol_ham.startswith(_SAYAC_HARIC):
            return ("", 204)
        vid = str(veri.get("vid") or "")[:64]
        simdi = utc_now_naive()
        gun = (simdi + TURKIYE_UTC_OFFSET).date()
        tuz = (app.config.get("SECRET_KEY") or "") + gun.isoformat()
        anon_kimlik = vid or f"{_istemci_ip()}|{ua}"
        anon = hashlib.sha256(f"{tuz}|{anon_kimlik}".encode()).hexdigest()[:16]
        kid = current_user.id if current_user.is_authenticated else None
        anahtar = f"u{kid}" if kid else anon
        yol = _yol_normallestir(yol_ham)

        k = ZiyaretGunluk.query.filter_by(gun=gun, ziyaretci=anahtar).first()
        if kid:
            # Aynı gün önce girişsiz gezip sonra giriş yaptıysa iki kişi sayılmasın
            eski = ZiyaretGunluk.query.filter_by(gun=gun, ziyaretci=anon).first()
            if eski and not k:
                eski.ziyaretci, eski.kullanici_id = anahtar, kid
                k = eski
            elif eski and k:
                k.sayfa_sayisi = (k.sayfa_sayisi or 0) + (eski.sayfa_sayisi or 0)
                k.ilk_saat = min(k.ilk_saat or simdi, eski.ilk_saat or simdi)
                k.kaynak = k.kaynak or eski.kaynak
                db.session.delete(eski)
        if k:
            k.sayfa_sayisi = (k.sayfa_sayisi or 0) + 1
            k.son_saat = simdi
        else:
            kaynak = None
            ref = str(veri.get("ref") or "")
            if ref:
                from urllib.parse import urlparse
                h = (urlparse(ref).netloc or "").lower()
                if h and h != request.host.lower() and not h.endswith((ANA_DOMAIN or "skorbilen.com")) \
                        and not h.endswith(".onrender.com"):
                    kaynak = h[:100]
            mobil = _re.search(r"Mobi|Android|iPhone|iPad", ua) is not None
            db.session.add(ZiyaretGunluk(gun=gun, ziyaretci=anahtar, kullanici_id=kid, ilk_sayfa=yol,
                                         kaynak=kaynak, cihaz="mobil" if mobil else "masaüstü",
                                         tarayici=_tarayici_ozeti(ua), ilk_saat=simdi, son_saat=simdi))
        sg = SayfaGunluk.query.filter_by(gun=gun, yol=yol).first()
        if sg:
            sg.sayi = (sg.sayi or 0) + 1
        else:
            db.session.add(SayfaGunluk(gun=gun, yol=yol, sayi=1))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return ("", 204)


SAYFA_ADLARI = {
    "/": "Ana sayfa (tanıtım)", "/dashboard": "Ana sayfa (girişli)", "/giris": "Giriş", "/kayit": "Kayıt",
    "/puan-durumu": "Puan durumu", "/nasil-oynanir": "Nasıl oynanır", "/gizlilik": "Gizlilik",
    "/iletisim": "İletişim", "/grup/<id>": "Grup sayfası", "/grup/<id>/sohbet": "Sohbet",
    "/profil": "Profil", "/paylas/kral": "Kral kartı (paylaşılan)", "/katil": "Davet linki",
    "/gruplar/yonet": "Grup kur / katıl", "/geri-bildirim": "Geri bildirim", "/sohbet": "Sohbet listesi",
}


def ziyaret_ozeti(gun_sayisi=30):
    bugun = (utc_now_naive() + TURKIYE_UTC_OFFSET).date()
    bas = bugun - timedelta(days=gun_sayisi - 1)
    satirlar = (db.session.query(ZiyaretGunluk.gun,
                                 db.func.count(ZiyaretGunluk.id),
                                 db.func.count(ZiyaretGunluk.kullanici_id),
                                 db.func.coalesce(db.func.sum(ZiyaretGunluk.sayfa_sayisi), 0))
                .filter(ZiyaretGunluk.gun >= bas).group_by(ZiyaretGunluk.gun).all())
    gunluk = {g: {"ziyaretci": z, "uye": u, "sayfa": int(sy)} for g, z, u, sy in satirlar}
    seri = []
    for i in range(gun_sayisi):
        g = bas + timedelta(days=i)
        v = gunluk.get(g, {"ziyaretci": 0, "uye": 0, "sayfa": 0})
        seri.append({"gun": g, "etiket": f"{g.day:02d}.{g.month:02d}", **v})
    enfazla = max([x["ziyaretci"] for x in seri] + [1])

    def toplam(gunden):
        q = ZiyaretGunluk.query.filter(ZiyaretGunluk.gun >= gunden)
        return {"ziyaretci": q.count(),
                "uye": db.session.query(db.func.count(db.distinct(ZiyaretGunluk.kullanici_id)))
                         .filter(ZiyaretGunluk.gun >= gunden).scalar() or 0,
                "sayfa": int(db.session.query(db.func.coalesce(db.func.sum(ZiyaretGunluk.sayfa_sayisi), 0))
                             .filter(ZiyaretGunluk.gun >= gunden).scalar() or 0)}

    hafta_bas = bugun - timedelta(days=6)
    sayfalar = (db.session.query(SayfaGunluk.yol, db.func.sum(SayfaGunluk.sayi).label("t"))
                .filter(SayfaGunluk.gun >= hafta_bas).group_by(SayfaGunluk.yol)
                .order_by(db.desc("t")).limit(12).all())
    kaynaklar = (db.session.query(ZiyaretGunluk.kaynak, db.func.count(ZiyaretGunluk.id).label("t"))
                 .filter(ZiyaretGunluk.gun >= hafta_bas, ZiyaretGunluk.kaynak.isnot(None))
                 .group_by(ZiyaretGunluk.kaynak).order_by(db.desc("t")).limit(10).all())
    cihazlar = dict(db.session.query(ZiyaretGunluk.cihaz, db.func.count(ZiyaretGunluk.id))
                    .filter(ZiyaretGunluk.gun >= hafta_bas).group_by(ZiyaretGunluk.cihaz).all())
    simdi = utc_now_naive()
    su_an = ZiyaretGunluk.query.filter(ZiyaretGunluk.son_saat >= simdi - timedelta(minutes=5)).count()
    bugunkuler = ZiyaretGunluk.query.filter_by(gun=bugun).order_by(ZiyaretGunluk.son_saat.desc()).limit(100).all()
    adlar = {u.id: u.kullanici_adi for u in Kullanici.query.filter(
        Kullanici.id.in_([z.kullanici_id for z in bugunkuler if z.kullanici_id] or [-1])).all()}
    yeni_uye = Kullanici.query.filter(Kullanici.olusturulma >= simdi - timedelta(days=7)).count()
    return {
        "bugun": gunluk.get(bugun, {"ziyaretci": 0, "uye": 0, "sayfa": 0}),
        "dun": gunluk.get(bugun - timedelta(days=1), {"ziyaretci": 0, "uye": 0, "sayfa": 0}),
        "hafta": toplam(hafta_bas), "ay": toplam(bas),
        "seri": seri, "enfazla": enfazla, "su_an": su_an, "yeni_uye": yeni_uye,
        "sayfalar": [(SAYFA_ADLARI.get(y, y), int(t)) for y, t in sayfalar],
        "kaynaklar": [(k, int(t)) for k, t in kaynaklar],
        "cihazlar": cihazlar,
        "bugunkuler": [{"kim": adlar.get(z.kullanici_id) if z.kullanici_id else None,
                        "ilk": z.ilk_saat, "son": z.son_saat, "sayfa": z.sayfa_sayisi,
                        "giris": SAYFA_ADLARI.get(z.ilk_sayfa, z.ilk_sayfa), "cihaz": z.cihaz,
                        "tarayici": z.tarayici, "kaynak": z.kaynak,
                        "aktif": z.son_saat and z.son_saat >= simdi - timedelta(minutes=5)} for z in bugunkuler],
    }


@app.route("/admin/ziyaretler")
@admin_required
def admin_ziyaretler():
    return render_template("admin_ziyaretler.html", o=ziyaret_ozeti(30))


# ------------------------------------------------------------
# Anketler: açık oylama (kim neye oy verdi görünür), kişi başı tek oy; anket bitene kadar değiştirilebilir.
# ------------------------------------------------------------
ANKET_SONUC_GOSTERIM = timedelta(days=3)   # bittikten sonra sonuç bu kadar süre görünür

ILK_ANKET_SORUSU = ("Beraberliği bilene 3 yerine 4 puan, beraberliği skoruyla tam bilene "
                    "5 yerine 6 puan verilsin mi?")


def _ilk_anketi_olustur():
    try:
        if not Anket.query.filter_by(soru=ILK_ANKET_SORUSU).first():
            db.session.add(Anket(soru=ILK_ANKET_SORUSU, secenekler="Evet\nHayır",
                                 bitis=datetime(2026, 10, 21, 15, 0)))   # 21.10.2026 18:00 TR
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print("[anket] ilk anket oluşturulamadı:", e)


with app.app_context():
    _ilk_anketi_olustur()


def anket_ozeti(anket, izleyen=None):
    secenekler = anket.secenek_listesi()
    oylar = AnketOyu.query.options(db.joinedload(AnketOyu.kullanici)).filter_by(anket_id=anket.id).all()
    # İsimler: izleyenle aynı grupta olanlar görünür (tek grupta herkes); sayılar herkes için
    gorunur = None
    if izleyen is not None and not getattr(izleyen, "admin", False):
        gruplar = [g for (g,) in db.session.query(GrupUyeligi.grup_id).filter_by(kullanici_id=izleyen.id)]
        gorunur = {k for (k,) in db.session.query(GrupUyeligi.kullanici_id).filter(GrupUyeligi.grup_id.in_(gruplar or [-1]))}
    toplam = len(oylar)
    satirlar = []
    for i, ad in enumerate(secenekler):
        bu = [o for o in oylar if o.secenek == i]
        isimler = sorted((o.kullanici.kullanici_adi for o in bu if o.kullanici and (gorunur is None or o.kullanici_id in gorunur)),
                         key=lambda x: x.lower())
        satirlar.append({"ad": ad, "sayi": len(bu), "yuzde": round(100 * len(bu) / toplam) if toplam else 0, "isimler": isimler})
    benim = None
    if izleyen is not None:
        b = next((o for o in oylar if o.kullanici_id == izleyen.id), None)
        benim = b.secenek if b else None
    return {"id": anket.id, "soru": anket.soru, "secenekler": secenekler, "satirlar": satirlar, "toplam": toplam,
            "acik": anket.acik_mi(), "bitis": anket.bitis, "benim": benim}


def gosterilecek_anketler(kullanici):
    simdi = utc_now_naive()
    q = Anket.query.filter(Anket.bitis > simdi - ANKET_SONUC_GOSTERIM).order_by(Anket.id.desc()).all()
    # Bitmiş anketlerin sonuçları kullanıcılara gösterilmez (admin panelinde görünür)
    return [anket_ozeti(a, kullanici) for a in q if a.acik_mi()]


@app.context_processor
def anket_baglami():
    if not current_user.is_authenticated or request.endpoint not in ("dashboard", "grup"):
        return {}
    try:
        return {"anketler": gosterilecek_anketler(current_user)}
    except Exception:
        db.session.rollback()
        return {"anketler": []}


@app.route("/anket/<int:anket_id>/oy", methods=["POST"])
@login_required
def anket_oy(anket_id):
    anket = db.session.get(Anket, anket_id)
    if not anket:
        abort(404)
    veri = request.get_json(silent=True) or request.form
    try:
        secenek = int(veri.get("secenek"))
    except (TypeError, ValueError):
        return jsonify({"hata": "Geçersiz seçenek."}), 400
    if not anket.acik_mi():
        return jsonify({"hata": "Anket sona erdi.", "ozet": _anket_json(anket)}), 400
    if yz_mi(current_user) or not (0 <= secenek < len(anket.secenek_listesi())):
        return jsonify({"hata": "Geçersiz seçenek."}), 400
    # Anket açık olduğu sürece oy değiştirilebilir
    oy = AnketOyu.query.filter_by(anket_id=anket.id, kullanici_id=current_user.id).first()
    if oy:
        oy.secenek, oy.zaman = secenek, datetime.utcnow()
    else:
        db.session.add(AnketOyu(anket_id=anket.id, kullanici_id=current_user.id, secenek=secenek))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    if request.is_json:
        return jsonify({"ozet": _anket_json(anket)})
    return redirect(request.referrer or url_for("dashboard"))


def _anket_json(anket):
    o = anket_ozeti(anket, current_user)
    o["bitis"] = tr_saat_filter(o["bitis"], "%d.%m %H:%M")
    return o


@app.route("/admin/anketler", methods=["GET", "POST"])
@admin_required
def admin_anketler():
    if request.method == "POST":
        soru = request.form.get("soru", "").strip()[:300]
        secenekler = [x.strip()[:80] for x in request.form.get("secenekler", "").splitlines() if x.strip()][:6]
        bitis_str = request.form.get("bitis", "").strip()
        try:
            bitis = datetime.strptime(bitis_str, "%Y-%m-%dT%H:%M") - TURKIYE_UTC_OFFSET
        except ValueError:
            bitis = None
        if not soru or len(secenekler) < 2 or not bitis:
            flash("Soru, en az 2 seçenek ve bitiş zamanı gerekli.", "danger")
        else:
            db.session.add(Anket(soru=soru, secenekler="\n".join(secenekler), bitis=bitis))
            db.session.commit()
            flash("Anket yayınlandı.", "success")
        return redirect(url_for("admin_anketler"))
    anketler = [anket_ozeti(a) for a in Anket.query.order_by(Anket.id.desc()).limit(20).all()]
    return render_template("admin_anketler.html", anketler=anketler)


@app.route("/admin/anketler/<int:anket_id>/kapat", methods=["POST"])
@admin_required
def admin_anket_kapat(anket_id):
    a = db.session.get(Anket, anket_id)
    if a:
        a.kapali = True
        db.session.commit()
        flash("Anket kapatıldı.", "success")
    return redirect(url_for("admin_anketler"))


# ------------------------------------------------------------
# Admin ile birebir destek yazışması. Girişli kullanıcı kendi hesabıyla,
# giriş yapamayan (örn. şifresini unutan) misafir ise tarayıcısına verilen
# gizli anahtarla kendi yazışmasına ulaşır.
# ------------------------------------------------------------
DESTEK_MAX_UZUNLUK = 1500


def _destek_konusmasi(olustur=False, misafir_ad=None):
    if current_user.is_authenticated:
        k = DestekKonusma.query.filter_by(kullanici_id=current_user.id).first()
        if not k and olustur:
            k = DestekKonusma(kullanici_id=current_user.id)
            db.session.add(k)
            db.session.flush()
        return k
    anahtar = session.get("destek_anahtar")
    if anahtar:
        k = DestekKonusma.query.filter_by(anahtar_hash=_anahtar_hash(anahtar)).first()
        if k:
            return k
    if olustur:
        import secrets
        anahtar = secrets.token_urlsafe(24)
        session["destek_anahtar"] = anahtar
        session.permanent = True
        k = DestekKonusma(misafir_ad=(misafir_ad or "")[:80] or None, anahtar_hash=_anahtar_hash(anahtar))
        db.session.add(k)
        db.session.flush()
        return k
    return None


def _destek_json(k):
    return [{"id": m.id, "admin": m.admin_mi, "metin": m.metin, "saat": tr_saat_filter(m.zaman, "%d.%m %H:%M")}
            for m in (k.mesajlar if k else [])]


@app.route("/destek", methods=["GET", "POST"])
def destek():
    if request.method == "POST":
        metin = (request.form.get("metin") or "").strip()[:DESTEK_MAX_UZUNLUK]
        if request.form.get("web_sitesi"):          # bot tuzağı
            return redirect(url_for("destek"))
        if len(metin) < 2:
            flash("Mesaj boş olamaz.", "warning")
            return redirect(url_for("destek"))
        # Basit sınır: 10 dakikada en fazla 8 mesaj
        simdi_ts = utc_now_naive().timestamp()
        gecmis = [t for t in session.get("destek_zamanlar", []) if simdi_ts - t < 600]
        if len(gecmis) >= 8:
            flash("Kısa sürede çok mesaj gönderdin, biraz sonra tekrar dene.", "warning")
            return redirect(url_for("destek"))
        session["destek_zamanlar"] = gecmis + [simdi_ts]
        k = _destek_konusmasi(olustur=True, misafir_ad=request.form.get("ad", "").strip())
        db.session.add(DestekMesaj(konusma_id=k.id, admin_mi=False, metin=metin))
        k.son_mesaj, k.admin_okumadi = datetime.utcnow(), True
        db.session.commit()
        return redirect(url_for("destek") + "#son")
    k = _destek_konusmasi()
    if k and k.kullanici_okumadi:
        k.kullanici_okumadi = False
        db.session.commit()
    return render_template("destek.html", konusma=k, mesajlar=_destek_json(k), max_uzunluk=DESTEK_MAX_UZUNLUK)


@app.route("/destek/mesajlar")
def destek_mesajlar():
    k = _destek_konusmasi()
    if k and k.kullanici_okumadi:
        k.kullanici_okumadi = False
        db.session.commit()
    return jsonify({"mesajlar": _destek_json(k)})


@app.context_processor
def destek_bildirimi():
    """Admin yanıt verdiyse kullanıcıya gösterilecek işaret."""
    try:
        if current_user.is_authenticated:
            k = DestekKonusma.query.filter_by(kullanici_id=current_user.id, kullanici_okumadi=True).first()
            return {"destek_yanit_var": bool(k)}
    except Exception:
        db.session.rollback()
    return {"destek_yanit_var": False}


@app.route("/admin/destek")
@admin_required
def admin_destek():
    konusmalar = (DestekKonusma.query.options(db.joinedload(DestekKonusma.kullanici))
                  .order_by(DestekKonusma.admin_okumadi.desc(), DestekKonusma.son_mesaj.desc()).limit(100).all())
    ozet = []
    for k in konusmalar:
        son = k.mesajlar[-1] if k.mesajlar else None
        ozet.append({"k": k, "son": son})
    return render_template("admin_destek.html", konusmalar=ozet)


@app.route("/admin/destek/<int:konusma_id>", methods=["GET", "POST"])
@admin_required
def admin_destek_konusma(konusma_id):
    k = db.session.get(DestekKonusma, konusma_id) or abort(404)
    if request.method == "POST":
        metin = (request.form.get("metin") or "").strip()[:DESTEK_MAX_UZUNLUK]
        if metin:
            db.session.add(DestekMesaj(konusma_id=k.id, admin_mi=True, metin=metin))
            k.son_mesaj, k.kullanici_okumadi = datetime.utcnow(), True
        k.admin_okumadi = False
        db.session.commit()
        return redirect(url_for("admin_destek_konusma", konusma_id=k.id) + "#son")
    if k.admin_okumadi:
        k.admin_okumadi = False
        db.session.commit()
    return render_template("admin_destek_konusma.html", k=k, mesajlar=_destek_json(k), max_uzunluk=DESTEK_MAX_UZUNLUK)


# ------------------------------------------------------------
# GÜNLÜK KELİME OYUNU (Türkçe Wordle). Her gün Türkiye saatiyle 00:00'da
# herkese aynı 5 harfli kelime. Cevap tarayıcıya gönderilmez; her tahmini
# sunucu kontrol eder. Sıralama sadece giriş yapanlar için, sadece o gün.
# ------------------------------------------------------------
KELIME_UZUNLUK = 5
KELIME_HAK = 6
KELIME_BASLANGIC_GUNU = date(2026, 10, 24)      # 1 numaralı bulmaca
_KELIME_DIZINI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kelime")


def _kelime_dosyasi(ad):
    try:
        with open(os.path.join(_KELIME_DIZINI, ad), encoding="utf-8") as f:
            return [s.strip() for s in f if s.strip()]
    except OSError:
        return []


KELIME_CEVAPLAR = _kelime_dosyasi("cevaplar.txt")
KELIME_GECERLI = set(_kelime_dosyasi("gecerli.txt")) | set(KELIME_CEVAPLAR)


def tr_kucuk(metin):
    return (metin or "").replace("I", "ı").replace("İ", "i").lower().strip()


def kelime_bugun():
    """Türkiye saatine göre bugünün tarihi."""
    return (utc_now_naive() + TURKIYE_UTC_OFFSET).date()


def kelime_numarasi(gun=None):
    return ((gun or kelime_bugun()) - KELIME_BASLANGIC_GUNU).days + 1


def kelime_cevabi(gun=None):
    if not KELIME_CEVAPLAR:
        return None
    return KELIME_CEVAPLAR[(kelime_numarasi(gun) - 1) % len(KELIME_CEVAPLAR)]


def kelime_renkleri(tahmin, cevap):
    """Wordle renkleri: 'y' doğru yer, 's' kelimede var, 'g' yok. Tekrarlı harfler doğru sayılır."""
    sonuc = ["g"] * len(tahmin)
    kalan = {}
    for i, (t, c) in enumerate(zip(tahmin, cevap)):
        if t == c:
            sonuc[i] = "y"
        else:
            kalan[c] = kalan.get(c, 0) + 1
    for i, t in enumerate(tahmin):
        if sonuc[i] != "y" and kalan.get(t, 0) > 0:
            sonuc[i] = "s"
            kalan[t] -= 1
    return "".join(sonuc)


def _kelime_durumu():
    """Bugünkü oyunun durumu. Giriş yapmışsa veritabanı, misafirse oturum çerezi."""
    gun = kelime_bugun()
    if current_user.is_authenticated:
        oyun = KelimeOyunu.query.filter_by(kullanici_id=current_user.id, gun=gun).first()
        return {"tahminler": oyun.liste() if oyun else [], "bitti": bool(oyun and oyun.bitti),
                "kazandi": bool(oyun and oyun.kazandi), "oyun": oyun}
    m = session.get("kelime") or {}
    if m.get("gun") != gun.isoformat():
        m = {}
    return {"tahminler": m.get("t", []), "bitti": m.get("b", False), "kazandi": m.get("k", False), "oyun": None}


def kelime_siralamasi(gun=None):
    gun = gun or kelime_bugun()
    oyunlar = (KelimeOyunu.query.options(db.joinedload(KelimeOyunu.kullanici))
               .filter(KelimeOyunu.gun == gun, KelimeOyunu.bitti.is_(True)).all())

    def sure(o):
        if o.baslangic and o.bitis:
            return max(0, int((o.bitis - o.baslangic).total_seconds()))
        return 10 ** 9
    oyunlar.sort(key=lambda o: (not o.kazandi, len(o.liste()) if o.kazandi else 99, sure(o)))
    # İsimler sadece aynı grupta olduğun kişilerde (ve kendinde) görünür; misafir
    # ve grup dışı kişiler sadece sırayı görür. Gizli isim sunucudan hiç gönderilmez.
    gorunur = set()
    if current_user.is_authenticated:
        gorunur.add(current_user.id)
        gidler = kullanicinin_grup_idleri(current_user.id)
        if gidler:
            gorunur |= {kid for (kid,) in db.session.query(GrupUyeligi.kullanici_id)
                        .filter(GrupUyeligi.grup_id.in_(gidler)).all()}
    liste, sira = [], 0
    for o in oyunlar:
        if not o.kullanici or yz_mi(o.kullanici):
            continue
        sira += 1
        s = sure(o)
        acik = o.kullanici_id in gorunur
        liste.append({"sira": sira if o.kazandi else None,
                      "ad": o.kullanici.kullanici_adi if acik else "Oyuncu" + "•" * (3 + o.id % 5),
                      "gizli": not acik,
                      "kullanici_id": o.kullanici_id if acik else None, "kazandi": o.kazandi,
                      "deneme": len(o.liste()), "sure": (f"{s // 60}:{s % 60:02d}" if s < 10 ** 9 else "")})
    return liste


def _kelime_yanit(durum, cevap):
    veri = {"tahminler": [{"kelime": t, "renk": kelime_renkleri(t, cevap)} for t in durum["tahminler"]],
            "bitti": durum["bitti"], "kazandi": durum["kazandi"], "hak": KELIME_HAK,
            "numara": kelime_numarasi()}
    if durum["bitti"]:
        veri["cevap"] = cevap
    return veri


@app.route("/kelime")
def kelime_oyunu():
    cevap = kelime_cevabi()
    if not cevap:
        abort(404)
    durum = _kelime_durumu()
    dun = kelime_bugun() - timedelta(days=1)
    return render_template(
        "kelime.html", veri=_kelime_yanit(durum, cevap), siralama=kelime_siralamasi(),
        dunku_kelime=kelime_cevabi(dun) if dun >= KELIME_BASLANGIC_GUNU else None,
        uzunluk=KELIME_UZUNLUK,
    )


@app.route("/kelime/tahmin", methods=["POST"])
def kelime_tahmin():
    cevap = kelime_cevabi()
    if not cevap:
        abort(404)
    tahmin = tr_kucuk((request.get_json(silent=True) or {}).get("kelime") or request.form.get("kelime"))
    durum = _kelime_durumu()
    if durum["bitti"]:
        return jsonify({"hata": "Bugünkü kelime bitti, yarın yenisi gelecek.", **_kelime_yanit(durum, cevap)}), 400
    if len(tahmin) != KELIME_UZUNLUK:
        return jsonify({"hata": f"Kelime {KELIME_UZUNLUK} harfli olmalı."}), 400
    if tahmin not in KELIME_GECERLI:
        return jsonify({"hata": "Bu kelime sözlükte yok."}), 400
    if tahmin in durum["tahminler"]:
        return jsonify({"hata": "Bu kelimeyi zaten denedin."}), 400

    tahminler = durum["tahminler"] + [tahmin]
    kazandi = tahmin == cevap
    bitti = kazandi or len(tahminler) >= KELIME_HAK
    simdi = utc_now_naive()
    if current_user.is_authenticated:
        oyun = durum["oyun"]
        if oyun is None:
            oyun = KelimeOyunu(kullanici_id=current_user.id, gun=kelime_bugun(), baslangic=simdi)
            db.session.add(oyun)
        oyun.tahminler = ",".join(tahminler)
        oyun.kazandi, oyun.bitti = kazandi, bitti
        if bitti:
            oyun.bitis = simdi
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"hata": "Kaydedilemedi, sayfayı yenileyip tekrar dene."}), 409
    else:
        session["kelime"] = {"gun": kelime_bugun().isoformat(), "t": tahminler, "b": bitti, "k": kazandi}
        session.permanent = True
    yeni = {"tahminler": tahminler, "bitti": bitti, "kazandi": kazandi}
    veri = _kelime_yanit(yeni, cevap)
    if bitti:
        veri["siralama"] = kelime_siralamasi()
    return jsonify(veri)


def kelime_karti_bilgisi():
    """Ana sayfa kartı için: bugünkü bulmaca no ve bugün bitiren kişi sayısı."""
    bilgi = {"numara": kelime_numarasi(), "oynayan": 0, "bitirdi": False}
    try:
        bilgi["oynayan"] = KelimeOyunu.query.filter_by(gun=kelime_bugun(), bitti=True).count()
        if current_user.is_authenticated:
            bilgi["bitirdi"] = bool(KelimeOyunu.query.filter_by(
                kullanici_id=current_user.id, gun=kelime_bugun(), bitti=True).first())
        else:
            m = session.get("kelime") or {}
            bilgi["bitirdi"] = m.get("gun") == kelime_bugun().isoformat() and m.get("b", False)
    except Exception:
        db.session.rollback()
    return bilgi


app.jinja_env.globals["kelime_karti_bilgisi"] = kelime_karti_bilgisi


@app.route("/kelime/siralama")
def kelime_siralama_json():
    return jsonify({"siralama": kelime_siralamasi()})


@app.route("/admin/geri-bildirimler")
@admin_required
def admin_geri_bildirimler():
    hepsi = request.args.get("hepsi") == "1"
    q = GeriBildirim.query.options(db.joinedload(GeriBildirim.kullanici)).order_by(GeriBildirim.id.desc())
    if not hepsi:
        q = q.filter_by(okundu=False)
    return render_template("admin_geri_bildirimler.html", bildirimler=q.limit(200).all(),
                           hepsi=hepsi, turler=GERI_BILDIRIM_TURLERI)


@app.route("/admin/geri-bildirimler/<int:gb_id>/<islem>", methods=["POST"])
@admin_required
def admin_geri_bildirim_islem(gb_id, islem):
    gb = db.session.get(GeriBildirim, gb_id)
    if gb:
        if islem == "okundu":
            gb.okundu = True
        elif islem == "okunmadi":
            gb.okundu = False
        elif islem == "sil":
            db.session.delete(gb)
        db.session.commit()
    return redirect(url_for("admin_geri_bildirimler", hepsi=request.args.get("hepsi")))


@app.route("/admin/geri-bildirimler/hepsini-okundu-yap", methods=["POST"])
@admin_required
def admin_geri_bildirim_hepsi_okundu():
    GeriBildirim.query.filter_by(okundu=False).update({"okundu": True})
    db.session.commit()
    return redirect(url_for("admin_geri_bildirimler"))


@app.route("/cikis")
@login_required
def cikis():
    logout_user()
    flash("Çıkış yapıldı.", "success")
    return redirect(url_for("giris"))


def aktif_hafta_maclari(now):
    """Grup sayfasında tahmin için gösterilen (bu haftanın) maçlar.
    Döner: (maclar, aktif_haftalar, sonraki_mac_tarihleri, pencere_sonu)"""
    pencere_sonu = now + timedelta(days=HAFTA_PENCERESI_GUN)
    aktif_haftalar = {}
    sonraki_mac_tarihleri = {}
    for lig_id in gosterilecek_lig_id_listesi():
        # Haftanın son maçı BİTENE kadar o hafta gösterilsin: önce başlamış ama
        # henüz bitmemiş (oynanan / sonucu beklenen) maça bak, yoksa sıradaki maça.
        devam_eden = Mac.query.filter(
            Mac.lig_id == lig_id, Mac.baslangic.isnot(None),
            Mac.baslangic < now, Mac.baslangic >= now - timedelta(hours=AKTIF_HAFTA_BEKLEME_SAAT),
            ~Mac.durum.in_(["bitti", "iptal", "ertelendi"]),
            super_lig_takim_filtresi(),
        ).order_by(Mac.baslangic.asc()).first()
        siradaki = Mac.query.filter(
            Mac.lig_id == lig_id, Mac.baslangic.isnot(None), Mac.baslangic >= now,
            super_lig_takim_filtresi(),
        ).order_by(Mac.baslangic.asc()).first()
        # Hafta bittikten sonra 1 gün daha o haftayı göster (sonuçlar/puanlar
        # görülsün). Ama yeni haftanın ilk maçı o 1 gün içinde başlıyorsa
        # tahmin girilebilsin diye yeni haftaya geç.
        son_biten = None
        if not devam_eden and not (siradaki and siradaki.baslangic <= now + timedelta(hours=BITEN_HAFTA_GOSTERIM_SAAT)):
            son_biten = Mac.query.filter(
                Mac.lig_id == lig_id, Mac.baslangic.isnot(None), Mac.baslangic < now,
                Mac.baslangic >= now - timedelta(hours=BITEN_HAFTA_GOSTERIM_SAAT + 2),
                Mac.durum == "bitti",
                super_lig_takim_filtresi(),
            ).order_by(Mac.baslangic.desc()).first()
        en_yakin_mac = devam_eden or son_biten or siradaki
        if not en_yakin_mac:
            continue
        if en_yakin_mac.hafta and en_yakin_mac.baslangic <= pencere_sonu:
            aktif_haftalar[lig_id] = en_yakin_mac.hafta
        else:
            sonraki_mac_tarihleri[lig_id] = en_yakin_mac.baslangic

    maclar = []
    if aktif_haftalar:
        kosullar = [db.and_(Mac.lig_id == lig_id, Mac.hafta == hafta) for lig_id, hafta in aktif_haftalar.items()]
        maclar = Mac.query.filter(
            Mac.lig_id.in_(gosterilecek_lig_id_listesi()), db.or_(*kosullar),
            Mac.baslangic <= pencere_sonu,
            super_lig_takim_filtresi(),
        ).order_by(Mac.baslangic.asc()).all()

    return maclar, aktif_haftalar, sonraki_mac_tarihleri, pencere_sonu


@app.route("/dashboard")
@login_required
def dashboard():
    now = utc_now_naive()
    yaklasan_maclar = Mac.query.filter(
        Mac.baslangic.isnot(None), Mac.baslangic > now,
        Mac.lig_id.in_(gosterilecek_lig_id_listesi()),
        ~Mac.durum.in_(["bitti", "iptal", "ertelendi"]),
        super_lig_takim_filtresi(),
    ).order_by(Mac.baslangic.asc()).limit(10).all()

    biten_maclar = Mac.query.filter(
        Mac.lig_id.in_(gosterilecek_lig_id_listesi()), Mac.durum == "bitti",
        super_lig_takim_filtresi(),
    ).order_by(Mac.baslangic.desc()).limit(5).all()

    tahminler = {t.mac_id: t for t in Tahmin.query.filter_by(kullanici_id=current_user.id).all()}
    gruplar = [u.grup for u in GrupUyeligi.query.filter_by(kullanici_id=current_user.id).all()]
    toplam_puan = (db.session.query(db.func.coalesce(db.func.sum(Tahmin.puan), 0)).filter(Tahmin.kullanici_id == current_user.id).scalar() or 0) \
        + ceza_toplamlari(kullanici_id=current_user.id).get(current_user.id, 0)
    toplam_tahmin = Tahmin.query.filter_by(kullanici_id=current_user.id).count()

    return render_template(
        "dashboard.html",
        haberler=son_haberler(),
        yaklasan_maclar=yaklasan_maclar,
        biten_maclar=biten_maclar,
        tahminler=tahminler,
        tahmin_acik_maclar=[m for m in aktif_hafta_maclari(now)[0]
                            if m.baslangic and m.baslangic > now and m.durum not in ("bitti", "iptal", "ertelendi")]
                           + Mac.query.filter(
                               Mac.ozel_grup_id.in_([g.id for g in gruplar] or [-1]), Mac.baslangic > now,
                               Mac.baslangic <= now + timedelta(days=HAFTA_PENCERESI_GUN),
                               ~Mac.durum.in_(["bitti", "iptal", "ertelendi"])).all(),
        gruplar=gruplar,
        toplam_puan=toplam_puan,
        toplam_tahmin=toplam_tahmin,
        tahmin_edilen_maclar=toplam_tahmin
    )


@app.route("/gruplar")
@login_required
def gruplar():
    """Alt menüdeki 'Gruplar': doğrudan kendi grubuna (en son baktığın) götürür.
    Hiç grubun yoksa grup kur / katıl sayfası açılır."""
    grup_idleri = [u.grup_id for u in GrupUyeligi.query.filter_by(kullanici_id=current_user.id)
                   .order_by(GrupUyeligi.katilma_tarihi.asc()).all()]
    if not grup_idleri:
        return redirect(url_for("grup_yonet"))
    son = session.get("son_grup")
    return redirect(url_for("grup", grup_id=son if son in grup_idleri else grup_idleri[0]))


@app.route("/gruplar/yonet")
@login_required
def grup_yonet():
    """Gruplarım listesi + grup kur + gruba katıl."""
    uyelikler = GrupUyeligi.query.filter_by(kullanici_id=current_user.id).all()
    return render_template("gruplar.html", gruplar=[u.grup for u in uyelikler])


@app.route("/grup-olustur", methods=["POST"])
@login_required
def grup_olustur():
    ad = request.form.get("ad", "").strip()
    aciklama = request.form.get("aciklama", "").strip()
    if not ad:
        flash("Grup adı girin.", "danger")
        return redirect(url_for("grup_yonet"))

    grup = Grup(ad=ad, aciklama=aciklama, davet_kodu=generate_invite_code(), sezon=2026, kurucu_id=current_user.id)
    db.session.add(grup)
    db.session.flush()
    db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=current_user.id))
    try:
        db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=yz_kullanici_getir().id))
    except Exception:
        pass
    db.session.commit()
    _tahminleri_esitle_guvenli(current_user.id)
    flash("Grup oluşturuldu.", "success")
    return redirect(url_for("grup", grup_id=grup.id))


# ------------------------------------------------------------
# Tahminler tüm gruplarda ortak: kullanıcı bir maça tek tahmin girer,
# üyesi olduğu her grupta aynı tahmin geçerli olur. Tablo grup bazlı
# kalıyor (puan/sıralama sorguları değişmesin diye), kayıt her gruba yazılır.
# ------------------------------------------------------------
def kullanicinin_grup_idleri(kullanici_id):
    return [gid for (gid,) in db.session.query(GrupUyeligi.grup_id)
            .filter(GrupUyeligi.kullanici_id == kullanici_id).all()]


def tahmini_tum_gruplara_yaz(kullanici_id, mac_id, ev_skor, deplasman_skor, grup_idleri=None):
    """Bir maçın tahminini kullanıcının tüm gruplarına yazar (yoksa ekler, varsa günceller)."""
    if grup_idleri is None:
        grup_idleri = kullanicinin_grup_idleri(kullanici_id)
    mac = db.session.get(Mac, mac_id)
    if mac is not None and mac.ozel_grup_id:
        # Gruba özel maç: tahmin sadece o gruba yazılır, diğer gruplara yansımaz
        grup_idleri = [g for g in grup_idleri if g == mac.ozel_grup_id]
    mevcut = {t.grup_id: t for t in Tahmin.query.filter(
        Tahmin.kullanici_id == kullanici_id, Tahmin.mac_id == mac_id,
        Tahmin.grup_id.in_(grup_idleri or [-1])).all()}
    simdi = datetime.utcnow()
    for gid in grup_idleri:
        t = mevcut.get(gid)
        if t:
            t.ev_skor = ev_skor
            t.deplasman_skor = deplasman_skor
            t.guncellenme = simdi
        else:
            db.session.add(Tahmin(grup_id=gid, mac_id=mac_id, kullanici_id=kullanici_id,
                                  ev_skor=ev_skor, deplasman_skor=deplasman_skor, puan=0))


def tahminleri_gruplara_esitle(kullanici_id=None):
    """Henüz başlamamış maçlarda, kullanıcının en son girdiği tahmini tüm gruplarına
    kopyalar. Yeni gruba katılınca ve uygulama açılışında (eski veriler için) çalışır.
    Başlamış/bitmiş maçlara dokunmaz. Robot Ziya zaten her gruba ayrı tahmin girer."""
    simdi = utc_now_naive()
    q = (db.session.query(Tahmin).join(Mac, Mac.id == Tahmin.mac_id)
         .filter(Mac.baslangic.isnot(None), Mac.baslangic > simdi, Mac.ozel_grup_id.is_(None),
                 ~Mac.durum.in_(["bitti", "iptal", "ertelendi"])))
    if kullanici_id is not None:
        q = q.filter(Tahmin.kullanici_id == kullanici_id)
    en_son = {}
    for t in q.all():
        anahtar = (t.kullanici_id, t.mac_id)
        z = t.guncellenme or t.olusturulma or datetime.min
        if anahtar not in en_son or z > en_son[anahtar][0]:
            en_son[anahtar] = (z, t.ev_skor, t.deplasman_skor)
    if not en_son:
        return 0
    yz = yz_kullanici_getir(olustur=False)
    yz_id = yz.id if yz else None
    gruplar = {}
    degisen = 0
    for (kid, mid), (_, ev, dep) in en_son.items():
        if kid == yz_id:
            continue
        if kid not in gruplar:
            gruplar[kid] = kullanicinin_grup_idleri(kid)
        once = {(t.grup_id, t.ev_skor, t.deplasman_skor) for t in Tahmin.query.filter_by(kullanici_id=kid, mac_id=mid)}
        if all((gid, ev, dep) in once for gid in gruplar[kid]):
            continue
        tahmini_tum_gruplara_yaz(kid, mid, ev, dep, gruplar[kid])
        degisen += 1
    db.session.commit()
    return degisen


def _tahminleri_esitle_guvenli(kullanici_id=None):
    try:
        return tahminleri_gruplara_esitle(kullanici_id)
    except Exception as e:
        db.session.rollback()
        print("[tahmin-esitle] hata:", e)
        return 0




@app.route("/grup-katil", methods=["POST"])
@login_required
def grup_katil():
    davet_kodu = request.form.get("davet_kodu", "").strip().upper()
    grup = Grup.query.filter_by(davet_kodu=davet_kodu).first()
    if not grup:
        flash("Geçersiz davet kodu.", "danger")
        return redirect(url_for("grup_yonet"))

    if GrupUyeligi.query.filter_by(grup_id=grup.id, kullanici_id=current_user.id).first():
        flash("Zaten bu grubun üyesisiniz.", "warning")
    else:
        db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=current_user.id))
        db.session.commit()
        _tahminleri_esitle_guvenli(current_user.id)
        flash("Gruba katıldınız. Diğer gruplarındaki tahminlerin bu gruba da geçti.", "success")
    return redirect(url_for("grup", grup_id=grup.id))


@app.route("/katil/<davet_kodu>")
def katil_link(davet_kodu):
    """
    Tek tıkla paylaşılabilir davet linki (örn. WhatsApp'a atılan link).
    Giriş yapılmamışsa önce kayıt/giriş ekranına yönlendirir, sonrasında
    otomatik olarak aynı linke geri dönüp gruba katılmayı tamamlar.
    """
    davet_kodu = davet_kodu.strip().upper()
    grup = Grup.query.filter_by(davet_kodu=davet_kodu).first()
    if not grup:
        flash("Geçersiz veya süresi dolmuş davet linki.", "danger")
        return redirect(url_for("giris") if not current_user.is_authenticated else url_for("grup_yonet"))

    if not current_user.is_authenticated:
        hedef = url_for("katil_link", davet_kodu=davet_kodu)
        flash(f"'{grup.ad}' grubuna katılmak için önce giriş yap ya da kayıt ol.", "success")
        return redirect(url_for("giris", next=hedef))

    if not GrupUyeligi.query.filter_by(grup_id=grup.id, kullanici_id=current_user.id).first():
        db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=current_user.id))
        db.session.commit()
        _tahminleri_esitle_guvenli(current_user.id)
        flash(f"'{grup.ad}' grubuna katıldınız. 🎉", "success")

    return redirect(url_for("grup", grup_id=grup.id))


def hafta_anahtari(tarih):
    """Bir maç başlangıç tarihinin ait olduğu takvim haftasının (o haftanın
    Pazartesi 00:00'ı) anahtarını üretir. Lig/hafta numarası yerine gerçek
    takvime göre gruplama yapılır ki Şampiyonlar Ligi ve Süper Lig maçları
    aynı haftada oynanıyorsa TEK bir haftalık tabloda birleşsin."""
    if not tarih:
        return None
    # Hafta, Türkiye saatiyle Cuma 12:00'de başlar (Cuma 12:00 → sonraki Cuma 12:00).
    # tarih UTC (naive); dönüş de UTC (naive).
    tr = tarih + TURKIYE_UTC_OFFSET
    gun = (tr.weekday() - HAFTA_BASLANGIC_GUNU) % 7
    bas = (tr - timedelta(days=gun)).replace(hour=HAFTA_BASLANGIC_SAATI, minute=0, second=0, microsecond=0)
    if bas > tr:
        bas -= timedelta(days=7)
    return bas - TURKIYE_UTC_OFFSET


def _haftalik_sira_anahtari(satir):
    """Puana göre sırala; eşit puanda Robot Ziya hep insanların arkasında kalsın
    (hafta sıfırlanınca herkes 0'dayken sonuncu Ziya olur)."""
    return (satir["points"], not yz_mi(satir.get("user")))


def haftalik_liderlik_hesapla(grup_id):
    """
    Grubun daha önce oynanmış TÜM maçlarını (Şampiyonlar Ligi + Süper Lig
    birlikte, ayrı ayrı değil) gerçek takvim haftasına göre gruplayıp o
    haftaya özel puan toplamlarını hesaplar. Böylece her hafta puanlar
    sıfırdan başlar ama geçmiş haftaların zirve/lider listesi kalıcı olarak
    görülebilir.
    """
    kayitlar = (
        db.session.query(Mac.baslangic, Tahmin.kullanici_id, Tahmin.puan)
        .join(Mac, Tahmin.mac_id == Mac.id)
        .filter(Tahmin.grup_id == grup_id, Mac.baslangic.isnot(None), Mac.durum == "bitti")
        .all()
    )

    cezalar = (db.session.query(Mac.baslangic, TahminCezasi.kullanici_id, TahminCezasi.puan)
               .join(Mac, TahminCezasi.mac_id == Mac.id)
               .filter(TahminCezasi.grup_id == grup_id, Mac.baslangic.isnot(None)).all())
    kayitlar = list(kayitlar) + list(cezalar)
    haftalar = {}
    for baslangic, kullanici_id, puan in kayitlar:
        anahtar = hafta_anahtari(baslangic)
        veri = haftalar.setdefault(anahtar, {"puanlar": {}})
        veri["puanlar"][kullanici_id] = veri["puanlar"].get(kullanici_id, 0) + (puan or 0)

    kullanici_map = {u.kullanici_id: u.kullanici for u in GrupUyeligi.query.options(db.joinedload(GrupUyeligi.kullanici)).filter_by(grup_id=grup_id).all()}

    sonuc = []
    for hafta_basi, veri in haftalar.items():
        siralama = sorted(
            [
                {"user": kullanici_map.get(uid), "points": puan}
                for uid, puan in veri["puanlar"].items() if kullanici_map.get(uid)
            ],
            key=_haftalik_sira_anahtari,
            reverse=True
        )
        sonuc.append({
            "hafta_basi": hafta_basi,
            "hafta_sonu": (hafta_basi + timedelta(days=7)) if hafta_basi else None,
            "siralama": siralama,
            "lider": siralama[0] if siralama else None
        })

    sonuc.sort(key=lambda x: x["hafta_basi"] or datetime.min, reverse=True)
    return sonuc


def su_anki_hafta_liderligi_hesapla(grup_id, hafta_basi, hafta_sonu):
    """
    "Bu Haftanın Lider Tablosu" için: TÜM takip edilen liglerin (Şampiyonlar
    Ligi + Süper Lig) o an içinde bulunduğumuz takvim haftasındaki (Pazartesi'den
    Pazar'a) maçlarından gelen puanları TEK bir tabloda birleştirir — ayrı ayrı
    lig tabloları göstermek yerine. Grubun TÜM üyelerini (hiç tahmin girmemiş
    olsa bile) 0 puanla dahil eder ki yeni hafta başladığında herkes gerçekten
    0'dan başlasın.
    """
    members = (GrupUyeligi.query.options(db.joinedload(GrupUyeligi.kullanici))
               .filter_by(grup_id=grup_id).all())
    kullanici_map = {u.kullanici_id: u.kullanici for u in members}
    puanlar = {uid: 0 for uid in kullanici_map}

    kayitlar = (
        db.session.query(Tahmin.kullanici_id, Tahmin.puan)
        .join(Mac, Tahmin.mac_id == Mac.id)
        .filter(
            Tahmin.grup_id == grup_id,
            db.or_(Mac.lig_id.in_(gosterilecek_lig_id_listesi()), Mac.ozel_grup_id == grup_id),
            Mac.baslangic >= hafta_basi,
            Mac.baslangic < hafta_sonu,
        )
        .all()
    )
    for uid, puan in kayitlar:
        if uid in puanlar:
            puanlar[uid] += (puan or 0)
    for uid, ceza in ceza_toplamlari(grup_id=grup_id, bas=hafta_basi, son=hafta_sonu).items():
        if uid in puanlar:
            puanlar[uid] += ceza

    return sorted(
        [{"user": kullanici_map[uid], "points": puan} for uid, puan in puanlar.items()],
        key=_haftalik_sira_anahtari,
        reverse=True,
    )


@app.route("/grup/<int:grup_id>")
@login_required
def grup(grup_id):
    grup_obj = db.session.get(Grup, grup_id)
    if not grup_obj or not GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first():
        abort(403)
    session["son_grup"] = grup_id
    gruplarim = [u.grup for u in GrupUyeligi.query.options(db.joinedload(GrupUyeligi.grup))
                 .filter_by(kullanici_id=current_user.id).order_by(GrupUyeligi.katilma_tarihi.asc()).all() if u.grup]

    now = utc_now_naive()

    # Takip edilen her lig için bir sonraki maçı bulup o ligin "aktif haftası"nı
    # belirliyoruz; birden fazla lig aynı anda gösterilebiliyor (örn. Şampiyonlar
    # Ligi + Süper Lig).
    # SADECE BU HAFTANIN maçları: bir ligin sıradaki maçı önümüzdeki
    # HAFTA_PENCERESI_GUN gün içinde değilse o lig bu hafta boş gösterilir
    # (örn. Uluslar Ligi'nin bir sonraki maçı 1 ay sonraysa listelenmez).
    maclar, aktif_haftalar, sonraki_mac_tarihleri, pencere_sonu = aktif_hafta_maclari(now)

    # Lig sekmeleri: grup sayfasının üstünde her lig için ayrı bir buton.
    lig_sekmeleri = []
    for kod, bilgi in LEAGUES.items():
        if bilgi["id"] not in gosterilecek_lig_id_listesi():
            continue
        lig_maclari = [m for m in maclar if m.lig_id == bilgi["id"]]
        lig_sekmeleri.append({
            "id": bilgi["id"],
            "kod": kod,
            "ad": LIG_KISA_ADLARI.get(bilgi["id"], bilgi["ad"]),
            "maclar": lig_maclari,
            "hafta": None if bilgi["id"] == AVRUPA_ID else aktif_haftalar.get(bilgi["id"]),
            "sonraki_mac": sonraki_mac_tarihleri.get(bilgi["id"]),
            "acik_mac": sum(1 for m in lig_maclari if m.oynanabilir_mi()),
        })
    # Gruba özel (elle eklenen) maçlar: henüz bitmemişler + son 1 günde bitenler
    ozel_maclar = (Mac.query.filter(
        Mac.ozel_grup_id == grup_id, Mac.baslangic.isnot(None),
        db.or_(Mac.durum != "bitti", Mac.baslangic >= now - timedelta(days=1)),
    ).order_by(Mac.baslangic.asc()).all())
    if ozel_maclar:
        maclar = list(maclar) + ozel_maclar
        lig_sekmeleri.append({
            "id": OZEL_LIG_ID, "kod": "ozel", "ad": "Özel maçlar", "maclar": ozel_maclar,
            "hafta": None, "sonraki_mac": None,
            "acik_mac": sum(1 for m in ozel_maclar if m.oynanabilir_mi()),
        })
    # Varsayılan açık sekme: tahmine açık maçı en yakın olan lig.
    def _ilk_acik_mac(sekme):
        acik = [m.baslangic for m in sekme["maclar"] if m.oynanabilir_mi() and m.baslangic]
        return min(acik) if acik else datetime.max
    dolu_sekmeler = [s for s in lig_sekmeleri if s["maclar"]]
    if dolu_sekmeler:
        varsayilan_sekme = min(dolu_sekmeler, key=_ilk_acik_mac)["kod"]
    else:
        varsayilan_sekme = lig_sekmeleri[0]["kod"] if lig_sekmeleri else None

    # Geriye dönük uyumluluk: şablon tek bir "aktif_hafta" bekliyorsa
    # (örn. Şampiyonlar Ligi'ninkini) onu da gönderiyoruz.
    aktif_hafta = aktif_haftalar.get(CHAMPIONS_LEAGUE_ID) or next(iter(aktif_haftalar.values()), None)

    # Şablonda her ligin kendi hafta bilgisini AYRI AYRI gösterebilmek için
    # (örn. "Şampiyonlar Ligi bu hafta yok ama Süper Lig Hafta 5" durumunda
    # kafa karışmasın diye) lig adı + hafta çiftlerini de gönderiyoruz.
    lig_id_to_ad = {bilgi["id"]: bilgi["ad"] for bilgi in LEAGUES.values()}
    aktif_haftalar_liste = [
        {"lig_adi": lig_id_to_ad.get(lig_id, "Lig"), "hafta": hafta}
        for lig_id, hafta in aktif_haftalar.items()
    ]

    # Üyeler kullanıcı bilgileriyle TEK sorguda (her üye için ayrı sorgu atılmasın)
    members = (GrupUyeligi.query.options(db.joinedload(GrupUyeligi.kullanici))
               .filter_by(grup_id=grup_id).all())

    # Grubun tüm tahminleri TEK sorguda; hem kendi tahminlerim hem matris hem
    # sezon toplamları buradan hesaplanır.
    grup_tahmin_matrisi = {}
    tahmin_dict = {}
    toplam_puanlar = {}
    for t in Tahmin.query.filter_by(grup_id=grup_id).all():
        grup_tahmin_matrisi.setdefault(t.mac_id, {})[t.kullanici_id] = t
        toplam_puanlar[t.kullanici_id] = toplam_puanlar.get(t.kullanici_id, 0) + (t.puan or 0)
        if t.kullanici_id == current_user.id:
            tahmin_dict[t.mac_id] = t

    for uid, ceza in ceza_toplamlari(grup_id=grup_id).items():
        toplam_puanlar[uid] = toplam_puanlar.get(uid, 0) + ceza

    # Sezonluk / tüm zamanlar genel sıralama (kümülatif toplam puan)
    leaderboard = sorted([
        {"user": m.kullanici, "points": int(toplam_puanlar.get(m.kullanici_id, 0))}
        for m in members
    ], key=lambda x: x["points"], reverse=True)

    # Bu haftanın lider tablosu: hangi ligden olursa olsun (Şampiyonlar Ligi +
    # Süper Lig birlikte, ayrı ayrı DEĞİL), gerçek takvim haftasındaki (Pazartesi–
    # Pazar) maçların puanları TEK bir tabloda. Yeni hafta başladığında (o
    # haftanın maçları henüz oynanmamış olsa bile) herkes gerçekten 0'dan başlar.
    hafta_basi, hafta_sonu = bu_haftanin_araligi(now)
    su_anki_hafta_siralamasi = su_anki_hafta_liderligi_hesapla(grup_id, hafta_basi, hafta_sonu)
    oynanabilir_mac_var = any(m.oynanabilir_mi() for m in maclar)

    return render_template(
        "grup.html",
        grup=grup_obj,
        gruplarim=gruplarim,
        maclar=maclar,
        lig_sekmeleri=lig_sekmeleri,
        mesaj_sayisi=okunmamis_mesaj_sayisi(grup_id, current_user.id),
        varsayilan_sekme=varsayilan_sekme,
        aktif_hafta=aktif_hafta,
        aktif_haftalar_liste=aktif_haftalar_liste,
        tahminler=tahmin_dict,
        members=members,
        grup_tahmin_matrisi=grup_tahmin_matrisi,
        leaderboard=leaderboard,
        su_anki_hafta_siralamasi=su_anki_hafta_siralamasi,
        hafta_basi=hafta_basi,
        hafta_sonu=hafta_sonu,
        oynanabilir_mac_var=oynanabilir_mac_var,
        paylas_haftalar=_guvenli_paylas_haftalar(grup_id),
        ozel_mac_yetkisi=ozel_mac_yetkili_mi(grup_obj),
    )


def _guvenli_paylas_haftalar(grup_id):
    try:
        return paylasilabilir_haftalar(grup_id)
    except Exception as e:
        db.session.rollback()
        print("[kral-karti] hata:", e)
        return []


def _grup_uyesi_mi(grup_id):
    return GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first() is not None


@app.route("/grup/<int:grup_id>/sohbet")
@login_required
def grup_sohbet(grup_id):
    grup_obj = db.session.get(Grup, grup_id)
    if not grup_obj or not _grup_uyesi_mi(grup_id):
        abort(403)
    son_mesajlar = (Mesaj.query.filter_by(grup_id=grup_id)
                    .order_by(Mesaj.id.desc()).limit(SOHBET_SAYFA_BOYU).all())
    son_mesajlar.reverse()
    if son_mesajlar:
        sohbet_okundu_isaretle(grup_id, current_user.id, son_mesajlar[-1].id)
    uyeler = (GrupUyeligi.query.options(db.joinedload(GrupUyeligi.kullanici))
              .filter_by(grup_id=grup_id).all())
    uye_sayisi = len(uyeler)
    return render_template(
        "sohbet.html",
        uyeler=[{"id": u.kullanici_id, "ad": u.kullanici.kullanici_adi} for u in uyeler if u.kullanici],
        grup=grup_obj,
        mesajlar=[m.to_dict() for m in son_mesajlar],
        uye_sayisi=uye_sayisi,
        max_uzunluk=MESAJ_MAX_UZUNLUK,
    )


@app.route("/grup/<int:grup_id>/sohbet/mesajlar")
@login_required
def grup_sohbet_mesajlar(grup_id):
    """Yeni mesajları getirir (sayfa birkaç saniyede bir bunu çağırır)."""
    if not _grup_uyesi_mi(grup_id):
        abort(403)
    sonra = request.args.get("sonra", type=int, default=0)
    yeni = (Mesaj.query.filter(Mesaj.grup_id == grup_id, Mesaj.id > sonra)
            .order_by(Mesaj.id.asc()).limit(100).all())
    if yeni:
        sohbet_okundu_isaretle(grup_id, current_user.id, yeni[-1].id)
    # Silinen mesajları istemcinin de kaldırabilmesi için görünür son id'leri gönder
    mevcut_idler = [mid for (mid,) in db.session.query(Mesaj.id)
                    .filter(Mesaj.grup_id == grup_id)
                    .order_by(Mesaj.id.desc()).limit(SOHBET_SAYFA_BOYU).all()]
    return jsonify({"mesajlar": [m.to_dict() for m in yeni], "mevcut_idler": mevcut_idler})


@app.route("/grup/<int:grup_id>/sohbet/gonder", methods=["POST"])
@login_required
def grup_sohbet_gonder(grup_id):
    if not _grup_uyesi_mi(grup_id):
        abort(403)
    veri = request.get_json(silent=True) or {}
    icerik = (veri.get("icerik") or request.form.get("icerik") or "").strip()
    if not icerik:
        return jsonify({"hata": "Boş mesaj gönderilemez."}), 400
    if len(icerik) > MESAJ_MAX_UZUNLUK:
        return jsonify({"hata": f"Mesaj en fazla {MESAJ_MAX_UZUNLUK} karakter olabilir."}), 400

    son = (Mesaj.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id)
           .order_by(Mesaj.id.desc()).first())
    if son and (datetime.utcnow() - son.olusturulma).total_seconds() < SOHBET_MIN_ARALIK_SN:
        return jsonify({"hata": "Biraz yavaş 🙂"}), 429

    yanit_id = veri.get("yanit_id")
    if yanit_id not in (None, ""):
        try:
            yanit_id = int(yanit_id)
        except (TypeError, ValueError):
            return jsonify({"hata": "Geçersiz yanıt."}), 400
        hedef = db.session.get(Mesaj, yanit_id)
        if not hedef or hedef.grup_id != grup_id:
            return jsonify({"hata": "Yanıtlanan mesaj bulunamadı."}), 400
    else:
        yanit_id = None

    mesaj = Mesaj(grup_id=grup_id, kullanici_id=current_user.id, icerik=icerik, yanit_id=yanit_id)
    db.session.add(mesaj)
    db.session.commit()
    sohbet_okundu_isaretle(grup_id, current_user.id, mesaj.id)
    return jsonify({"mesaj": mesaj.to_dict()})


@app.route("/grup/<int:grup_id>/sohbet/sil/<int:mesaj_id>", methods=["POST"])
@login_required
def grup_sohbet_sil(grup_id, mesaj_id):
    mesaj = db.session.get(Mesaj, mesaj_id)
    if not mesaj or mesaj.grup_id != grup_id:
        abort(404)
    grup_obj = db.session.get(Grup, grup_id)
    yetkili = (mesaj.kullanici_id == current_user.id or current_user.admin
               or (grup_obj and grup_obj.kurucu_id == current_user.id))
    if not yetkili:
        abort(403)
    db.session.delete(mesaj)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/grup/<int:grup_id>/tahminleri-kaydet", methods=["POST"])
@login_required
def tahminleri_kaydet(grup_id):
    """
    Kullanıcı, o haftanın TÜM maçlarına tek bir formda tahmin girip tek bir
    'Tahminleri Kaydet' butonuyla hepsini birden kaydeder — her maç için
    ayrı ayrı kaydetmesi gerekmez.
    """
    membership = GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first()
    if not membership:
        abort(403)

    kaydedilen = 0
    atlanan = 0
    grup_idleri = kullanicinin_grup_idleri(current_user.id)
    coklu = len(grup_idleri) > 1

    for key in list(request.form.keys()):
        if not key.startswith("ev_skor_"):
            continue

        mac_id_str = key[len("ev_skor_"):]
        if not mac_id_str.isdigit():
            continue
        mac_id = int(mac_id_str)

        ev_raw = request.form.get(f"ev_skor_{mac_id}", "").strip()
        dep_raw = request.form.get(f"deplasman_skor_{mac_id}", "").strip()

        # Kullanıcı bu maça hiç dokunmadıysa (boş bıraktıysa) sessizce atla,
        # diğer maçlar için girilen tahminleri yine de kaydet.
        if ev_raw == "" and dep_raw == "":
            continue

        try:
            ev_skor = int(ev_raw)
            deplasman_skor = int(dep_raw)
        except (ValueError, TypeError):
            atlanan += 1
            continue

        if ev_skor < 0 or deplasman_skor < 0:
            atlanan += 1
            continue

        mac = db.session.get(Mac, mac_id)
        if not mac or not mac.oynanabilir_mi() or (mac.ozel_grup_id and mac.ozel_grup_id not in grup_idleri):
            atlanan += 1
            continue

        tahmini_tum_gruplara_yaz(current_user.id, mac_id, ev_skor, deplasman_skor, grup_idleri)
        kaydedilen += 1

    db.session.commit()

    if kaydedilen and atlanan:
        flash(f"{kaydedilen} maç için tahmininiz kaydedildi, {atlanan} maç atlandı (başlamış olabilir).", "success")
    elif kaydedilen:
        ek = f" Tüm gruplarına ({len(grup_idleri)}) yansıdı." if coklu else ""
        flash(f"{kaydedilen} maç için tahmininiz kaydedildi. 🎯{ek}", "success")
    else:
        flash("Kaydedilecek yeni bir tahmin bulunamadı.", "warning")

    return redirect(url_for("grup", grup_id=grup_id))


@app.route("/tahmin/<int:grup_id>/<int:mac_id>", methods=["GET", "POST"])
@login_required
def tahmin(grup_id, mac_id):
    """Tek bir maç için tahmin girme/güncelleme sayfası (örn. geçmiş sayfasından tekil düzenleme için)."""
    membership = GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first()
    mac = db.session.get(Mac, mac_id)
    if not membership or not mac or (mac.ozel_grup_id and mac.ozel_grup_id != grup_id):
        abort(403)

    if not mac.oynanabilir_mi():
        flash("Bu maç için artık tahmin yapılamaz.", "warning")
        return redirect(url_for("grup", grup_id=grup_id))

    existing = Tahmin.query.filter_by(grup_id=grup_id, mac_id=mac_id, kullanici_id=current_user.id).first()

    if request.method == "POST":
        try:
            ev_skor = int(request.form.get("ev_skor"))
            deplasman_skor = int(request.form.get("deplasman_skor"))
        except Exception:
            flash("Skorları doğru girin.", "danger")
            return redirect(url_for("tahmin", grup_id=grup_id, mac_id=mac_id))

        if ev_skor < 0 or deplasman_skor < 0:
            flash("Skor negatif olamaz.", "danger")
            return redirect(url_for("tahmin", grup_id=grup_id, mac_id=mac_id))

        tahmini_tum_gruplara_yaz(current_user.id, mac_id, ev_skor, deplasman_skor)
        db.session.commit()
        flash("Tahmininiz kaydedildi.", "success")
        return redirect(url_for("grup", grup_id=grup_id))

    return render_template("tahmin.html", grup=membership.grup, mac=mac, tahmin=existing)


# ------------------------------------------------------------
# GRUBA ÖZEL MAÇ: sadece admin, takip edilen liglerde olmayan
# bir maçı sadece kendi grubuna ekler, sonucunu da kendisi girer.
# Puanlama aynı kurallarla yapılır ve sadece o grubun tablolarına yansır.
# ------------------------------------------------------------
def ozel_mac_yetkili_mi(grup_obj):
    """Özel maç ekleme / sonuç girme / silme SADECE admin."""
    return bool(current_user.is_authenticated and grup_obj and getattr(current_user, "admin", False))


def _ozel_mac_grubu(grup_id):
    grup_obj = db.session.get(Grup, grup_id)
    if not grup_obj or not ozel_mac_yetkili_mi(grup_obj):
        abort(403)
    return grup_obj


@app.route("/grup/<int:grup_id>/ozel-maclar", methods=["GET", "POST"])
@login_required
def grup_ozel_maclar(grup_id):
    grup_obj = _ozel_mac_grubu(grup_id)
    if request.method == "POST":
        ev = " ".join((request.form.get("ev_sahibi") or "").split())[:100]
        dep = " ".join((request.form.get("deplasman") or "").split())[:100]
        etiket = " ".join((request.form.get("etiket") or "").split())[:60] or "Özel maç"
        try:
            tr_saat = datetime.strptime(request.form.get("baslangic") or "", "%Y-%m-%dT%H:%M")
        except ValueError:
            tr_saat = None
        if len(ev) < 2 or len(dep) < 2:
            flash("İki takımın adını da yaz.", "warning")
        elif not tr_saat:
            flash("Maçın tarih ve saatini seç.", "warning")
        elif tr_saat - TURKIYE_UTC_OFFSET <= utc_now_naive():
            flash("Başlama saati ileride olmalı; geçmiş bir maça tahmin girilemez.", "warning")
        else:
            import random as _rnd
            while True:
                ext = -_rnd.randint(1, 2_000_000_000)
                if not Mac.query.filter_by(external_id=ext).first():
                    break
            mac = Mac(external_id=ext, ev_sahibi=ev, deplasman=dep, lig=etiket, lig_id=OZEL_LIG_ID,
                      baslangic=tr_saat - TURKIYE_UTC_OFFSET, durum="planlandi", api_status="MANUEL",
                      ozel_grup_id=grup_obj.id, son_guncelleme=utc_now_naive())
            db.session.add(mac)
            db.session.commit()
            flash(f"Eklendi: {ev} – {dep}. Sadece {grup_obj.ad} grubunda görünür.", "success")
            return redirect(url_for("grup_ozel_maclar", grup_id=grup_id))
    maclar = Mac.query.filter_by(ozel_grup_id=grup_id).order_by(Mac.baslangic.desc()).all()
    tahmin_sayilari = dict(db.session.query(Tahmin.mac_id, db.func.count(Tahmin.id))
                           .filter(Tahmin.mac_id.in_([m.id for m in maclar] or [-1]))
                           .group_by(Tahmin.mac_id).all())
    varsayilan = (utc_now_naive() + TURKIYE_UTC_OFFSET + timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    return render_template("ozel_maclar.html", grup=grup_obj, maclar=maclar, tahmin_sayilari=tahmin_sayilari,
                           varsayilan_saat=varsayilan.strftime("%Y-%m-%dT%H:%M"),
                           form=request.form if request.method == "POST" else {})


@app.route("/grup/<int:grup_id>/ozel-maclar/<int:mac_id>/sonuc", methods=["POST"])
@login_required
def grup_ozel_mac_sonuc(grup_id, mac_id):
    _ozel_mac_grubu(grup_id)
    mac = db.session.get(Mac, mac_id)
    if not mac or mac.ozel_grup_id != grup_id:
        abort(404)
    if not mac.basladi_mi():
        flash("Sonuç ancak maç başladıktan sonra girilebilir.", "warning")
        return redirect(url_for("grup_ozel_maclar", grup_id=grup_id))
    try:
        ev, dep = int(request.form.get("ev_skor")), int(request.form.get("deplasman_skor"))
        if ev < 0 or dep < 0 or ev > 30 or dep > 30:
            raise ValueError
    except (TypeError, ValueError):
        flash("Skoru doğru gir.", "warning")
        return redirect(url_for("grup_ozel_maclar", grup_id=grup_id))
    mac.ev_skor, mac.deplasman_skor = ev, dep
    mac.durum, mac.api_status, mac.son_guncelleme = "bitti", "MANUEL", utc_now_naive()
    db.session.commit()
    recalculate_all_points()
    flash(f"Sonuç kaydedildi: {mac.ev_sahibi} {ev}–{dep} {mac.deplasman}. Puanlar hesaplandı.", "success")
    return redirect(url_for("grup_ozel_maclar", grup_id=grup_id))


@app.route("/grup/<int:grup_id>/ozel-maclar/<int:mac_id>/sil", methods=["POST"])
@login_required
def grup_ozel_mac_sil(grup_id, mac_id):
    _ozel_mac_grubu(grup_id)
    mac = db.session.get(Mac, mac_id)
    if not mac or mac.ozel_grup_id != grup_id:
        abort(404)
    ad = f"{mac.ev_sahibi} – {mac.deplasman}"
    TahminCezasi.query.filter_by(mac_id=mac.id).delete()
    db.session.delete(mac)      # tahminler de birlikte silinir
    db.session.commit()
    recalculate_all_points()
    flash(f"Silindi: {ad}. Bu maçtan gelen puanlar tablolardan çıkarıldı.", "success")
    return redirect(url_for("grup_ozel_maclar", grup_id=grup_id))


@app.route("/grup/<int:grup_id>/haftalik-liderlik")
@login_required
def grup_haftalik_liderlik(grup_id):
    """Her haftanın (ve her ligin) kendi lider tablosunu ayrı ayrı gösteren arşiv sayfası."""
    grup_obj = db.session.get(Grup, grup_id)
    if not grup_obj or not GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first():
        abort(403)

    haftalik_liderlik = haftalik_liderlik_hesapla(grup_id)
    return render_template("haftalik_liderlik.html", grup=grup_obj, haftalik_liderlik=haftalik_liderlik)


@app.route("/grup/<int:grup_id>/gecmis-maclar")
@login_required
def grup_gecmis_maclar(grup_id):
    """
    Kullanıcının, grubundaki diğer üyelerin GEÇMİŞ (oynanmış/bitmiş) maçlara
    girdiği tahminleri görebildiği sayfa.
    """
    grup_obj = db.session.get(Grup, grup_id)
    if not grup_obj or not GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first():
        abort(403)

    bitmis_maclar = Mac.query.filter(
        db.or_(db.and_(Mac.lig_id.in_(gosterilecek_lig_id_listesi()), super_lig_takim_filtresi()),
               Mac.ozel_grup_id == grup_id),
        Mac.durum == "bitti",
    ).order_by(Mac.baslangic.desc()).limit(100).all()

    members = GrupUyeligi.query.options(db.joinedload(GrupUyeligi.kullanici)).filter_by(grup_id=grup_id).all()

    tahmin_matrisi = {}
    bitmis_mac_idleri = [m.id for m in bitmis_maclar]
    if bitmis_mac_idleri:
        for t in Tahmin.query.filter(Tahmin.grup_id == grup_id, Tahmin.mac_id.in_(bitmis_mac_idleri)).all():
            tahmin_matrisi.setdefault(t.mac_id, {})[t.kullanici_id] = t

    # Hiç kimsenin tahmin yapmadığı maçları geçmiş listesinden gizliyoruz —
    # hafta sayısı arttıkça sayfa gereksiz yere kalabalıklaşmasın diye.
    bitmis_maclar = [m for m in bitmis_maclar if tahmin_matrisi.get(m.id)]

    return render_template(
        "gecmis_maclar.html",
        grup=grup_obj,
        maclar=bitmis_maclar,
        members=members,
        tahmin_matrisi=tahmin_matrisi
    )


@app.route("/profil")
@login_required
def profil():
    total_points = (db.session.query(db.func.coalesce(db.func.sum(Tahmin.puan), 0)).filter(Tahmin.kullanici_id == current_user.id).scalar() or 0) \
        + ceza_toplamlari(kullanici_id=current_user.id).get(current_user.id, 0)
    prediction_count = Tahmin.query.filter_by(kullanici_id=current_user.id).count()
    return render_template("profil.html", toplam_puan=total_points, toplam_tahmin=prediction_count)


@app.route("/profil/sifre", methods=["GET", "POST"])
@login_required
def sifre_degistir():
    """Kullanıcı kendi şifresini değiştirir. Yönetici geçici şifre verdiyse
    (sifre_degismeli) mevcut şifre sorulmaz; kullanıcı zaten onunla giriş yaptı."""
    zorunlu = bool(current_user.sifre_degismeli)
    if request.method == "POST":
        mevcut = request.form.get("mevcut_sifre", "")
        yeni = request.form.get("yeni_sifre", "")
        tekrar = request.form.get("yeni_sifre_tekrar", "")
        if not zorunlu and not current_user.check_password(mevcut):
            flash("Mevcut şifren hatalı.", "danger")
        elif len(yeni) < 6:
            flash("Yeni şifre en az 6 karakter olmalı.", "danger")
        elif yeni != tekrar:
            flash("Yeni şifreler birbirini tutmuyor.", "danger")
        elif current_user.check_password(yeni):
            flash("Yeni şifre eskisiyle aynı olamaz.", "danger")
        else:
            current_user.set_password(yeni)
            current_user.sifre_degismeli = False
            db.session.commit()
            flash("Şifren güncellendi.", "success")
            return redirect(url_for("dashboard") if zorunlu else url_for("profil"))
    return render_template("sifre_degistir.html", zorunlu=zorunlu)


@app.before_request
def gecici_sifreyi_degistirt():
    """Geçici şifreyle giren kullanıcı yeni şifresini belirlemeden başka sayfaya geçemez."""
    if not current_user.is_authenticated or not getattr(current_user, "sifre_degismeli", False):
        return None
    if request.endpoint in ("sifre_degistir", "cikis", "static", "destek", "destek_mesajlar", "ads_txt", "robots_txt") \
            or (request.endpoint or "").startswith("cron"):
        return None
    if request.path.startswith("/cron"):
        return None
    return redirect(url_for("sifre_degistir"))


@app.route("/admin")
@admin_required
def admin():
    kullanici_sayisi = Kullanici.query.count()
    grup_sayisi = Grup.query.count()
    mac_sayisi = Mac.query.count()
    cl_mac_sayisi = Mac.query.filter(Mac.lig_id == CHAMPIONS_LEAGUE_ID).count()
    super_lig_mac_sayisi = Mac.query.filter(Mac.lig_id == SUPER_LIG_ID).count()
    uluslar_ligi_mac_sayisi = Mac.query.filter(Mac.lig_id == NATIONS_LEAGUE_ID).count()
    tahmin_sayisi = Tahmin.query.count()
    upcoming_matches = Mac.query.filter(
        Mac.baslangic.isnot(None), Mac.baslangic > utc_now_naive(),
        Mac.lig_id.in_(gosterilecek_lig_id_listesi()),
        ~Mac.durum.in_(["bitti", "iptal", "ertelendi"])
    ).count()
    return render_template(
        "admin.html",
        kullanici_sayisi=kullanici_sayisi,
        grup_sayisi=grup_sayisi,
        mac_sayisi=mac_sayisi,
        cl_mac_sayisi=cl_mac_sayisi,
        super_lig_mac_sayisi=super_lig_mac_sayisi,
        uluslar_ligi_mac_sayisi=uluslar_ligi_mac_sayisi,
        tahmin_sayisi=tahmin_sayisi,
        # eski isimler de duruyor, başka bir yerde kullanan varsa bozulmasın
        total_users=kullanici_sayisi,
        total_groups=grup_sayisi,
        total_matches=mac_sayisi,
        total_predictions=tahmin_sayisi,
        upcoming_matches=upcoming_matches,
        ziya=_ziya_admin_ozeti(),
        sifre_talepleri=bekleyen_sifre_talepleri(),
        ziyaret=_guvenli_ziyaret_ozeti(),
    )


def _guvenli_ziyaret_ozeti():
    try:
        bugun = (utc_now_naive() + TURKIYE_UTC_OFFSET).date()
        return {"bugun": ZiyaretGunluk.query.filter_by(gun=bugun).count(),
                "su_an": ZiyaretGunluk.query.filter(ZiyaretGunluk.son_saat >= utc_now_naive() - timedelta(minutes=5)).count()}
    except Exception:
        db.session.rollback()
        return None


def _ziya_admin_ozeti():
    """Admin kartı için: Robot Ziya'nın yaklaşan maçlardaki tahmin durumu."""
    try:
        simdi = utc_now_naive()
        yz = yz_kullanici_getir(olustur=False)
        bekleyen = 0
        if yz:
            bekleyen = (db.session.query(db.func.count(db.distinct(Tahmin.mac_id)))
                        .join(Mac, Tahmin.mac_id == Mac.id)
                        .filter(Tahmin.kullanici_id == yz.id, Mac.baslangic > simdi).scalar() or 0)
        son = _senkron_zamani_oku("yz")
        return {
            "kaynak": f"Gemini ({GEMINI_MODEL})" if GEMINI_API_KEY else "İstatistik modeli (GEMINI_API_KEY tanımlı değil)",
            "teshis": yz_teshis(),
            "ad": YZ_KULLANICI_ADI,
            "tahminli_mac": bekleyen,
            "son_calisma": to_turkiye_saati(son).strftime("%d.%m.%Y %H:%M") if son else "Henüz çalışmadı",
            "sonraki": (yz_son_tahmin_ani(simdi) + timedelta(days=7, hours=3)).strftime("%d.%m.%Y %H:%M"),
        }
    except Exception:
        db.session.rollback()
        return None


@app.route("/admin/ziya-tahminlesin", methods=["POST"])
@admin_required
def admin_ziya_tahminlesin():
    """Robot Ziya'yı Perşembe'yi beklemeden şimdi çalıştırır (bu haftanın maçları)."""
    sonuc = _yz_guvenli_calistir(zorla=True)
    if sonuc.get("success") is False:
        flash(f"{YZ_KULLANICI_ADI} tahmin yapamadı: {sonuc.get('error')}", "danger")
    elif sonuc.get("yeni_tahmin"):
        flash(f"{YZ_KULLANICI_ADI} tahminlerini girdi: {sonuc['yeni_tahmin']} yeni tahmin"
              f" ({sonuc.get('gruba_eklendi', 0)} gruba yeni katıldı, kaynak: {sonuc.get('kaynak', '-')}).", "success")
    else:
        flash(f"{YZ_KULLANICI_ADI} için yeni tahmin yok: önümüzdeki maçların hepsine zaten tahmin girmiş"
              f" ya da fikstürde yaklaşan maç yok.", "warning")
    return redirect(url_for("admin"))


@app.route("/admin/haberleri-yenile", methods=["POST"])
@admin_required
def admin_haberleri_yenile():
    sonuc = _haber_guvenli_guncelle(zorla=True)
    if sonuc.get("success") is False:
        flash(f"Haberler alınamadı: {sonuc.get('error')}", "danger")
    else:
        hata = f" Ulaşılamayan kaynaklar: {'; '.join(sonuc.get('hatalar') or [])}" if sonuc.get("hatalar") else ""
        flash(f"Haberler yenilendi: {sonuc.get('eklenen', 0)} yeni haber.{hata}", "success" if not hata else "warning")
    return redirect(url_for("admin"))


@app.route("/admin/ziya-yenile", methods=["POST"])
@admin_required
def admin_ziya_yenile():
    """Ziya'nın başlamamış maçlardaki tahminlerini silip yeniden tahmin ettirir."""
    try:
        sonuc = yz_tahminleri_yenile()
        flash(f"{YZ_KULLANICI_ADI} tahminleri yenilendi: {sonuc.get('silinen', 0)} eski tahmin silindi, "
              f"{sonuc.get('yeni_tahmin', 0)} yeni tahmin girildi (kaynak: {sonuc.get('kaynak', '-')}).", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"{YZ_KULLANICI_ADI} tahminleri yenilenemedi: {e}", "danger")
    return redirect(url_for("admin"))


@app.route("/admin/puanlari-sifirla", methods=["POST"])
@admin_required
def admin_puanlari_sifirla():
    """
    Haftalık ve sezonluk lider tabloları, Tahmin.puan alanının canlı
    toplamından hesaplanır (ayrı bir 'skor' tablosu yoktur). Bu yüzden
    puanları gerçekten '0'dan başlatmak için tüm mevcut Tahmin kayıtlarının
    puan alanını sıfırlıyoruz. Kullanıcıların girdiği skor tahminleri (kaç-
    kaç dedikleri) SİLİNMEZ, sadece kazandıkları puan 0 yapılır — yani geçmiş
    test/deneme haftalarının puanları artık hiçbir tabloda görünmez ama
    tahmin geçmişi durur.

    Kesme noktası olarak TAM O ANI değil, BUGÜNÜN BAŞLANGICINI (Türkiye
    saatiyle 00:00) kaydediyoruz. Böylece bugün oynanmış/oynanan bir maç
    varsa o maç normal şekilde puanlanmaya devam eder; sadece bugünden
    ÖNCEKİ maçların puanları kalıcı olarak 0'da kilitlenir. Bu fonksiyonu
    SADECE yeni sezon/dönem başlarken bir kez çalıştırın.
    """
    simdi_tr = to_turkiye_saati(utc_now_naive())
    bugun_baslangic_tr = simdi_tr.replace(hour=0, minute=0, second=0, microsecond=0)
    bugun_baslangic_utc = bugun_baslangic_tr - TURKIYE_UTC_OFFSET

    guncellenen = Tahmin.query.update({Tahmin.puan: 0})
    sistem = sistem_ayarlarini_getir()
    sistem.puan_sifirlama_tarihi = bugun_baslangic_utc
    db.session.commit()

    # Bugün zaten bitmiş bir maç varsa puanı hemen geri hesaplansın —
    # bir sonraki maç sonucu girilene kadar beklemeye gerek yok.
    recalculate_all_points()

    flash(
        f"{guncellenen} tahminin puanı sıfırlandı. Bugünden önce oynanan maçların puanı bir daha geri "
        f"gelmeyecek şekilde kilitlendi; bugün oynanan/oynanacak maçlar ise normal puanlanmaya devam ediyor.",
        "success",
    )
    return redirect(url_for("admin"))


@app.route("/admin/durum")
@admin_required
def admin_durum():
    mesajlar = []
    for kod, bilgi in LEAGUES.items():
        ornek_maclar = Mac.query.filter(Mac.lig_id == bilgi["id"]).order_by(Mac.baslangic.desc()).limit(5).all()
        toplam = Mac.query.filter(Mac.lig_id == bilgi["id"]).count()
        if not ornek_maclar:
            mesajlar.append(f"{bilgi['ad']}: DB'de hiç maç yok.")
        else:
            ornekler = "; ".join(f"{m.ev_sahibi}-{m.deplasman} ({m.durum}, hafta {m.hafta})" for m in ornek_maclar)
            mesajlar.append(f"{bilgi['ad']}: {toplam} maç. Son 5: {ornekler}")
    flash(" | ".join(mesajlar), "success")
    return redirect(url_for("admin"))


@app.route("/admin/sync", methods=["GET", "POST"])
@admin_required
def admin_sync():
    """Takip edilen TÜM ligleri (Şampiyonlar Ligi + Süper Lig) tek seferde senkronize eder."""
    sonuclar = sync_all_leagues()
    mesajlar = []
    for kod, sonuc in sonuclar.items():
        lig_adi = LEAGUES.get(kod, {}).get("ad", kod)
        if sonuc.get("success"):
            mesajlar.append(f"{lig_adi}: Yeni {sonuc['inserted']} | Güncellenen {sonuc['updated']}")
        else:
            mesajlar.append(f"{lig_adi}: HATA — {sonuc.get('error')}")
    kategori = "success" if all(s.get("success") for s in sonuclar.values()) else "warning"
    flash(" · ".join(mesajlar), kategori)
    return redirect(url_for("admin"))


@app.route("/admin/sync/<lig_kodu>", methods=["GET", "POST"])
@admin_required
def admin_sync_tek_lig(lig_kodu):
    """Tek bir ligi senkronize eder (örn. sadece Süper Lig'i tekrar denemek için)."""
    if lig_kodu not in LEAGUES:
        flash("Bilinmeyen lig kodu.", "danger")
        return redirect(url_for("admin"))
    try:
        result = sync_league(lig_kodu)
        lig_adi = LEAGUES[lig_kodu]["ad"]
        ek = f" | Rakip form maçı: {result['form_maclari']}" if "form_maclari" in result else ""
        flash(f"{lig_adi} senkronizasyonu tamamlandı. API: {result['api_matches']} | Yeni: {result['inserted']} | Güncellenen: {result['updated']}{ek}", "success")
    except Exception as e:
        db.session.rollback()
        ipucu = ""
        provider = LEAGUES.get(lig_kodu, {}).get("provider")
        if provider == "sportapi7":
            ipucu = " — Not: SportAPI7'nin ücretsiz RapidAPI planı aylık istek limitlidir (limit dolduysa bir süre bekleyin) ve SPORTAPI7_KEY'in doğru olduğundan emin olun."
        elif provider == "api-football":
            ipucu = " — Not: API-Football ücretsiz planı günde 100 istekle sınırlıdır; limit dolduysa bir süre sonra tekrar deneyin ve API_FOOTBALL_KEY'in doğru olduğundan emin olun."
        elif provider in ("espn", "espn-nations", "espn-avrupa"):
            ipucu = " — Not: ESPN uç noktası resmi/dokümante değildir, ESPN tarafında geçici bir sorun olabilir; birkaç dakika sonra tekrar deneyin."
        flash(f"Senkronizasyon hatası ({lig_kodu}): {e}{ipucu}", "danger")
    return redirect(url_for("admin"))


@app.route("/admin/mac-sonucu")
@admin_required
def admin_mac_sonucu():
    """SportAPI7 (ya da diğer sağlayıcılar) kota/hata yüzünden bir maçın
    sonucunu çekemediğinde, adminin sonucu elle girebilmesi için. Kaydedilen
    maç otomatik olarak 'bitti' durumuna alınır ve recalculate_all_points()
    ile TÜM kullanıcıların o maça ait tahmin puanları anında yeniden
    hesaplanır — elle skor girmek dışında hiçbir ek işlem gerekmez."""
    bekleyen_maclar = (
        Mac.query.filter(
            Mac.lig_id.in_(gosterilecek_lig_id_listesi()),
            Mac.durum != "bitti",
        )
        .order_by(Mac.baslangic.asc())
        .all()
    )
    tamamlanan_maclar = (
        Mac.query.filter(
            Mac.lig_id.in_(gosterilecek_lig_id_listesi()),
            Mac.durum == "bitti",
        )
        .order_by(Mac.baslangic.desc())
        .limit(30)
        .all()
    )
    muaf_mac_id_seti = {m.mac_id for m in MacMuafiyet.query.all()}
    elle_saatli_mac_id_seti = {d.mac_id for d in MacSaatDuzeltme.query.all()}
    return render_template(
        "admin_mac_sonucu.html",
        bekleyen_maclar=bekleyen_maclar,
        tamamlanan_maclar=tamamlanan_maclar,
        muaf_mac_id_seti=muaf_mac_id_seti,
        elle_saatli_mac_id_seti=elle_saatli_mac_id_seti,
    )


@app.route("/admin/mac-muaf/<int:mac_id>", methods=["POST"])
@admin_required
def admin_mac_muaf_degistir(mac_id):
    """
    Bir maçı "Puanları Sıfırla" butonundan MUAF tutar (ya da muafiyeti
    kaldırır). Muaf bir maç, sıfırlama tarihinden önce oynanmış olsa bile
    tahmin puanları normal şekilde hesaplanmaya devam eder.
    """
    mac = Mac.query.get_or_404(mac_id)
    mevcut = MacMuafiyet.query.get(mac_id)
    if mevcut:
        db.session.delete(mevcut)
        db.session.commit()
        flash(f"{mac.ev_sahibi} - {mac.deplasman} maçı artık sıfırlamadan etkilenecek.", "success")
    else:
        db.session.add(MacMuafiyet(mac_id=mac_id))
        db.session.commit()
        flash(f"{mac.ev_sahibi} - {mac.deplasman} maçı sıfırlamadan muaf tutuldu, puanı hep sayılacak.", "success")

    recalculate_all_points()
    return redirect(url_for("admin_mac_sonucu"))


@app.route("/admin/mac-saati/<int:mac_id>", methods=["POST"])
@admin_required
def admin_mac_saati_kaydet(mac_id):
    """
    Bir maçın başlangıç saatini elle değiştirir. Form saati TÜRKİYE saatiyle
    gelir (datetime-local input), veritabanına UTC olarak yazılır. Saat ayrıca
    MacSaatDuzeltme tablosuna kaydedilir ki API senkronu eski saati geri
    yazmasın. "sifirla" gönderilirse elle girilen saat kaldırılır ve bir
    sonraki senkronda API'deki saat geçerli olur.
    """
    mac = Mac.query.get_or_404(mac_id)
    duzeltme = db.session.get(MacSaatDuzeltme, mac_id)

    if request.form.get("sifirla"):
        if duzeltme:
            db.session.delete(duzeltme)
            db.session.commit()
        flash(
            f"{mac.ev_sahibi} - {mac.deplasman} maçının elle girilen saati kaldırıldı. "
            f"Bir sonraki senkronda API'deki saat kullanılacak.",
            "success",
        )
        return redirect(url_for("admin_mac_sonucu"))

    saat_raw = request.form.get("baslangic", "").strip()
    try:
        yeni_saat_tr = datetime.strptime(saat_raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Geçersiz tarih/saat.", "danger")
        return redirect(url_for("admin_mac_sonucu"))

    yeni_saat_utc = yeni_saat_tr - TURKIYE_UTC_OFFSET
    if duzeltme:
        duzeltme.baslangic = yeni_saat_utc
    else:
        db.session.add(MacSaatDuzeltme(mac_id=mac_id, baslangic=yeni_saat_utc))

    mac.baslangic = yeni_saat_utc
    # Ertelenmiş bir maça yeni saat verildiyse tekrar tahmine açılsın.
    if mac.durum == "ertelendi" and yeni_saat_utc > utc_now_naive():
        mac.durum = "planlandi"
    mac.son_guncelleme = utc_now_naive()
    db.session.commit()

    # Saat, puan sıfırlama kesim tarihine göre puanın sayılıp sayılmayacağını
    # belirlediği için (özellikle bitmiş maçlarda) puanları yeniden hesapla.
    recalculate_all_points()

    flash(
        f"{mac.ev_sahibi} - {mac.deplasman} maçının saati "
        f"{yeni_saat_tr.strftime('%d.%m.%Y %H:%M')} olarak güncellendi.",
        "success",
    )
    return redirect(url_for("admin_mac_sonucu"))


@app.route("/admin/mac-sonucu/<int:mac_id>", methods=["POST"])
@admin_required
def admin_mac_sonucu_kaydet(mac_id):
    mac = Mac.query.get_or_404(mac_id)

    ev_raw = request.form.get("ev_skor", "").strip()
    dep_raw = request.form.get("deplasman_skor", "").strip()
    try:
        ev_skor = int(ev_raw)
        deplasman_skor = int(dep_raw)
    except (TypeError, ValueError):
        flash("Geçersiz skor — lütfen iki takım için de bir tam sayı girin.", "danger")
        return redirect(url_for("admin_mac_sonucu"))

    if ev_skor < 0 or deplasman_skor < 0:
        flash("Skor negatif olamaz.", "danger")
        return redirect(url_for("admin_mac_sonucu"))

    mac.ev_skor = ev_skor
    mac.deplasman_skor = deplasman_skor
    mac.durum = "bitti"
    mac.api_status = "MANUEL"
    mac.son_guncelleme = utc_now_naive()
    db.session.commit()

    recalculate_all_points()

    flash(
        f"Kaydedildi: {mac.ev_sahibi} {ev_skor} - {deplasman_skor} {mac.deplasman}. "
        f"Tahmin puanları otomatik güncellendi.",
        "success",
    )
    return redirect(url_for("admin_mac_sonucu"))


@app.route("/admin/api-test")
@admin_required
def admin_api_test():
    """Her iki sağlayıcıyı (football-data.org + API-Football) ayrı ayrı test eder."""
    mesajlar = []

    for kod, bilgi in LEAGUES.items():
        provider = bilgi.get("provider")
        if provider == "sportapi7":
            if not SPORTAPI7_KEY:
                mesajlar.append(f"{bilgi['ad']}: HATA — SPORTAPI7_KEY tanımlı değil (env var eksik).")
                continue
            try:
                info = sportapi7_get(f"/unique-tournament/{SPORTAPI7_SUPER_LIG_TOURNAMENT_ID}/seasons")
                ilk_sezon = (info.get("seasons") or [{}])[0]
                mesajlar.append(f"{bilgi['ad']} (SportAPI7): OK (sezon: {ilk_sezon.get('name', '?')})")
            except Exception as e:
                mesajlar.append(f"{bilgi['ad']} (SportAPI7): HATA — {e}")
        elif provider == "espn-nations":
            try:
                info = espn_get(ESPN_NATIONS_LEAGUE_SLUG, "scoreboard", params={"limit": 5})
                lig_adi_espn = ((info.get("leagues") or [{}])[0]).get("name", "?")
                mesajlar.append(f"{bilgi['ad']} (ESPN): OK ({lig_adi_espn})")
            except Exception as e:
                mesajlar.append(f"{bilgi['ad']} (ESPN): HATA — {e}")
        elif provider == "espn-avrupa":
            for slug, kupa in ESPN_AVRUPA_SLUGLARI.items():
                try:
                    espn_get(slug, "scoreboard", params={"limit": 5})
                    mesajlar.append(f"{kupa} (ESPN): OK")
                except Exception as e:
                    mesajlar.append(f"{kupa} (ESPN): HATA — {e}")
        elif provider == "espn":
            try:
                info = espn_get(ESPN_SUPER_LIG_SLUG, "scoreboard", params={"limit": 5})
                lig_adi_espn = ((info.get("leagues") or [{}])[0]).get("name", "?")
                mesajlar.append(f"{bilgi['ad']} (ESPN): OK ({lig_adi_espn})")
            except Exception as e:
                mesajlar.append(f"{bilgi['ad']} (ESPN): HATA — {e}")
        elif provider == "api-football":
            if not API_FOOTBALL_KEY:
                mesajlar.append(f"{bilgi['ad']}: HATA — API_FOOTBALL_KEY tanımlı değil (env var eksik).")
                continue
            try:
                info = api_football_get("leagues", params={"id": API_FOOTBALL_SUPER_LIG_ID})
                sonuc = (info.get("response") or [{}])[0]
                isim = (sonuc.get("league") or {}).get("name", "?")
                sezonlar = sonuc.get("seasons") or []
                aktif = next((s.get("year") for s in sezonlar if s.get("current")), "?")
                mesajlar.append(f"{bilgi['ad']} (API-Football): OK ({isim}, sezon {aktif})")
            except Exception as e:
                mesajlar.append(f"{bilgi['ad']} (API-Football): HATA — {e}")
        else:
            if not FOOTBALL_DATA_TOKEN:
                mesajlar.append(f"{bilgi['ad']}: HATA — FOOTBALL_DATA_TOKEN tanımlı değil (env var eksik).")
                continue
            try:
                info = football_data_get(f"competitions/{kod}")
                isim = info.get("name", "?")
                sezon = (info.get("currentSeason") or {}).get("startDate", "?")
                mesajlar.append(f"{bilgi['ad']} (football-data.org): OK ({isim}, sezon {sezon})")
            except Exception as e:
                mesajlar.append(f"{bilgi['ad']} (football-data.org): HATA — {e}")

    flash(" | ".join(mesajlar), "success" if all("HATA" not in m for m in mesajlar) else "warning")
    return redirect(url_for("admin"))


TEAM_LOGO_NEGATIVE_CACHE_TTL_SANIYE = 300  # başarısız denemeleri sadece 5 dk hatırla

# TheSportsDB, SportAPI7/RapidAPI kotası dolduğunda devreye girmesi için
# eklenmişti; ANCAK TheSportsDB artık ücretsiz anahtarla sadece "Arsenal"
# aramasına izin veriyor (dokümantasyonlarına göre), yani bizim takımlarımız
# için pratikte hep boş dönüyor. Yine de zararsız olduğu ve ileride tekrar
# açılabileceği için kod duruyor — ama ASIL garanti çözüm aşağıdaki, projeye
# gömülü statik logo dosyaları (bkz. STATIC_TEAM_LOGO_ESLESTIRME). Bunlar
# hiçbir dış servise bağlı olmadığı için Sofascore/RapidAPI/TheSportsDB'nin
# ne durumda olduğuna bakılmaksızın HER ZAMAN gösterilir.
THESPORTSDB_SEARCH_URL = "https://www.thesportsdb.com/api/v1/json/3/searchteams.php"

# Süper Lig'de takip edilen 4 takım için dış servislere bağımlı olmayan,
# projeyle birlikte giden (static/logos/) yedek logolar. Anahtar,
# SUPER_LIG_TAKIP_EDILEN_TAKIMLAR ile aynı küçük harfli anahtar kelimeler.
STATIC_TEAM_LOGO_ESLESTIRME = {
    "fenerbahçe": "fenerbahce.svg",
    "fenerbahce": "fenerbahce.svg",
    "galatasaray": "galatasaray.svg",
    "beşiktaş": "besiktas.svg",
    "besiktas": "besiktas.svg",
    "trabzonspor": "trabzonspor.svg",
}


def _thesportsdb_logo_url(takim_adi):
    try:
        yanit = requests.get(THESPORTSDB_SEARCH_URL, params={"t": takim_adi}, timeout=LOGO_TIMEOUT)
        yanit.raise_for_status()
        takimlar = (yanit.json() or {}).get("teams") or []
        if not takimlar:
            return None
        return takimlar[0].get("strTeamBadge") or takimlar[0].get("strTeamLogo")
    except Exception:
        return None


def _statik_logo_dosyasi(takim_adi):
    if not takim_adi:
        return None
    adi_kucuk = takim_adi.lower()
    for anahtar, dosya in STATIC_TEAM_LOGO_ESLESTIRME.items():
        if anahtar in adi_kucuk:
            return dosya
    return None


# Takip edilen 4 takım dışındaki (ör. Şampiyonlar Ligi) takımların statik
# SVG dosyası yok; onlar için gerçek amblem (crest) boş gelirse ya da
# yüklenemezse ekranda HİÇBİR ŞEY görünmüyordu. Aşağıdaki palet + fonksiyon,
# HERHANGİ bir takım adı için hiçbir ağ isteği gerektirmeyen (data: URI),
# bu yüzden asla başarısız olmayan bir "baş harf rozeti" üretir. Takip
# edilen 4 takım için kulüp renkleri/kısaltması kullanılır, geri kalan
# HERKES (rakipler dahil) için isme göre sabit bir renk seçilir.
_GENEL_LOGO_RENK_PALETI = [
    ("#1d4ed8", "#ffffff"), ("#b91c1c", "#ffffff"), ("#047857", "#ffffff"),
    ("#7c3aed", "#ffffff"), ("#c2410c", "#ffffff"), ("#0f766e", "#ffffff"),
    ("#a21caf", "#ffffff"), ("#334155", "#ffffff"), ("#0369a1", "#ffffff"),
    ("#b45309", "#ffffff"), ("#166534", "#ffffff"), ("#9d174d", "#ffffff"),
]

# Takip edilen 4 takım için: (kısaltma, arka plan rengi, yazı rengi) —
# projeyle birlikte gelen eski static/logos/*.svg dosyalarıyla aynı
# renk/kısaltmalar, ama artık dosyaya değil doğrudan üretilen rozete gidiyor.
_BILINEN_TAKIM_ROZETI = {
    "fenerbahçe": ("FB", "#08195e", "#ffe000"),
    "fenerbahce": ("FB", "#08195e", "#ffe000"),
    "galatasaray": ("GS", "#a90432", "#fdb913"),
    "beşiktaş": ("BJK", "#000000", "#ffffff"),
    "besiktas": ("BJK", "#000000", "#ffffff"),
    "trabzonspor": ("TS", "#67213e", "#0072ce"),
}


def _takim_baslangic_harfleri(takim_adi):
    kelimeler = [k for k in (takim_adi or "?").split() if k]
    if not kelimeler:
        return "?"
    if len(kelimeler) == 1:
        return kelimeler[0][:2].upper()
    return (kelimeler[0][0] + kelimeler[1][0]).upper()


def genel_yedek_logo_data_uri(takim_adi):
    """Herhangi bir takım için (takip edilen 4 Süper Lig takımı VE
    rakipleri dahil HERKES için) her zaman çalışan yedek rozet — data: URI
    olduğu için hiçbir HTTP isteği yapmaz, dolayısıyla ağ/kota/CORS
    sorunlarından tamamen bağımsız, asla kırık görünmez. Takip edilen 4
    takım kendi kulüp renkleri/kısaltmasıyla, diğer herkes isme göre sabit
    bir renkle çizilir."""
    adi_kucuk = (takim_adi or "").lower()
    bilinen = next(
        (deger for anahtar, deger in _BILINEN_TAKIM_ROZETI.items() if anahtar in adi_kucuk),
        None,
    )
    if bilinen:
        harfler, arka_plan, yazi_rengi = bilinen
    else:
        harfler = _takim_baslangic_harfleri(takim_adi)
        index = int(hashlib.md5((takim_adi or "?").encode("utf-8")).hexdigest(), 16) % len(
            _GENEL_LOGO_RENK_PALETI
        )
        arka_plan, yazi_rengi = _GENEL_LOGO_RENK_PALETI[index]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<circle cx="32" cy="32" r="30" fill="{arka_plan}"/>'
        f'<text x="32" y="40" font-family="Arial, sans-serif" font-size="22" '
        f'font-weight="700" fill="{yazi_rengi}" text-anchor="middle">{harfler}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;utf8," + quote(svg)


def takim_logo_url(takim_adi, mevcut_logo=None):
    """Şablonlarda GERÇEK logonun (mac.ev_logo/deplasman_logo) yanında,
    <img onerror="..."> hedefi olarak kullanılır — bkz. templates/*.html.

    Amaç: HER takım için (takip edilen 4 Süper Lig takımı VE rakipleri,
    ayrıca Şampiyonlar Ligi kulüpleri dahil herkes için) önce gerçek
    amblemi göstermeyi denemek (SportAPI7/Sofascore, TheSportsDB ya da
    football-data.org crest'i üzerinden), ama görsel gerçekten
    yüklenemezse ya da hiç yoksa (kota, geçici hata, boş crest alanı vb.)
    tarayıcıda sessizce, ağ gerektirmeyen bir baş harf rozetine düşmek —
    böylece kullanıcı hiçbir zaman kırık/boş bir logo görmez.
    """
    return genel_yedek_logo_data_uri(takim_adi)


app.jinja_env.globals["takim_logo_url"] = takim_logo_url


app.jinja_env.globals['yz_mi'] = lambda k: yz_mi(k)


@app.context_processor
def sohbet_rozeti():
    """Alt menüdeki Sohbet sekmesinin okunmamış rozeti (sadece girişli kullanıcıda, tek sorgu)."""
    if not current_user.is_authenticated:
        return {}
    try:
        return {"toplam_okunmamis": sum(gruplara_gore_okunmamis(current_user.id).values())}
    except Exception:
        db.session.rollback()
        return {"toplam_okunmamis": 0}


@app.route("/sohbet")
@login_required
def sohbetler():
    """Alt menüdeki Sohbet sekmesi: tek grup varsa direkt o grubun sohbeti, yoksa grup seçimi."""
    uyelikler = (GrupUyeligi.query.options(db.joinedload(GrupUyeligi.grup))
                 .filter_by(kullanici_id=current_user.id).all())
    if len(uyelikler) == 1:
        return redirect(url_for("grup_sohbet", grup_id=uyelikler[0].grup_id))
    okunmamis = gruplara_gore_okunmamis(current_user.id)
    son_mesajlar = {}
    for u in uyelikler:
        son = Mesaj.query.filter_by(grup_id=u.grup_id).order_by(Mesaj.id.desc()).first()
        son_mesajlar[u.grup_id] = son.to_dict() if son else None
    gruplar = sorted([u.grup for u in uyelikler],
                     key=lambda g: (son_mesajlar[g.id] or {}).get("id", 0), reverse=True)
    return render_template("sohbetler.html", gruplar=gruplar, okunmamis=okunmamis, son_mesajlar=son_mesajlar)


@app.route("/admin/kullanicilar")
@admin_required
def admin_kullanicilar():
    kullanicilar = Kullanici.query.order_by(Kullanici.olusturulma.desc()).all()
    grup_sayilari = dict(
        db.session.query(GrupUyeligi.kullanici_id, db.func.count(GrupUyeligi.id))
        .group_by(GrupUyeligi.kullanici_id)
        .all()
    )
    kurulan_gruplar = dict(
        db.session.query(Grup.kurucu_id, db.func.count(Grup.id))
        .group_by(Grup.kurucu_id)
        .all()
    )
    return render_template(
        "admin_kullanicilar.html",
        kullanicilar=kullanicilar,
        grup_sayilari=grup_sayilari,
        kurulan_gruplar=kurulan_gruplar,
    )


@app.route("/admin/kullanicilar/<int:kullanici_id>/sil", methods=["POST"])
@admin_required
def admin_kullanici_sil(kullanici_id):
    kullanici = db.session.get(Kullanici, kullanici_id)
    if not kullanici:
        flash("Kullanıcı bulunamadı.", "danger")
        return redirect(url_for("admin_kullanicilar"))

    if kullanici.id == current_user.id:
        flash("Kendi hesabını admin panelinden silemezsin.", "danger")
        return redirect(url_for("admin_kullanicilar"))

    kurdugu_grup_sayisi = Grup.query.filter_by(kurucu_id=kullanici.id).count()
    if kurdugu_grup_sayisi:
        flash(
            f"'{kullanici.kullanici_adi}' {kurdugu_grup_sayisi} grubun kurucusu olduğu için silinemedi. "
            "Önce bu grupları silmen ya da başka birine devretmen gerekir.",
            "danger",
        )
        return redirect(url_for("admin_kullanicilar"))

    Tahmin.query.filter_by(kullanici_id=kullanici.id).delete()
    TahminCezasi.query.filter_by(kullanici_id=kullanici.id).delete()
    Mesaj.query.filter_by(kullanici_id=kullanici.id).delete()
    SohbetOkuma.query.filter_by(kullanici_id=kullanici.id).delete()
    GrupUyeligi.query.filter_by(kullanici_id=kullanici.id).delete()
    kullanici_adi = kullanici.kullanici_adi
    db.session.delete(kullanici)
    db.session.commit()
    flash(f"'{kullanici_adi}' kullanıcısı silindi.", "success")
    return redirect(url_for("admin_kullanicilar"))


@app.route("/admin/kullanicilar/<int:kullanici_id>/sifre-sifirla", methods=["POST"])
@admin_required
def admin_kullanici_sifre_sifirla(kullanici_id):
    kullanici = db.session.get(Kullanici, kullanici_id)
    if not kullanici:
        flash("Kullanıcı bulunamadı.", "danger")
        return redirect(url_for("admin_kullanicilar"))

    import secrets
    import string
    chars = string.ascii_letters + string.digits
    yeni_sifre = "".join(secrets.choice(chars) for _ in range(10))
    kullanici.sifre_hash = generate_password_hash(yeni_sifre)
    kullanici.sifre_degismeli = True
    db.session.commit()
    flash(
        f"'{kullanici.kullanici_adi}' için geçici şifre: {yeni_sifre} — bunu kullanıcıya ilet, "
        "bir daha gösterilmeyecek. Kullanıcı bu şifreyle girince kendi yeni şifresini belirleyecek.",
        "success",
    )
    return redirect(url_for("admin_kullanicilar"))


@app.route("/admin/kullanicilar/<int:kullanici_id>/admin-degistir", methods=["POST"])
@admin_required
def admin_kullanici_admin_degistir(kullanici_id):
    kullanici = db.session.get(Kullanici, kullanici_id)
    if not kullanici:
        flash("Kullanıcı bulunamadı.", "danger")
        return redirect(url_for("admin_kullanicilar"))

    if kullanici.id == current_user.id:
        flash("Kendi admin yetkini admin panelinden değiştiremezsin.", "danger")
        return redirect(url_for("admin_kullanicilar"))

    kullanici.admin = not kullanici.admin
    db.session.commit()
    durum = "admin yapıldı" if kullanici.admin else "admin yetkisi kaldırıldı"
    flash(f"'{kullanici.kullanici_adi}' {durum}.", "success")
    return redirect(url_for("admin_kullanicilar"))


@app.route("/api/team-logo/<int:team_id>")
def team_logo(team_id):
    """
    Takım logosunu önce RapidAPI (SportAPI7/Sofascore) üzerinden sunucu
    tarafında çekip servis eder — tarayıcıdan doğrudan Sofascore'a gidilirse
    WAF/hotlink koruması engelleyebiliyor. SportAPI7 başarısız olursa (kota
    dolması, geçici hata vb.) ?name= ile gönderilen takım adını kullanarak
    TheSportsDB'den yedek logo dener. Böylece tek bir sağlayıcının o an
    çalışmaması (ör. aylık RapidAPI kotasının dolması) belirli bir cihazda/
    ortamda logoların hiç görünmemesine yol açmaz.
    Başarılı sonuçlar 7 gün, başarısız denemeler ise tekrar tekrar aynı
    sağlayıcıya vurmamak için sadece 5 dk önbelleğe alınır.
    """
    simdi = utc_now_naive()

    # NOT: Burada eskiden, takımın statik yedek logosu varsa (4 takip edilen
    # Süper Lig takımı) hiçbir dış servise bağlanmadan direkt statik dosyaya
    # yönlendiren "erken çıkış" bloğu vardı. Bu, güvenilirliği garantiliyordu
    # ama takımın GERÇEK amblemini asla göstermiyordu — SportAPI7/Sofascore
    # hiç denenmiyordu. Şimdi gerçek logo önce deneniyor; statik dosya en
    # alttaki son çare olarak duruyor (bkz. fonksiyon sonu), yani iki servis
    # de gerçekten başarısız olursa hâlâ HER ZAMAN bir şey gösteriliyor.

    onbellek = TEAM_LOGO_CACHE.get(team_id)
    if onbellek:
        ttl = TEAM_LOGO_CACHE_TTL_SANIYE if onbellek.get("veri") else TEAM_LOGO_NEGATIVE_CACHE_TTL_SANIYE
        if (simdi - onbellek["alinma_zamani"]).total_seconds() < ttl:
            if not onbellek.get("veri"):
                yanit = Response(status=404)
                yanit.headers["Cache-Control"] = "no-store"
                return yanit
            yanit = Response(onbellek["veri"], mimetype=onbellek["content_type"])
            yanit.headers["Cache-Control"] = "public, max-age=604800"
            return yanit

    veri, content_type = None, None

    if SPORTAPI7_KEY:
        try:
            url = f"{SPORTAPI7_BASE_URL}/team/{team_id}/image"
            headers = {"X-RapidAPI-Key": SPORTAPI7_KEY, "X-RapidAPI-Host": SPORTAPI7_HOST}
            response = requests.get(url, headers=headers, timeout=LOGO_TIMEOUT)
            if response.status_code == 200:
                veri = response.content
                content_type = response.headers.get("Content-Type", "image/png")
        except Exception:
            pass

    if veri is None:
        takim_adi = request.args.get("name")
        if takim_adi:
            logo_url = _thesportsdb_logo_url(takim_adi)
            if logo_url:
                try:
                    response = requests.get(logo_url, timeout=LOGO_TIMEOUT)
                    if response.status_code == 200:
                        veri = response.content
                        content_type = response.headers.get("Content-Type", "image/png")
                except Exception:
                    pass

    if veri is None:
        # Sofascore/RapidAPI ve TheSportsDB ikisi de başarısız oldu. Son çare:
        # projeyle birlikte giden, hiçbir dış servise bağlı olmayan statik
        # logo — bu HER ZAMAN çalışır, cihaz/tarayıcı/kota farkı gözetmez.
        takim_adi = request.args.get("name")
        dosya = _statik_logo_dosyasi(takim_adi)
        if dosya:
            return redirect(url_for("static", filename=f"logos/{dosya}"))
        TEAM_LOGO_CACHE[team_id] = {"veri": None, "content_type": None, "alinma_zamani": simdi}
        yanit = Response(status=404)
        yanit.headers["Cache-Control"] = "no-store"
        return yanit

    TEAM_LOGO_CACHE[team_id] = {
        "veri": veri,
        "content_type": content_type,
        "alinma_zamani": simdi,
    }
    yanit = Response(veri, mimetype=content_type)
    yanit.headers["Cache-Control"] = "public, max-age=604800"
    return yanit


# ============================================================
# LİG PUAN TABLOSU
# ============================================================
# Süper Lig ve Şampiyonlar Ligi tablosu sitenin KENDİ maç verisinden hesaplanır
# (ek API isteği yok, kota yok). Uluslar Ligi'nde sadece Türkiye maçları
# tutulduğu için Türkiye'nin grubu ESPN'in standings uç noktasından çekilir.
ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings"
_PUAN_TABLOSU_ONBELLEK = {}
PUAN_TABLOSU_ONBELLEK_SN = 30 * 60


def _guncel_sezon_yili():
    simdi = datetime.now(timezone.utc)
    return simdi.year if simdi.month >= 7 else simdi.year - 1


def lig_puan_tablosu_hesapla(lig_id):
    """Biten maçlardan puan tablosu: [{sira, takim, logo, o, g, b, m, ag, yg, av, p, form}]"""
    sorgu = Mac.query.filter(
        Mac.lig_id == lig_id, Mac.durum == "bitti",
        Mac.ev_skor.isnot(None), Mac.deplasman_skor.isnot(None),
    )
    sezon = _guncel_sezon_yili()
    sorgu = sorgu.filter(db.or_(Mac.sezon == sezon, Mac.sezon.is_(None)))
    maclar = sorgu.order_by(Mac.baslangic.asc()).all()

    if lig_id == SUPER_LIG_ID:
        # ESPN'e geçişten kalan eşleşmemiş eski kopyalar puanları iki kez saymasın
        espn = [m for m in maclar if m.external_id and m.external_id >= ESPN_EXTERNAL_ID_OFFSET]
        if espn:
            maclar = espn
    if lig_id == CHAMPIONS_LEAGUE_ID:
        # Sadece lig aşaması (1-8. maç günü); eleme turları tabloya girmez
        maclar = [m for m in maclar if m.hafta and str(m.hafta).isdigit() and 1 <= int(m.hafta) <= 8]

    tablo = {}
    def satir(ad, logo):
        t = tablo.setdefault(ad, {"takim": ad, "logo": None, "o": 0, "g": 0, "b": 0, "m": 0, "ag": 0, "yg": 0, "form": []})
        if logo:
            t["logo"] = logo
        return t
    for m in maclar:
        ev, dep = satir(m.ev_sahibi, m.ev_logo), satir(m.deplasman, m.deplasman_logo)
        for t, at, ye in ((ev, m.ev_skor, m.deplasman_skor), (dep, m.deplasman_skor, m.ev_skor)):
            t["o"] += 1; t["ag"] += at; t["yg"] += ye
            sonuc = "G" if at > ye else ("B" if at == ye else "M")
            t[{"G": "g", "B": "b", "M": "m"}[sonuc]] += 1
            t["form"].append(sonuc)
            t.setdefault("form_detay", []).append({
                "harf": sonuc, "mac": f"{m.ev_sahibi} {m.ev_skor}-{m.deplasman_skor} {m.deplasman}",
                "alt": f"{tr_saat_filter(m.baslangic, '%d.%m')} · {LIG_KISA_ADLARI.get(m.lig_id, m.lig or '')}"})
    satirlar = []
    for t in tablo.values():
        t["av"] = t["ag"] - t["yg"]
        t["p"] = t["g"] * 3 + t["b"]
        t["form"] = t["form"][-5:]
        t["form_detay"] = t.get("form_detay", [])[-5:]
        satirlar.append(t)
    satirlar.sort(key=lambda t: (-t["p"], -t["av"], -t["ag"], t["takim"]))
    for i, t in enumerate(satirlar, 1):
        t["sira"] = i
    return satirlar


def _espn_turkiye_grubu():
    """Uluslar Ligi'nde Türkiye'nin grubunu ESPN'den çeker (30 dk önbellek)."""
    onbellek = _PUAN_TABLOSU_ONBELLEK.get("UNL")
    if onbellek and (datetime.utcnow() - onbellek["zaman"]).total_seconds() < PUAN_TABLOSU_ONBELLEK_SN:
        return onbellek["veri"]
    yanit = requests.get(ESPN_STANDINGS_URL.format(slug=ESPN_NATIONS_LEAGUE_SLUG),
                         headers={"Accept": "application/json"}, timeout=10)
    yanit.raise_for_status()
    veri = yanit.json()

    def gruplar(dugum):
        if isinstance(dugum, dict):
            if (dugum.get("standings") or {}).get("entries"):
                yield dugum
            for c in dugum.get("children") or []:
                yield from gruplar(c)

    def stat(entry, *adlar):
        for st in entry.get("stats") or []:
            if st.get("name") in adlar or st.get("type") in adlar or st.get("abbreviation") in adlar:
                try:
                    return int(float(st.get("value") or 0))
                except (TypeError, ValueError):
                    return 0
        return 0

    for g in gruplar(veri):
        entries = g["standings"]["entries"]
        adlar = [((e.get("team") or {}).get("displayName") or "") for e in entries]
        if not any(("Turk" in a) or ("Türk" in a) for a in adlar):
            continue
        satirlar = []
        for e in entries:
            takim = e.get("team") or {}
            logolar = takim.get("logos") or []
            ag, yg = stat(e, "pointsFor"), stat(e, "pointsAgainst")
            satirlar.append({
                "takim": takim.get("displayName") or "?", "logo": logolar[0].get("href") if logolar else None,
                "o": stat(e, "gamesPlayed"), "g": stat(e, "wins"), "b": stat(e, "ties"), "m": stat(e, "losses"),
                "ag": ag, "yg": yg, "av": stat(e, "pointDifferential") or (ag - yg), "p": stat(e, "points"),
                "sira": stat(e, "rank"), "form": [],
            })
        satirlar.sort(key=lambda t: (t["sira"] or 99, -t["p"], -t["av"]))
        for i, t in enumerate(satirlar, 1):
            t["sira"] = i
        sonuc = {"grup": g.get("name") or g.get("abbreviation") or "", "satirlar": satirlar}
        _PUAN_TABLOSU_ONBELLEK["UNL"] = {"zaman": datetime.utcnow(), "veri": sonuc}
        return sonuc
    return None


def grup_sezon_siralamasi(grup_id):
    """Grubun sezonluk (tüm zamanlar) sıralaması: üye başına toplam puan."""
    members = (GrupUyeligi.query.options(db.joinedload(GrupUyeligi.kullanici))
               .filter_by(grup_id=grup_id).all())
    toplam = dict(
        db.session.query(Tahmin.kullanici_id, db.func.coalesce(db.func.sum(Tahmin.puan), 0))
        .filter(Tahmin.grup_id == grup_id).group_by(Tahmin.kullanici_id).all()
    )
    for uid, ceza in ceza_toplamlari(grup_id=grup_id).items():
        toplam[uid] = toplam.get(uid, 0) + ceza
    return sorted(
        [{"user": m.kullanici, "points": int(toplam.get(m.kullanici_id, 0))} for m in members],
        key=lambda x: x["points"], reverse=True,
    )


def bu_haftanin_araligi(now=None):
    """Grup haftası Türkiye saatiyle Cuma 12:00'de başlar (UTC naive döner)."""
    bas = hafta_anahtari(now or utc_now_naive())
    return bas, bas + timedelta(days=7)


# ------------------------------------------------------------
# Haftanın Kralı paylaşım kartı
# Grup sayfaları gizli; kart ise imzalı (tahmin edilemeyen) bir linkle
# herkese açılır ki WhatsApp önizlemesi ve görsel çalışsın. Linkte davet
# kodu yok: kartı gören biri gruba katılamaz, sadece siteye yönlendirilir.
# ------------------------------------------------------------
import hmac as _hmac
from flask import make_response

TR_AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
            "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def _kart_imza(grup_id, ts):
    anahtar = (app.config.get("SECRET_KEY") or "").encode()
    return _hmac.new(anahtar, f"kral:{grup_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:16]


def _tr_tarih_araligi(bas, son):
    b = bas + TURKIYE_UTC_OFFSET
    e = son + TURKIYE_UTC_OFFSET
    if b.month == e.month:
        return f"{b.day} – {e.day} {TR_AYLAR[e.month - 1]}"
    return f"{b.day} {TR_AYLAR[b.month - 1]} – {e.day} {TR_AYLAR[e.month - 1]}"


def _haftada_biten_mac_sayisi(bas, son):
    return Mac.query.filter(Mac.lig_id.in_(gosterilecek_lig_id_listesi()), Mac.durum == "bitti",
                            Mac.baslangic >= bas, Mac.baslangic < son).count()


def kral_karti_verisi(grup_id, bas):
    grup_obj = db.session.get(Grup, grup_id)
    if not grup_obj:
        return None
    son = bas + timedelta(days=7)
    if _haftada_biten_mac_sayisi(bas, son) == 0:
        return None
    siralama = su_anki_hafta_liderligi_hesapla(grup_id, bas, son)
    if not siralama:
        return None
    devam = utc_now_naive() < son
    lider = siralama[0]
    if not lider["points"]:
        return None   # kimse puan almadıysa kral yok
    krallar = [r["user"].kullanici_adi for r in siralama if r["points"] == lider["points"]]
    if len(krallar) == 1:
        kral_ad = krallar[0]
    elif len(krallar) == 2:
        kral_ad = f"{krallar[0]} & {krallar[1]}"
    else:
        kral_ad = f"{krallar[0]} +{len(krallar) - 1}"
    liste, onceki_puan, sira = [], None, 0
    for i, r in enumerate(siralama[:5]):
        if r["points"] != onceki_puan:
            sira, onceki_puan = i + 1, r["points"]
        liste.append({"sira": sira, "ad": r["user"].kullanici_adi, "puan": r["points"], "bot": yz_mi(r["user"])})
    cenabet = None
    if len(siralama) > 1 and siralama[-1]["points"] < lider["points"]:
        cenabet = {"ad": siralama[-1]["user"].kullanici_adi, "puan": siralama[-1]["points"]}
    return {
        "grup_ad": grup_obj.ad,
        "ust_baslik": ("ŞU AN ZİRVEDE" if devam else ("HAFTANIN KRALLARI" if len(krallar) > 1 else "HAFTANIN KRALI")),
        "tarih": _tr_tarih_araligi(bas, son),
        "baslik_metin": ("şu an zirvede" if devam else ("haftanın kralları" if len(krallar) > 1 else "haftanın kralı")),
        "kral": {"ad": kral_ad, "puan": lider["points"]},
        "ortak_kral": 0,
        "liste": liste,
        "cenabet": None if devam else cenabet,
        "alt_yazi": (ANA_DOMAIN or "skorbilen.com"),
        "devam": devam,
    }


def paylasilabilir_haftalar(grup_id):
    """Grup sayfasındaki paylaş düğmeleri: devam eden hafta (maç oynandıysa)
    ve son tamamlanan hafta (son 4 hafta içinde maç oynanmış olan)."""
    simdi_bas, _ = bu_haftanin_araligi()
    sonuc = []
    if _haftada_biten_mac_sayisi(simdi_bas, simdi_bas + timedelta(days=7)):
        sonuc.append({"bas": simdi_bas, "etiket": "Şu anki lideri paylaş", "devam": True})
    for i in range(1, 5):
        b = simdi_bas - timedelta(days=7 * i)
        if _haftada_biten_mac_sayisi(b, b + timedelta(days=7)):
            sonuc.append({"bas": b, "etiket": "Haftanın kralını paylaş" if i == 1 else "Son haftanın kralını paylaş",
                          "devam": False, "gecen_hafta": i == 1})
            break
    for h in sonuc:
        veri = kral_karti_verisi(grup_id, h["bas"]) or {}
        h["kral_ad"] = (veri.get("kral") or {}).get("ad")
        h["puan"] = (veri.get("kral") or {}).get("puan")
        h["baslik_metin"] = veri.get("baslik_metin")
        h["tarih"] = veri.get("tarih")
        h["cenabet"] = veri.get("cenabet")
        ts = int(calendar.timegm(h["bas"].timetuple()))
        imza = _kart_imza(grup_id, ts)
        h["sayfa"] = url_for("kral_karti_sayfa", grup_id=grup_id, ts=ts, imza=imza, _external=True)
        h["gorsel"] = url_for("kral_karti_png", grup_id=grup_id, ts=ts, imza=imza)
    return sonuc


def _kart_dogrula(grup_id, ts, imza):
    if not _hmac.compare_digest(_kart_imza(grup_id, ts), imza or ""):
        abort(404)
    bas = datetime.utcfromtimestamp(ts)
    if hafta_anahtari(bas) != bas:
        abort(404)
    veri = kral_karti_verisi(grup_id, bas)
    if not veri:
        abort(404)
    return veri


@app.route("/paylas/kral/<int:grup_id>/<int:ts>/<imza>.png")
def kral_karti_png(grup_id, ts, imza):
    veri = _kart_dogrula(grup_id, ts, imza)
    from kral_karti import kart_ciz
    yanit = make_response(kart_ciz(veri))
    yanit.headers["Content-Type"] = "image/png"
    yanit.headers["Cache-Control"] = "public, max-age=600"
    return yanit


@app.route("/paylas/kral/<int:grup_id>/<int:ts>/<imza>")
def kral_karti_sayfa(grup_id, ts, imza):
    veri = _kart_dogrula(grup_id, ts, imza)
    gorsel = url_for("kral_karti_png", grup_id=grup_id, ts=ts, imza=imza, _external=True)
    return render_template("kral_karti.html", veri=veri, gorsel=gorsel,
                           sayfa=url_for("kral_karti_sayfa", grup_id=grup_id, ts=ts, imza=imza, _external=True))


# ------------------------------------------------------------
# Takım logoları kendi sunucumuz üzerinden: bazı operatörler/ağlar ESPN'in
# görsel sunucusunu (a.espncdn.com) engelleyebiliyor; o zaman logolar
# yerine baş harf rozetleri görünüyordu. Logo sunucuda indirilip küçültülür
# (500px PNG yerine 96px) ve uzun süre önbellekte tutulur; Cloudflare de .png
# uzantısı sayesinde kendi önbelleğinde saklar. Yalnızca bilinen iki kaynak
# kabul edilir, açık bir vekil (proxy) değildir.
# ------------------------------------------------------------
_LOGO_KAYNAKLARI = {
    "espn": ("https://a.espncdn.com/i/teamlogos/", _re.compile(r"^[a-z]+/\d{2,4}/[A-Za-z0-9_-]+\.png$")),
    "fd": ("https://crests.football-data.org/", _re.compile(r"^[A-Za-z0-9_-]+\.(png|svg)$")),
}
_LOGO_ONBELLEK = {}
_LOGO_ONBELLEK_MAX = 400


def logo_src(url):
    """Şablonlarda: dış logo adresini kendi /tlogo/ adresimize çevirir."""
    if not url or not isinstance(url, str):
        return url
    for anahtar, (kok, desen) in _LOGO_KAYNAKLARI.items():
        if url.startswith(kok):
            yol = url[len(kok):].split("?")[0]
            if desen.match(yol):
                if yol.endswith(".svg"):
                    return url          # SVG'yi küçültemeyiz; doğrudan bırak
                return f"/tlogo/{anahtar}/{yol}"
    if url.startswith("http://a.espncdn.com/"):
        return logo_src("https://" + url[len("http://"):])
    return url


app.jinja_env.globals["logo_src"] = logo_src


@app.route("/tlogo/<kaynak>/<path:yol>")
def takim_logo_vekil(kaynak, yol):
    k = _LOGO_KAYNAKLARI.get(kaynak)
    if not k or not k[1].match(yol):
        abort(404)
    anahtar = f"{kaynak}/{yol}"
    veri = _LOGO_ONBELLEK.get(anahtar)
    if veri is None:
        try:
            r = requests.get(k[0] + yol, timeout=8, headers={"User-Agent": "Mozilla/5.0 (TahminLigi logo)"})
            if r.status_code != 200 or not r.content:
                abort(404)
            veri = r.content
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(veri))
                im.load()
                if max(im.size) > 96:
                    im = im.convert("RGBA")
                    im.thumbnail((96, 96), Image.LANCZOS)
                    cikti = io.BytesIO()
                    im.save(cikti, "PNG", optimize=True)
                    veri = cikti.getvalue()
            except Exception:
                pass     # küçültülemezse orijinali ver
            if len(_LOGO_ONBELLEK) >= _LOGO_ONBELLEK_MAX:
                _LOGO_ONBELLEK.pop(next(iter(_LOGO_ONBELLEK)))
            _LOGO_ONBELLEK[anahtar] = veri
        except requests.RequestException:
            abort(404)
    yanit = make_response(veri)
    yanit.headers["Content-Type"] = "image/png"
    yanit.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return yanit


# ------------------------------------------------------------
# Tahmin ekranında takımların son 5 maçı (G/B/M). Tüm turnuvalardaki
# (Süper Lig, Şampiyonlar Ligi, Avrupa kupaları) biten maçlardan, en yenisi
# sağda olacak şekilde. İstek başına bir kez hesaplanır.
# ------------------------------------------------------------
from flask import g as _g


FORM_MAC_SAYISI = 3


def _lig_kisaltmasi(m):
    ad = (m.lig or "").lower()
    if m.lig_id == SUPER_LIG_ID or "süper" in ad:
        return "SL", "Süper Lig"
    if m.lig_id == CHAMPIONS_LEAGUE_ID or "şampiyon" in ad or "champions" in ad:
        return "ŞL", "Şampiyonlar Ligi"
    if "konferans" in ad or "conference" in ad:
        return "KL", "Konferans Ligi"
    if "avrupa" in ad or "europa" in ad:
        return "AL", "Avrupa Ligi"
    if "uluslar" in ad or "nations" in ad:
        return "UL", "Uluslar Ligi"
    return (m.lig or "?")[:2].upper(), (m.lig or "")


def _tum_takim_formlari():
    """{(lig_id, normalize_takim_adi): [son 3 maç]} — her lig kendi maçlarıyla."""
    if getattr(_g, "takim_formlari", None) is not None:
        return _g.takim_formlari
    sonuc = {}
    try:
        sinir = utc_now_naive() - timedelta(days=365)
        maclar = (Mac.query.filter(Mac.durum == "bitti", Mac.ev_skor.isnot(None), Mac.deplasman_skor.isnot(None),
                                   Mac.baslangic.isnot(None), Mac.baslangic >= sinir)
                  .order_by(Mac.baslangic.desc()).limit(3000).all())
        try:
            # Tahmine açılmayan ama form için tutulan maçlar (Avrupa'da rakiplerin diğer maçları)
            maclar += FormMaci.query.filter(FormMaci.baslangic >= sinir).all()
            maclar.sort(key=lambda m: m.baslangic, reverse=True)
        except Exception:
            db.session.rollback()
        gorulen = set()
        for m in maclar:
            ev, dep = takim_adi_normalize(m.ev_sahibi), takim_adi_normalize(m.deplasman)
            # Aynı maçın farklı kaynaklardan gelen kopyası iki kez sayılmasın
            imza = (m.lig_id, m.baslangic.date(), frozenset((ev, dep)))
            if imza in gorulen:
                continue
            gorulen.add(imza)
            tarih = tr_saat_filter(m.baslangic, "%d.%m")
            kisa, lig = _lig_kisaltmasi(m)
            mac_metni = f"{m.ev_sahibi} {m.ev_skor}-{m.deplasman_skor} {m.deplasman}"
            for k, atti, yedi in ((ev, m.ev_skor, m.deplasman_skor), (dep, m.deplasman_skor, m.ev_skor)):
                liste = sonuc.setdefault((m.lig_id, k), [])
                if len(liste) >= FORM_MAC_SAYISI:
                    continue
                harf = "G" if atti > yedi else ("M" if atti < yedi else "B")
                liste.append({"harf": harf, "lig": kisa, "mac": mac_metni, "alt": f"{tarih} · {lig}",
                              "aciklama": f"{tarih} {mac_metni} · {lig}"})
        for k in sonuc:
            sonuc[k].reverse()   # en eski solda, en yeni sağda
    except Exception as e:
        db.session.rollback()
        print("[takim-formu] hata:", e)
    _g.takim_formlari = sonuc
    return sonuc


def takim_formu(takim_adi, lig_id=None):
    """Takımın o ligdeki son 3 maçı (Şampiyonlar Ligi maçında ŞL, lig maçında lig sonuçları)."""
    return _tum_takim_formlari().get((lig_id, takim_adi_normalize(takim_adi)), [])


app.jinja_env.globals["takim_formu"] = takim_formu


# ------------------------------------------------------------
# Herkese açık "Robot Ziya'nın tahminleri" sayfası (SEO + içerik).
# Maç başlamadan: olasılıklar, beklenen goller, form, sıralama, ikili maçlar ve
# skor içermeyen bir yorum. Ziya'nın kesin skoru maç başlayınca açılır
# (gruplarda kopyalanmasın diye).
# ------------------------------------------------------------
_ZIYA_SAYFA_ONBELLEK = {"zaman": None, "veri": None}
ZIYA_SAYFA_ONBELLEK_SURE = timedelta(minutes=10)


def _yuzde(x):
    return int(round(x * 100))


def _ziya_takim_ozeti(ad, yer, ist):
    k = takim_adi_normalize(ad)
    t = ist["takimlar"].get(k)
    sira = ist["siralama"].get(k)
    ozet = {"ad": ad, "sira": sira["sira"] if sira else None, "puan": sira["puan"] if sira else None,
            "oynanan": sira["o"] if sira else 0, "at": None, "ye": None}
    if t:
        b = t[yer]
        if b[0] >= 1:
            ozet["at"], ozet["ye"] = round(b[1] / b[0], 1), round(b[2] / b[0], 1)
    return ozet


def _ziya_yorum(mac, ev, dep, p_ev, p_x, p_dep, lam_ev, lam_dep):
    """Skor söylemeyen, istatistiklerden kısa Türkçe yorum."""
    cumleler = []
    if ev["sira"] and dep["sira"]:
        cumleler.append(f"{ev['ad']} {ev['sira']}. sırada ({ev['puan']} puan), {dep['ad']} {dep['sira']}. sırada ({dep['puan']} puan).")
    if ev["at"] is not None:
        cumleler.append(f"{ev['ad']} evinde maç başına {ev['at']} gol atıp {ev['ye']} gol yiyor.")
    if dep["at"] is not None:
        cumleler.append(f"{dep['ad']} deplasmanda maç başına {dep['at']} gol atıp {dep['ye']} gol yiyor.")
    enb = max(p_ev, p_x, p_dep)
    if enb == p_x:
        cumleler.append(f"Robot Ziya'ya göre güçler dengeli; beraberlik ihtimali %{_yuzde(p_x)}.")
    else:
        fav, olas = (ev["ad"], p_ev) if p_ev >= p_dep else (dep["ad"], p_dep)
        fark = abs(p_ev - p_dep)
        derece = "açık ara favori" if fark > 0.35 else ("favori" if fark > 0.15 else "hafif favori")
        cumleler.append(f"Robot Ziya'ya göre {fav} {derece} (%{_yuzde(olas)}).")
    toplam = lam_ev + lam_dep
    if toplam >= 3.0:
        cumleler.append("Gollü bir maç bekleniyor.")
    elif toplam <= 2.0:
        cumleler.append("Az gollü, kontrollü bir maç bekleniyor.")
    return " ".join(cumleler)


def ziya_sayfa_verisi():
    simdi = utc_now_naive()
    o = _ZIYA_SAYFA_ONBELLEK
    if o["veri"] is not None and o["zaman"] and simdi - o["zaman"] < ZIYA_SAYFA_ONBELLEK_SURE:
        return o["veri"]
    ligler = gosterilecek_lig_id_listesi()
    yaklasan = (Mac.query.filter(Mac.lig_id.in_(ligler), super_lig_takim_filtresi(), Mac.baslangic.isnot(None),
                                 Mac.baslangic >= simdi - timedelta(hours=3), Mac.baslangic < simdi + timedelta(days=8),
                                 ~Mac.durum.in_(["iptal", "ertelendi"]))
                .order_by(Mac.baslangic.asc()).limit(60).all())
    biten = (Mac.query.filter(Mac.lig_id.in_(ligler), super_lig_takim_filtresi(), Mac.durum == "bitti",
                              Mac.baslangic >= simdi - timedelta(days=8), Mac.baslangic < simdi)
             .order_by(Mac.baslangic.desc()).limit(40).all())
    yz = yz_kullanici_getir(olustur=False)
    ziya_tahmin = {}
    if yz:
        ids = [m.id for m in yaklasan + biten]
        for t in Tahmin.query.filter(Tahmin.kullanici_id == yz.id, Tahmin.mac_id.in_(ids or [-1])).all():
            ziya_tahmin.setdefault(t.mac_id, t)
    analizler = {a.mac_id: a.metin for a in YzAnaliz.query.filter(
        YzAnaliz.mac_id.in_([m.id for m in yaklasan + biten] or [-1])).all()}
    istatistikler = {}

    def ist_al(lig_id):
        if lig_id not in istatistikler:
            istatistikler[lig_id] = _yz_lig_istatistigi(lig_id, simdi)
        return istatistikler[lig_id]

    kartlar = []
    for m in yaklasan:
        if m.durum == "bitti":
            continue
        ist = ist_al(m.lig_id)
        lam_ev, lam_dep, _, _ = yz_beklenen_goller(m, ist)
        p_ev, p_x, p_dep = yz_sonuc_olasiliklari(lam_ev, lam_dep)
        ev, dep = _ziya_takim_ozeti(m.ev_sahibi, "ic", ist), _ziya_takim_ozeti(m.deplasman, "dis", ist)
        import math
        kg = (1 - math.exp(-lam_ev)) * (1 - math.exp(-lam_dep))
        uc_arti = 1 - sum(_poisson(i, lam_ev) * _poisson(j, lam_dep) for i in range(3) for j in range(3 - i))
        ikili = ist["ikili"].get(frozenset((takim_adi_normalize(m.ev_sahibi), takim_adi_normalize(m.deplasman))), [])[-3:]
        basladi = m.basladi_mi()
        t = ziya_tahmin.get(m.id)
        kartlar.append({
            "mac": m, "ev": ev, "dep": dep, "lam_ev": round(lam_ev, 1), "lam_dep": round(lam_dep, 1),
            "p_ev": _yuzde(p_ev), "p_x": _yuzde(p_x), "p_dep": _yuzde(p_dep),
            "kg": _yuzde(kg), "uc_arti": _yuzde(uc_arti), "ikili": list(reversed(ikili)),
            "yorum": _ziya_yorum(m, ev, dep, p_ev, p_x, p_dep, lam_ev, lam_dep),
            "basladi": basladi,
            "ziya_skor": (f"{t.ev_skor}-{t.deplasman_skor}" if (t and basladi) else None),
            "analiz": analizler.get(m.id) if basladi else None,
        })
    sonuclar = []
    for m in biten:
        t = ziya_tahmin.get(m.id)
        if not t:
            continue
        sonuclar.append({"mac": m, "ziya_skor": f"{t.ev_skor}-{t.deplasman_skor}", "puan": t.puan or 0,
                         "analiz": analizler.get(m.id)})
    tutan = sum(1 for r in sonuclar if r["puan"] > 0)
    tam = sum(1 for r in sonuclar if r["puan"] >= 5)
    veri = {"kartlar": kartlar, "sonuclar": sonuclar,
            "karne": {"mac": len(sonuclar), "tutan": tutan, "tam": tam}, "guncelleme": simdi}
    o["zaman"], o["veri"] = simdi, veri
    return veri


app.jinja_env.globals["ANA_DOMAIN_ADI"] = ANA_DOMAIN or "skorbilen.com"


@app.route("/derbi")
def derbi_paylasim():
    """Sosyal medyada paylaşılacak link: Facebook/WhatsApp önizlemesinde derbi
    görselini gösterir, tıklayan kişiyi Robot Ziya'nın tahmin sayfasına götürür."""
    hedef = url_for("ziya_tahminleri")
    kok = f"https://{ANA_DOMAIN or request.host}"
    baslik = "Derbi tahmini: arkadaşlarınla yarış, eğlen! ⚽ Robot Ziya ne diyor? 🤖"
    aciklama = "Arkadaşlarınla grup kur, derbi skorunu tahmin et, yarışın! Robot Ziya: GS %32, beraberlik %25, FB %43. Ücretsiz, bahis yok."
    gorsel = f"{kok}/static/paylas/derbi_arkadas_og.png"
    html = f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{baslik}</title>
<meta name="description" content="{aciklama}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Skorbilen">
<meta property="og:url" content="{kok}/derbi">
<meta property="og:title" content="{baslik}">
<meta property="og:description" content="{aciklama}">
<meta property="og:image" content="{gorsel}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta property="og:locale" content="tr_TR">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{gorsel}">
<link rel="canonical" href="{kok}{hedef}">
<script>location.replace("{hedef}");</script>
</head><body style="font-family:sans-serif;text-align:center;padding:40px">
<p><a href="{hedef}">Robot Ziya'nın derbi tahminine git ›</a></p></body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/robot-ziya-tahminleri")
def ziya_tahminleri():
    try:
        veri = ziya_sayfa_verisi()
    except Exception as e:
        db.session.rollback()
        print("[ziya-sayfa] hata:", e)
        veri = {"kartlar": [], "sonuclar": [], "karne": {"mac": 0, "tutan": 0, "tam": 0}, "guncelleme": utc_now_naive()}
    return render_template("ziya_tahminleri.html", **veri)


@app.route("/puan-durumu")
def puan_durumu():
    """Herkese açık: girişsiz ziyaretçiler de liglerin puan tablosunu görebilir."""
    sekmeler = []
    for kod, bilgi in LEAGUES.items():
        if bilgi["id"] not in gosterilecek_lig_id_listesi() or bilgi["id"] == AVRUPA_ID:
            continue
        sekme = {"kod": kod, "ad": LIG_KISA_ADLARI.get(bilgi["id"], bilgi["ad"]), "satirlar": [], "not": None, "hata": None}
        if bilgi["id"] == NATIONS_LEAGUE_ID:
            try:
                grup = _espn_turkiye_grubu()
                if grup:
                    sekme["satirlar"] = grup["satirlar"]
                    sekme["not"] = f"Türkiye'nin grubu{(' (' + grup['grup'] + ')') if grup['grup'] else ''}"
                else:
                    sekme["hata"] = "Türkiye'nin grubu ESPN'de bulunamadı."
            except Exception:
                sekme["hata"] = "Uluslar Ligi tablosu şu an alınamadı. Biraz sonra tekrar dene."
        else:
            sekme["satirlar"] = lig_puan_tablosu_hesapla(bilgi["id"])
            if bilgi["id"] == CHAMPIONS_LEAGUE_ID:
                sekme["not"] = "Lig aşaması. İlk 8 son 16'ya, 9-24 arası play-off'a kalır."
                sekme["bolgeler"] = {"yesil": range(1, 9), "sari": range(9, 25)}
            else:
                n = len(sekme["satirlar"])
                sekme["not"] = "Son 3 takım küme düşer."
                sekme["bolgeler"] = {"yesil": range(1, 2), "kirmizi": range(max(n - 2, 1), n + 1) if n >= 6 else range(0)}
        sekmeler.append(sekme)
    # Giriş yapmış kullanıcıya kendi grubunun sezonluk ve haftalık sıralaması
    secili_grup, gruplarim = None, []
    if current_user.is_authenticated:
        gruplarim = [u.grup for u in GrupUyeligi.query.options(db.joinedload(GrupUyeligi.grup))
                     .filter_by(kullanici_id=current_user.id).all() if u.grup]
        istenen = request.args.get("grup", type=int)
        secili_grup = next((g for g in gruplarim if g.id == istenen), None) or (gruplarim[0] if gruplarim else None)
    if secili_grup:
        hafta_basi, hafta_sonu = bu_haftanin_araligi()
        sekmeler = [
            {"kod": "SEZON", "ad": "Grup · Sezon", "kisa": "Sezon", "grup_tablosu": True,
             "siralama": grup_sezon_siralamasi(secili_grup.id), "kral": "Sezonun lideri", "cenabet": "Sezonun sonuncusu",
             "not": f"{secili_grup.ad}: tüm maçların toplam puanı."},
            {"kod": "HAFTA", "ad": "Grup · Haftalık", "kisa": "Haftalık", "grup_tablosu": True,
             "siralama": su_anki_hafta_liderligi_hesapla(secili_grup.id, hafta_basi, hafta_sonu),
             "kral": "Haftanın kralı", "cenabet": "Haftanın cenabeti",
             "not": f"{secili_grup.ad}: {to_turkiye_saati(hafta_basi).strftime('%d.%m')} – "
                    f"{to_turkiye_saati(hafta_sonu).strftime('%d.%m')} (Cuma 12:00) arası, her hafta sıfırdan başlar."},
        ] + sekmeler

    gecerli = {s["kod"] for s in sekmeler}
    if request.args.get("lig") in gecerli:
        secili = request.args.get("lig")
    elif secili_grup:
        secili = "SEZON"
    else:
        secili = sekmeler[1]["kod"] if len(sekmeler) > 1 and sekmeler[1]["satirlar"] else (sekmeler[0]["kod"] if sekmeler else None)
    return render_template("puan_durumu.html", sekmeler=sekmeler, secili=secili,
                           secili_grup=secili_grup, gruplarim=gruplarim)


# ------------------------------------------------------------
# AKILLI OTOMATİK SENKRON
# ------------------------------------------------------------
# cron her 15 dk'da bir /cron/sync'i çağırır. API'lere SADECE sonucu
# beklenen (başlamasının üzerinden MAC_BITIS_DAKIKA geçmiş ama hâlâ
# "bitti" olmayan) bir maç varsa gidilir — ve sadece o maçın ligi / ayı
# çekilir. Günde bir kez de tüm sezon taranır (ertelenen/yeni maçlar için).
MAC_BITIS_DAKIKA = 105          # 90 dk + devre arası + uzatmalar
KESINLESME_IZLEME_SAAT = 4      # 120. dk'da bitti sayılan maçı API kesinleştirene kadar izle
API_KESIN_BITTI_DURUMLARI = ["FINISHED", "AWARDED", "STATUS_FULL_TIME", "STATUS_FINAL",
                             "STATUS_FINAL_AET", "STATUS_FINAL_PEN", "STATUS_FULL_PEN", "MANUEL"]
BEKLEYEN_MAC_GERI_BAKIS_SAAT = 48
TAM_SENKRON_ARALIK_SAAT = 24


def _senkron_zamani_oku(ad):
    kayit = db.session.get(SenkronKaydi, ad)
    return kayit.zaman if kayit else None


def _senkron_zamani_yaz(ad):
    kayit = db.session.get(SenkronKaydi, ad)
    if kayit is None:
        db.session.add(SenkronKaydi(ad=ad, zaman=utc_now_naive()))
    else:
        kayit.zaman = utc_now_naive()
    db.session.commit()


def sonucu_beklenen_maclar(now=None):
    now = now or utc_now_naive()
    return Mac.query.filter(
        Mac.lig_id.in_(TAKIP_EDILEN_LIG_ID_LISTESI),
        Mac.baslangic.isnot(None),
        Mac.baslangic <= now - timedelta(minutes=MAC_BITIS_DAKIKA),
        Mac.baslangic >= now - timedelta(hours=BEKLEYEN_MAC_GERI_BAKIS_SAAT),
        db.or_(
            ~Mac.durum.in_(["bitti", "iptal", "ertelendi"]),
            # 120. dakikada bizim "bitti" saydığımız ama API'nin henüz kesinleştirmediği
            # maçlar: skor sonradan değişirse puan düzelsin diye birkaç saat daha izle
            db.and_(
                Mac.durum == "bitti",
                Mac.baslangic >= now - timedelta(hours=KESINLESME_IZLEME_SAAT),
                db.or_(Mac.api_status.is_(None), ~Mac.api_status.in_(API_KESIN_BITTI_DURUMLARI)),
            ),
        ),
        db.or_(Mac.api_status.is_(None), Mac.api_status != "MANUEL"),
    ).all()


# ============================================================
# YAPAY ZEKÂ OYUNCU
# Her gruba otomatik katılan, istatistik modeliyle (Poisson) tahmin yapan
# bot oyuncu. Ücretsizdir: dış API çağırmaz, sadece veritabanındaki biten
# maçlara bakar. Tahminleri normal oyuncular gibi puanlanır.
# ============================================================

YZ_KULLANICI_ADI = "Robot Ziya"


def _yz_adi_rezerve_mi(ad):
    """'robot ziya', 'ROBOT ZİYA', 'robot_ziya', 'RobotZiya' gibi taklitleri engeller."""
    sade = (ad or "").replace("İ", "i").replace("I", "ı").lower()
    sade = "".join(h for h in sade if h.isalnum()).replace("ı", "i").replace("\u0307", "")
    return sade in {"robotziya", "ziyarobot", "yapayzeka", "yapayzekâ"}
YZ_TAHMIN_GUNU = 3                # Perşembe (Pazartesi=0)
YZ_TAHMIN_SAATI = (23, 59)        # Türkiye saati
YZ_GECMIS_GUN = 400               # modelin baktığı geçmiş
YZ_ONSEL_MAC = 4                  # az maçı olan takımı lig ortalamasına çeker
YZ_MAX_GOL = 6


def yz_kullanici_getir(olustur=True):
    kullanici = Kullanici.query.filter_by(kullanici_adi=YZ_KULLANICI_ADI).first()
    if kullanici or not olustur:
        return kullanici
    import secrets
    kullanici = Kullanici(kullanici_adi=YZ_KULLANICI_ADI)
    kullanici.set_password(secrets.token_urlsafe(48))  # kimse giriş yapamaz
    db.session.add(kullanici)
    db.session.flush()
    return kullanici


def yz_mi(kullanici):
    return bool(kullanici) and kullanici.kullanici_adi == YZ_KULLANICI_ADI


def yz_gruplara_ekle(yz=None):
    """YZ'yi üyesi olmadığı tüm gruplara ekler. Eklenen grup sayısını döner."""
    yz = yz or yz_kullanici_getir()
    mevcut = {gid for (gid,) in db.session.query(GrupUyeligi.grup_id).filter_by(kullanici_id=yz.id).all()}
    eklenen = 0
    for (gid,) in db.session.query(Grup.id).all():
        if gid not in mevcut:
            db.session.add(GrupUyeligi(grup_id=gid, kullanici_id=yz.id))
            eklenen += 1
    return eklenen


def _poisson(k, lam):
    import math
    return math.exp(-lam) * lam ** k / math.factorial(k)


YZ_FORM_YARI_OMUR_GUN = 90   # 90 gün önceki maç, bugünkünün yarısı kadar önemli
YZ_CESARET = 1.0             # 1: kalibre (istatistiğe sadık); >1 güç farkını büyütür


def _yz_lig_istatistigi(lig_id, simdi):
    """
    Ligin geçmiş maçlarından takım bazında ağırlıklı (yakın maç daha önemli)
    iç saha / deplasman gol ortalamaları, son maç formu, sıralama ve ikili maçlar.
    """
    maclar = Mac.query.filter(
        Mac.lig_id == lig_id, Mac.durum == "bitti",
        Mac.ev_skor.isnot(None), Mac.deplasman_skor.isnot(None),
        Mac.baslangic.isnot(None), Mac.baslangic >= simdi - timedelta(days=YZ_GECMIS_GUN),
    ).order_by(Mac.baslangic.asc()).all()
    if lig_id == SUPER_LIG_ID:
        espn = [m for m in maclar if m.external_id and m.external_id >= ESPN_EXTERNAL_ID_OFFSET]
        if espn:
            maclar = espn

    if len(maclar) >= 10:
        ev_ort = sum(m.ev_skor for m in maclar) / len(maclar)
        dep_ort = sum(m.deplasman_skor for m in maclar) / len(maclar)
    else:
        ev_ort, dep_ort = 1.5, 1.15
    ev_ort, dep_ort = max(ev_ort, 0.5), max(dep_ort, 0.4)

    # takim -> {"genel"|"ic"|"dis": [agirlik, atti*agirlik, yedi*agirlik], "n": maç sayısı}
    takimlar, form, ikili = {}, {}, {}
    for m in maclar:
        gun = max((simdi - m.baslangic).days, 0)
        w = 0.5 ** (gun / YZ_FORM_YARI_OMUR_GUN)
        for ad, rakip, atti, yedi, yer in ((m.ev_sahibi, m.deplasman, m.ev_skor, m.deplasman_skor, "ic"),
                                           (m.deplasman, m.ev_sahibi, m.deplasman_skor, m.ev_skor, "dis")):
            k = takim_adi_normalize(ad)
            t = takimlar.setdefault(k, {"genel": [0.0, 0.0, 0.0], "ic": [0.0, 0.0, 0.0], "dis": [0.0, 0.0, 0.0], "n": 0})
            for bolum in ("genel", yer):
                t[bolum][0] += w; t[bolum][1] += atti * w; t[bolum][2] += yedi * w
            t["n"] += 1
            harf = "G" if atti > yedi else ("M" if atti < yedi else "B")
            form.setdefault(k, []).append({"harf": harf, "skor": f"{atti}-{yedi}", "rakip": rakip,
                                           "yer": "iç saha" if yer == "ic" else "deplasman"})
        anahtar = frozenset((takim_adi_normalize(m.ev_sahibi), takim_adi_normalize(m.deplasman)))
        ikili.setdefault(anahtar, []).append(f"{m.ev_sahibi} {m.ev_skor}-{m.deplasman_skor} {m.deplasman}")

    siralama = {}
    try:
        for satir in lig_puan_tablosu_hesapla(lig_id):
            siralama[takim_adi_normalize(satir["takim"])] = {"sira": satir["sira"], "puan": satir["p"], "o": satir["o"]}
    except Exception:
        db.session.rollback()
    return {"ev_ort": ev_ort, "dep_ort": dep_ort, "takimlar": takimlar, "form": form,
            "ikili": ikili, "siralama": siralama, "mac_sayisi": len(maclar)}


def _yz_oran(bolum, ort, onsel=2.0):
    """Ağırlıklı (atılan, yenilen) ortalamanın lig ortalamasına oranı; az veri lig ortalamasına çekilir."""
    w, atti, yedi = bolum
    return (atti + onsel * ort) / (w + onsel) / ort, (yedi + onsel * ort) / (w + onsel) / ort


def yz_beklenen_goller(mac, istatistik):
    """(ev_beklenen, dep_beklenen, ev_mac_sayisi, dep_mac_sayisi)"""
    ort = (istatistik["ev_ort"] + istatistik["dep_ort"]) / 2
    bos = {"genel": [0.0, 0.0, 0.0], "ic": [0.0, 0.0, 0.0], "dis": [0.0, 0.0, 0.0], "n": 0}
    ev = istatistik["takimlar"].get(takim_adi_normalize(mac.ev_sahibi), bos)
    dep = istatistik["takimlar"].get(takim_adi_normalize(mac.deplasman), bos)

    ev_h, ev_s = _yz_oran(ev["genel"], ort)
    dep_h, dep_s = _yz_oran(dep["genel"], ort)
    # İç saha / deplasman performansını genel güce yarı yarıya kat
    ev_ic_h, ev_ic_s = _yz_oran(ev["ic"], istatistik["ev_ort"], onsel=3.0)
    dep_dis_h, dep_dis_s = _yz_oran(dep["dis"], istatistik["dep_ort"], onsel=3.0)
    ev_h, ev_s = (ev_h * ev_ic_h) ** 0.5, (ev_s * ev_ic_s) ** 0.5
    dep_h, dep_s = (dep_h * dep_dis_h) ** 0.5, (dep_s * dep_dis_s) ** 0.5

    # Cesaret: güç farkını büyüt (oran^1.3)
    lam_ev = istatistik["ev_ort"] * (ev_h * dep_s) ** YZ_CESARET
    lam_dep = istatistik["dep_ort"] * (dep_h * ev_s) ** YZ_CESARET
    return min(max(lam_ev, 0.2), 5.0), min(max(lam_dep, 0.2), 5.0), ev["n"], dep["n"]


def yz_sonuc_olasiliklari(lam_ev, lam_dep):
    """Poisson ile (ev, beraberlik, deplasman) olasılıkları."""
    p_ev = p_x = p_dep = 0.0
    for i in range(10):
        for j in range(10):
            p = _poisson(i, lam_ev) * _poisson(j, lam_dep)
            if i > j: p_ev += p
            elif i < j: p_dep += p
            else: p_x += p
    return p_ev, p_x, p_dep


def _yuvarla(x):
    import math
    return int(math.floor(x + 0.5))


def yz_skor_tahmini(mac, istatistik):
    """
    İstatistik modeli: (1) ev / beraberlik / deplasman olasılıklarını hesaplar,
    en olası sonucu seçer (dengeli maçta beraberliği de değerlendirir);
    (2) o sonucun içindeki en olası tam skoru yazar.
    """
    lam_ev, lam_dep, _, _ = yz_beklenen_goller(mac, istatistik)
    p_ev, p_x, p_dep = yz_sonuc_olasiliklari(lam_ev, lam_dep)

    if abs(lam_ev - lam_dep) < 0.22 and p_x >= max(p_ev, p_dep) * 0.8:
        sonuc = 0
    else:
        sonuc = 1 if p_ev >= p_dep else -1

    en_iyi, en_iyi_p = (1, 1), -1.0
    for i in range(7):
        for j in range(7):
            if (i > j) - (i < j) != sonuc:
                continue
            p = _poisson(i, lam_ev) * _poisson(j, lam_dep)
            if p > en_iyi_p:
                en_iyi, en_iyi_p = (i, j), p
    return en_iyi


def _yz_takim_ozeti(ad, ist):
    k = takim_adi_normalize(ad)
    t = ist["takimlar"].get(k)
    son = ist["form"].get(k, [])[-5:]
    sira = ist["siralama"].get(k)
    if not t:
        return {"veri": "yok"}
    def ort(b):
        return [round(b[1] / b[0], 2), round(b[2] / b[0], 2)] if b[0] else None
    return {
        "lig_sirasi": sira["sira"] if sira else None,
        "puan": sira["puan"] if sira else None,
        "mac_sayisi": t["n"],
        "mac_basi_attigi_yedigi_genel": ort(t["genel"]),
        "ic_sahada_attigi_yedigi": ort(t["ic"]),
        "deplasmanda_attigi_yedigi": ort(t["dis"]),
        "son_5": [f'{f["harf"]} {f["skor"]} {f["rakip"]} ({f["yer"]})' for f in son],
    }


def yz_analiz_metni(mac, istatistik, skor):
    """Gemini yokken kullanılan, istatistiklerden üretilen kısa Türkçe analiz."""
    lam_ev, lam_dep, _, _ = yz_beklenen_goller(mac, istatistik)
    parcalar = []
    for ad, yer in ((mac.ev_sahibi, "ic"), (mac.deplasman, "dis")):
        k = takim_adi_normalize(ad)
        son = istatistik["form"].get(k, [])[-5:]
        t = istatistik["takimlar"].get(k)
        sira = istatistik["siralama"].get(k)
        if not t:
            parcalar.append(f"{ad} hakkında elimde pek veri yok.")
            continue
        form = "".join(f["harf"] for f in son)
        cumle = f"{ad}"
        if sira:
            cumle += f" ({sira['sira']}. sırada)"
        if form:
            cumle += f" son {len(son)} maçta {form.count('G')}G {form.count('B')}B {form.count('M')}M"
        b = t[yer]
        if b[0] >= 1:
            yer_adi = "evinde" if yer == "ic" else "deplasmanda"
            cumle += f", {yer_adi} maç başı {b[1] / b[0]:.1f} atıp {b[2] / b[0]:.1f} yiyor"
        parcalar.append(cumle + ".")
    ev, dep = skor
    p_ev, p_x, p_dep = yz_sonuc_olasiliklari(lam_ev, lam_dep)
    olasilik = f"Olasılıklar: ev %{p_ev * 100:.0f}, beraberlik %{p_x * 100:.0f}, deplasman %{p_dep * 100:.0f}."
    return " ".join(parcalar) + f" Beklenen gol {lam_ev:.1f}–{lam_dep:.1f}. {olasilik} Tahmin: {ev}-{dep}."


# ---------- İsteğe bağlı: ücretsiz Google Gemini ----------
# GEMINI_API_KEY tanımlıysa Ziya, istatistikleri Gemini'ye verip skoru ona sorar.
# Anahtar ücretsiz: https://aistudio.google.com/apikey  (kart istemez)
# Gemini cevap vermezse/hata verirse istatistik modelinin tahmini kullanılır.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_TIMEOUT = 60


def _yz_gemini_sor(maclar, istatistikler):
    """maclar: [Mac]. Dönüş: {mac_id: (ev, dep, analiz)} — hata olursa boş sözlük."""
    if not GEMINI_API_KEY or not maclar:
        return {}
    import json
    satirlar = []
    for mac in maclar:
        ist = istatistikler[mac.lig_id]
        lam_ev, lam_dep, _, _ = yz_beklenen_goller(mac, ist)
        ikili = ist["ikili"].get(frozenset((takim_adi_normalize(mac.ev_sahibi), takim_adi_normalize(mac.deplasman))), [])
        satirlar.append({
            "id": mac.id,
            "lig": mac.lig,
            "tarih": to_turkiye_saati(mac.baslangic).strftime("%d.%m.%Y %H:%M") if mac.baslangic else "",
            "ev_sahibi": mac.ev_sahibi,
            "deplasman": mac.deplasman,
            "ev_sahibi_veri": _yz_takim_ozeti(mac.ev_sahibi, ist),
            "deplasman_veri": _yz_takim_ozeti(mac.deplasman, ist),
            "aralarindaki_son_maclar": ikili[-3:],
            "istatistik_modeli_beklenen_gol": [round(lam_ev, 2), round(lam_dep, 2)],
            "istatistik_modeli_olasiliklar_ev_x_dep": [round(p * 100) for p in yz_sonuc_olasiliklari(lam_ev, lam_dep)],
            "lig_ortalamasi_ev_dep": [round(ist["ev_ort"], 2), round(ist["dep_ort"], 2)],
        })
    istem = (
        "Sen 'Robot Ziya' adında, veriyle çalışan bir futbol analistisin ve arkadaş grubunun skor tahmin "
        "oyununda yarışıyorsun. Puanlama: tam skor 5 puan, sadece doğru sonuç (ev/beraberlik/deplasman) 3 puan.\n"
        "Her maç için şu sırayla düşün:\n"
        "1) Verilen istatistikleri değerlendir: lig sırası ve puan, son 5 maç formu, iç saha/deplasman maç başı "
        "atılan-yenilen gol, aralarındaki son maçlar, istatistik modelinin beklenen golleri ve olasılıkları.\n"
        "2) Kendi futbol bilginle düzelt: kadro kalitesi, sakatlık/rotasyon ihtimali, maçın önemi, "
        "Avrupa maçı yorgunluğu gibi. Verinin az olduğu takımlarda (mac_sayisi düşük) bu bilgiye daha çok yaslan.\n"
        "3) Ev / beraberlik / deplasman olasılığını yüzde olarak belirle; en olası sonucu seç.\n"
        "4) O sonuç içinde beklenen gollerle tutarlı, en olası skoru yaz. Skoru abartma ya da kısma; "
        "beklenen goller 1.2–1.0 ise 1-1 ya da 1-0, 2.6–0.6 ise 2-0 / 3-0 gibi.\n"
        "'analiz' alanına 2-3 cümlelik, somut sayılara dayanan, sakin ve analitik bir Türkçe değerlendirme yaz; "
        "belirleyici 2-3 etkeni ve olasılıkları belirt (örn. 'Ev %55 / X %25 / D %20').\n"
        "Sadece şu biçimde JSON dizi döndür: [{\"id\": <maç id>, \"ev\": <gol>, \"dep\": <gol>, \"analiz\": \"...\"}]\n\n"
        + json.dumps(satirlar, ensure_ascii=False)
    )
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": istem}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
            },
            timeout=GEMINI_TIMEOUT,
        )
        r.raise_for_status()
        metin = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        veri = json.loads(metin)
        gecerli_idler = {m.id for m in maclar}
        sonuc = {}
        for kayit in veri if isinstance(veri, list) else []:
            try:
                mid, ev, dep = int(kayit["id"]), int(kayit["ev"]), int(kayit["dep"])
            except (KeyError, TypeError, ValueError):
                continue
            if mid in gecerli_idler and 0 <= ev <= 9 and 0 <= dep <= 9:
                analiz = str(kayit.get("analiz") or "").strip()[:600]
                sonuc[mid] = (ev, dep, analiz)
        return sonuc
    except Exception as e:
        app.logger.warning("Gemini tahmini alınamadı: %s", e)
        return {}


def _yz_analiz_kaydet(mac_id, metin, kaynak):
    if not metin:
        return
    kayit = db.session.get(YzAnaliz, mac_id)
    if kayit is None:
        db.session.add(YzAnaliz(mac_id=mac_id, metin=metin, kaynak=kaynak))
    else:
        kayit.metin, kayit.kaynak, kayit.olusturulma = metin, kaynak, datetime.utcnow()


def yz_son_tahmin_ani(simdi):
    """simdi'den önceki en son 'Perşembe 23:59 (Türkiye saati)' anını UTC olarak verir."""
    tr = simdi + timedelta(hours=3)
    gun_farki = (tr.weekday() - YZ_TAHMIN_GUNU) % 7
    aday = (tr - timedelta(days=gun_farki)).replace(hour=YZ_TAHMIN_SAATI[0], minute=YZ_TAHMIN_SAATI[1], second=0, microsecond=0)
    if aday > tr:
        aday -= timedelta(days=7)
    return aday - timedelta(hours=3)


def yz_tahminleri_uret(simdi=None, zorla=False):
    """
    Robot Ziya haftada bir, Perşembe 23:59'da (TR) tahmin yapar: o andan bir
    sonraki Perşembe 23:59'a kadar oynanacak tüm maçlara. Cron 10 dakikada bir
    çağırır; Perşembe 23:59 geçtikten sonraki ilk çağrıda tahminler girilir.
    Hafta içinde kurulan yeni gruplara, Ziya'nın o hafta verdiği tahminler kopyalanır.
    """
    simdi = simdi or utc_now_naive()
    yz = yz_kullanici_getir()
    eklenen_grup = yz_gruplara_ekle(yz)
    grup_idleri = [gid for (gid,) in db.session.query(GrupUyeligi.grup_id).filter_by(kullanici_id=yz.id).all()]
    db.session.commit()

    hedef = yz_son_tahmin_ani(simdi)
    son = _senkron_zamani_oku("yz")
    sira_geldi = zorla or son is None or son < hedef
    # İlk çalıştırmada ya da elle tetiklenince Perşembe'yi beklemeden bu haftanın
    # tüm maçlarını (bir sonraki Perşembe 23:59'a kadar) kapsa
    bitis = hedef + timedelta(days=14 if (zorla or son is None) else 7)
    sonuc = {"gruba_eklendi": eklenen_grup, "yeni_tahmin": 0, "kopyalanan": 0,
             "sonraki_tahmin_tr": (hedef + timedelta(days=7, hours=3)).strftime("%d.%m.%Y %H:%M")}
    if not grup_idleri:
        return sonuc

    # 1) Haftalık tahmin zamanı geldiyse yeni tahminleri üret
    if sira_geldi:
        maclar = Mac.query.filter(
            Mac.lig_id.in_(gosterilecek_lig_id_listesi()),
            Mac.baslangic.isnot(None),
            Mac.baslangic > simdi,
            Mac.baslangic <= bitis,
            ~Mac.durum.in_(["bitti", "iptal", "ertelendi"]),
            super_lig_takim_filtresi(),
        ).all()
        var_olan = set()
        if maclar:
            var_olan = set(
                db.session.query(Tahmin.grup_id, Tahmin.mac_id)
                .filter(Tahmin.kullanici_id == yz.id, Tahmin.mac_id.in_([m.id for m in maclar]))
                .all()
            )
        istatistikler = {}
        yapilacak = []
        for mac in maclar:
            eksik = [gid for gid in grup_idleri if (gid, mac.id) not in var_olan]
            if not eksik:
                continue
            if mac.lig_id not in istatistikler:
                istatistikler[mac.lig_id] = _yz_lig_istatistigi(mac.lig_id, simdi)
            yapilacak.append((mac, eksik))

        gemini = _yz_gemini_sor([m for m, _ in yapilacak], istatistikler)
        sonuc["kaynak"] = "Gemini" if gemini else "istatistik modeli"
        for mac, eksik in yapilacak:
            if mac.id in gemini:
                ev, dep, analiz = gemini[mac.id]
                kaynak = "Gemini"
                if not analiz:
                    analiz = yz_analiz_metni(mac, istatistikler[mac.lig_id], (ev, dep))
            else:
                ev, dep = yz_skor_tahmini(mac, istatistikler[mac.lig_id])
                analiz, kaynak = yz_analiz_metni(mac, istatistikler[mac.lig_id], (ev, dep)), "istatistik"
            _yz_analiz_kaydet(mac.id, analiz, kaynak)
            for gid in eksik:
                db.session.add(Tahmin(grup_id=gid, mac_id=mac.id, kullanici_id=yz.id, ev_skor=ev, deplasman_skor=dep))
                sonuc["yeni_tahmin"] += 1
        db.session.commit()
        _senkron_zamani_yaz("yz")
        return sonuc

    # 2) Ara günlerde: Ziya'nın bu hafta verdiği tahminleri, eksik kalan (yeni) gruplara kopyala
    mevcut = (
        db.session.query(Tahmin.grup_id, Tahmin.mac_id, Tahmin.ev_skor, Tahmin.deplasman_skor)
        .join(Mac, Tahmin.mac_id == Mac.id)
        .filter(Tahmin.kullanici_id == yz.id, Mac.baslangic > simdi)
        .all()
    )
    skorlar, sahip = {}, set()
    for gid, mid, ev, dep in mevcut:
        skorlar.setdefault(mid, (ev, dep))
        sahip.add((gid, mid))
    for mid, (ev, dep) in skorlar.items():
        for gid in grup_idleri:
            if (gid, mid) not in sahip:
                db.session.add(Tahmin(grup_id=gid, mac_id=mid, kullanici_id=yz.id, ev_skor=ev, deplasman_skor=dep))
                sonuc["kopyalanan"] += 1
    db.session.commit()
    return sonuc


def yz_tahminleri_yenile():
    """Ziya'nın henüz başlamamış maçlardaki tahminlerini silip yeniden üretir."""
    yz = yz_kullanici_getir(olustur=False)
    silinen = 0
    if yz:
        simdi = utc_now_naive()
        baslamamis = [mid for (mid,) in db.session.query(Mac.id).filter(Mac.baslangic > simdi).all()]
        if baslamamis:
            silinen = (Tahmin.query.filter(Tahmin.kullanici_id == yz.id, Tahmin.mac_id.in_(baslamamis))
                       .delete(synchronize_session=False))
            db.session.commit()
    sonuc = yz_tahminleri_uret(zorla=True)
    sonuc["silinen"] = silinen
    return sonuc


def yz_teshis():
    """Admin için: Ziya'nın yaklaşan maçlardaki tahmini ve modelin gördüğü veriler."""
    simdi = utc_now_naive()
    yz = yz_kullanici_getir(olustur=False)
    maclar = Mac.query.filter(
        Mac.lig_id.in_(gosterilecek_lig_id_listesi()),
        Mac.baslangic.isnot(None), Mac.baslangic > simdi,
        Mac.baslangic <= simdi + timedelta(days=14),
        ~Mac.durum.in_(["bitti", "iptal", "ertelendi"]),
        super_lig_takim_filtresi(),
    ).order_by(Mac.baslangic.asc()).limit(40).all()
    tahminler = {}
    if yz and maclar:
        for t in Tahmin.query.filter(Tahmin.kullanici_id == yz.id, Tahmin.mac_id.in_([m.id for m in maclar])).all():
            tahminler.setdefault(t.mac_id, (t.ev_skor, t.deplasman_skor))
    istatistikler, satirlar = {}, []
    for mac in maclar:
        if mac.lig_id not in istatistikler:
            istatistikler[mac.lig_id] = _yz_lig_istatistigi(mac.lig_id, simdi)
        ist = istatistikler[mac.lig_id]
        lam_ev, lam_dep, ev_n, dep_n = yz_beklenen_goller(mac, ist)
        satirlar.append({
            "mac": mac, "ziya": tahminler.get(mac.id), "model": yz_skor_tahmini(mac, ist),
            "lam_ev": lam_ev, "lam_dep": lam_dep, "ev_n": ev_n, "dep_n": dep_n,
            "lig_mac": ist["mac_sayisi"],
            "analiz": (db.session.get(YzAnaliz, mac.id).metin if db.session.get(YzAnaliz, mac.id) else ""),
        })
    return satirlar


def _yz_guvenli_calistir(zorla=False):
    try:
        return yz_tahminleri_uret(zorla=zorla)
    except Exception as e:
        db.session.rollback()
        return {"success": False, "error": str(e)}


# ============================================================
# SPOR HABERLERİ (ana sayfa slider'ı)
# Türk spor sitelerinin herkese açık RSS akışlarından sadece başlık, kaynak,
# bağlantı ve kapak fotoğrafı alınır; haberin metni kopyalanmaz. Tıklayınca
# haber kaynak sitede açılır. Cron 30 dakikada bir yeniler.
# ============================================================
HABER_KAYNAKLARI = [
    ("TRT Haber", "https://www.trthaber.com/spor_articles.rss"),
    ("NTV Spor", "https://www.ntvspor.net/rss/anasayfa"),
    ("AA", "https://www.aa.com.tr/tr/rss/default?cat=spor"),
    ("Fanatik", "https://www.fanatik.com.tr/rss/futbol"),
    ("Fotomaç", "https://www.fotomac.com.tr/rss/anasayfa.xml"),
]
HABER_YENILEME_DAKIKA = 30
HABER_SAKLAMA_GUN = 4
HABER_GOSTERILEN = 40
# Slider üstündeki takım filtresi: (kod, görünen ad, başlıkta aranacak kelimeler)
HABER_TAKIM_FILTRELERI = [
    ("gs", "Galatasaray", ["galatasaray", "cimbom", "aslan"]),
    ("fb", "Fenerbahçe", ["fenerbahce", "kanarya"]),
    ("bjk", "Beşiktaş", ["besiktas", "kara kartal"]),
    ("ts", "Trabzonspor", ["trabzonspor", "firtina"]),
    ("milli", "Milli Takım", ["milli takim", "a milli", "ay-yildizli", "ay yildizli", "montella"]),
]
HABER_OG_GORSEL_LIMIT = 20       # bir yenilemede fotoğrafı sayfadan çekilecek en fazla haber
# Türk takımları / milli takım: başlık veya özet bunlardan birini içermeli
HABER_ANAHTAR_KELIMELER = [
    "galatasaray", "fenerbahce", "besiktas", "trabzonspor", "basaksehir", "samsunspor", "goztepe",
    "kasimpasa", "antalyaspor", "alanyaspor", "konyaspor", "kayserispor", "sivasspor", "rizespor",
    "gaziantep", "eyupspor", "kocaelispor", "genclerbirligi", "karagumruk", "hatayspor", "bodrum",
    "super lig", "milli takim", "a milli", "montella", "tff",
]
HABER_UA = "Mozilla/5.0 (compatible; TahminLigi/1.0; +haber-basliklari)"


def _haber_metin_sade(m):
    return (m or "").translate(_TR_HARF).lower()


def _haber_ilgili_mi(baslik, ozet=""):
    sade = _haber_metin_sade(baslik + " " + (ozet or ""))
    anahtarlar = set(HABER_ANAHTAR_KELIMELER)
    try:  # veritabanındaki Süper Lig takımlarını da ekle (yeni çıkan takımlar için)
        for (ad,) in db.session.query(Mac.ev_sahibi).filter(Mac.lig_id == SUPER_LIG_ID).distinct().limit(60):
            ilk = takim_adi_normalize(ad).split()
            if ilk and len(ilk[0]) >= 5:
                anahtarlar.add(ilk[0])
    except Exception:
        db.session.rollback()
    return any(k in sade for k in anahtarlar)


def _rss_tarih(metin):
    from email.utils import parsedate_to_datetime
    if not metin:
        return None
    try:
        d = parsedate_to_datetime(metin.strip())
        if d.tzinfo:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return d
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(metin.strip().replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        return None


def _rss_gorsel(item, ozet_html):
    """RSS öğesindeki fotoğraf: enclosure, media:content/thumbnail, image, açıklamadaki <img>."""
    import re
    for el in item.iter():
        etiket = el.tag.split('}')[-1].lower()
        if etiket == "enclosure" and (el.get("type") or "image").startswith("image") and el.get("url"):
            return el.get("url")
        if etiket in ("content", "thumbnail") and el.get("url") and (el.get("medium") in (None, "image") ):
            if not (el.get("type") or "").startswith("video"):
                return el.get("url")
        if etiket in ("image", "img", "resim", "imageurl") and (el.text or "").strip().startswith("http"):
            return el.text.strip()
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', ozet_html or "", re.I)
    return m.group(1) if m else None


def _og_gorsel(url):
    import re
    try:
        r = requests.get(url, headers={"User-Agent": HABER_UA}, timeout=6)
        if r.status_code != 200:
            return None
        html = r.text[:200000]
        for desen in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                      r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                      r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
            m = re.search(desen, html, re.I)
            if m:
                return m.group(1).replace("&amp;", "&")
    except Exception:
        return None
    return None


def rss_oku(kaynak, url, xml_metni=None):
    """Bir RSS/Atom akışını okuyup [{baslik, link, ozet, gorsel, yayin, kaynak}] döner."""
    import xml.etree.ElementTree as ET
    import html as htmlmod
    if xml_metni is None:
        r = requests.get(url, headers={"User-Agent": HABER_UA}, timeout=10)
        r.raise_for_status()
        xml_metni = r.content
    kok = ET.fromstring(xml_metni)
    ogeler = [e for e in kok.iter() if e.tag.split('}')[-1] in ("item", "entry")]
    sonuc = []
    for it in ogeler:
        def bul(*adlar):
            for e in it:
                if e.tag.split('}')[-1] in adlar:
                    if e.tag.split('}')[-1] == "link" and e.get("href"):
                        return e.get("href")
                    return (e.text or "").strip()
            return ""
        baslik = htmlmod.unescape(bul("title"))
        link = bul("link") or bul("guid")
        if not baslik or not link.startswith("http"):
            continue
        ozet = bul("description", "summary", "encoded")
        sonuc.append({
            "kaynak": kaynak, "baslik": baslik[:400], "link": link[:600],
            "ozet": ozet, "gorsel": _rss_gorsel(it, ozet),
            "yayin": _rss_tarih(bul("pubDate", "published", "updated", "date")),
        })
    return sonuc


def haberleri_guncelle(zorla=False):
    simdi = utc_now_naive()
    son = _senkron_zamani_oku("haber")
    if not zorla and son and simdi - son < timedelta(minutes=HABER_YENILEME_DAKIKA):
        return {"tur": "haber", "atlandi": True}
    eklenen, hatalar, og_sayac = 0, [], 0
    mevcut = {l for (l,) in db.session.query(Haber.link).all()}
    for kaynak, url in HABER_KAYNAKLARI:
        try:
            ogeler = rss_oku(kaynak, url)
        except Exception as e:
            hatalar.append(f"{kaynak}: {e}")
            continue
        for o in ogeler:
            if o["link"] in mevcut or not _haber_ilgili_mi(o["baslik"], o["ozet"]):
                continue
            yayin = o["yayin"] or simdi
            if yayin < simdi - timedelta(days=HABER_SAKLAMA_GUN):
                continue
            gorsel = o["gorsel"]
            if not gorsel and og_sayac < HABER_OG_GORSEL_LIMIT:
                og_sayac += 1
                gorsel = _og_gorsel(o["link"])
            if not gorsel:
                continue  # fotoğrafsız haber gösterilmez
            db.session.add(Haber(link=o["link"], baslik=o["baslik"], kaynak=kaynak, gorsel=gorsel[:800], yayin=yayin))
            mevcut.add(o["link"])
            eklenen += 1
    Haber.query.filter(Haber.yayin < simdi - timedelta(days=HABER_SAKLAMA_GUN)).delete(synchronize_session=False)
    db.session.commit()
    _senkron_zamani_yaz("haber")
    return {"tur": "haber", "eklenen": eklenen, "hatalar": hatalar[:5]}


def haber_takim_kodlari(baslik):
    """Haber başlığında geçen takımların filtre kodları (örn. ['gs', 'fb'])."""
    sade = _haber_metin_sade(baslik)
    return [kod for kod, _ad, kelimeler in HABER_TAKIM_FILTRELERI if any(k in sade for k in kelimeler)]


app.jinja_env.globals["haber_takim_kodlari"] = haber_takim_kodlari
app.jinja_env.globals["HABER_TAKIM_FILTRELERI"] = HABER_TAKIM_FILTRELERI


def son_haberler(adet=HABER_GOSTERILEN):
    try:
        return Haber.query.order_by(Haber.yayin.desc()).limit(adet).all()
    except Exception:
        db.session.rollback()
        return []


def _haber_guvenli_guncelle(zorla=False):
    try:
        return haberleri_guncelle(zorla=zorla)
    except Exception as e:
        db.session.rollback()
        return {"tur": "haber", "success": False, "error": str(e)}


def akilli_senkron():
    now = utc_now_naive()
    yz_sonuc = _yz_guvenli_calistir()
    _haber_guvenli_guncelle()
    try:
        # API'ye gidemesek bile 120. dakikası dolmuş, skoru gelmiş maçları puanla
        if gecikmis_maclari_bitir():
            recalculate_all_points()
    except Exception:
        db.session.rollback()

    son_tam = _senkron_zamani_oku("tam")
    if son_tam is None or (now - son_tam) >= timedelta(hours=TAM_SENKRON_ARALIK_SAAT):
        sonuclar = sync_all_leagues()
        _senkron_zamani_yaz("tam")
        return {"tur": "tam (günlük)", "sonuclar": sonuclar, "yapay_zeka": yz_sonuc}

    bekleyenler = sonucu_beklenen_maclar(now)
    if not bekleyenler:
        return {"tur": "atlandı", "mesaj": "Sonucu beklenen maç yok, API'ye gidilmedi.", "yapay_zeka": yz_sonuc}

    lig_id_to_kod = {bilgi["id"]: kod for kod, bilgi in LEAGUES.items()}
    lig_aylari = {}
    for m in bekleyenler:
        kod = lig_id_to_kod.get(m.lig_id)
        if kod:
            # Türkiye saatiyle ay sınırına denk gelen maçlar için hem UTC hem TR ayı
            for t in (m.baslangic, m.baslangic + timedelta(hours=3)):
                lig_aylari.setdefault(kod, set()).add((t.year, t.month))

    sonuclar = {}
    for kod, aylar in lig_aylari.items():
        try:
            sonuclar[kod] = sync_league(kod, aylar=sorted(aylar))
        except Exception as e:
            db.session.rollback()
            sonuclar[kod] = {"success": False, "error": str(e)}
    return {
        "tur": "bekleyen maçlar",
        "bekleyen": [f"{m.ev_sahibi} - {m.deplasman}" for m in bekleyenler],
        "sonuclar": sonuclar,
        "yapay_zeka": yz_sonuc,
    }


@app.route("/cron/sync", methods=["GET", "POST"])
def cron_sync():
    """
    Render Cron Job / dış zamanlayıcı tarafından çağrılacak endpoint.
    Login gerektirmez, bunun yerine CRON_SECRET ile korunur.
    Kullanım: curl -H "X-Cron-Secret: <secret>" https://.../cron/sync
    veya:     curl https://.../cron/sync?secret=<secret>
    """
    beklenen_secret = os.getenv("CRON_SECRET", "").strip()
    if not beklenen_secret:
        return jsonify({"success": False, "error": "CRON_SECRET tanımlı değil (env var eksik)."}), 500

    gelen_secret = request.headers.get("X-Cron-Secret") or request.args.get("secret") or ""
    if gelen_secret != beklenen_secret:
        abort(403)

    # Ağır iş (ESPN/football-data senkronu, Gemini) arka planda çalışır; cron'a
    # HEMEN 200 döneriz ki zaman aşımı / geçici hata yüzünden cron-job.org görevi
    # kapatmasın. Sonucu görmek için ?bekle=1 eklenebilir (eski davranış).
    if request.args.get("ziya") == "1":
        is_adi, is_fonk = "robot ziya", (lambda: {"yapay_zeka": _yz_guvenli_calistir(zorla=True)})
    elif request.args.get("tam") == "1":
        def is_fonk():
            sonuclar = sync_all_leagues()
            _senkron_zamani_yaz("tam")
            return {"sonuclar": sonuclar, "yapay_zeka": _yz_guvenli_calistir()}
        is_adi = "tam"
    else:
        is_adi, is_fonk = "akıllı", akilli_senkron

    if request.args.get("bekle") == "1":
        try:
            return jsonify({"tur": is_adi, "sonuc": is_fonk()}), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": str(e)}), 200

    return jsonify(_cron_arka_planda_baslat(is_adi, is_fonk)), 200


_CRON_KILIDI = threading.Lock()
_CRON_SON_DURUM = {"is": None, "basladi": None, "bitti": None, "sonuc": None}


def _cron_arka_planda_baslat(is_adi, is_fonk):
    """İşi ayrı bir thread'de başlatır; zaten bir senkron sürüyorsa yenisini başlatmaz."""
    if not _CRON_KILIDI.acquire(blocking=False):
        return {"durum": "zaten çalışıyor", "onceki": _cron_durum_ozeti()}

    def calistir():
        try:
            with app.app_context():
                _CRON_SON_DURUM.update({"is": is_adi, "basladi": utc_now_naive().isoformat(), "bitti": None, "sonuc": None})
                try:
                    sonuc = is_fonk()
                except Exception as e:
                    db.session.rollback()
                    app.logger.exception("Cron işi hata verdi")
                    sonuc = {"success": False, "error": str(e)}
                finally:
                    db.session.remove()
                _CRON_SON_DURUM.update({"bitti": utc_now_naive().isoformat(), "sonuc": sonuc})
        finally:
            _CRON_KILIDI.release()

    onceki = _cron_durum_ozeti()
    threading.Thread(target=calistir, daemon=True).start()
    return {"durum": "başlatıldı", "is": is_adi, "onceki": onceki}


def _cron_durum_ozeti():
    d = dict(_CRON_SON_DURUM)
    d.pop("is_", None)
    sonuc = d.get("sonuc")
    if isinstance(sonuc, dict):
        d["sonuc"] = {k: v for k, v in sonuc.items() if k in ("tur", "success", "error", "mesaj", "bekleyen", "yapay_zeka")}
    return d


@app.errorhandler(403)
def forbidden(error):
    return "<h1>403</h1><p>Bu sayfaya erişim yetkiniz yok.</p>", 403

@app.errorhandler(404)
def not_found(error):
    return "<h1>404</h1><p>Sayfa bulunamadı.</p>", 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return "<h1>500</h1><p>Sunucu tarafında bir hata oluştu.</p>", 500


# Eski veriler: açılışta bir kez, başlamamış maçlardaki tahminleri tüm gruplara eşitle.
# Dosyanın sonunda çalışır ki Robot Ziya yardımcıları tanımlı olsun.
with app.app_context():
    _tahminleri_esitle_guvenli()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
