import hashlib
import os
from datetime import datetime, timezone, timedelta
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

# Takip edilen tüm ligler burada tanımlı. "provider" hangi API/senkronizasyon
# fonksiyonunun kullanılacağını belirler. Yeni bir lig eklemek için buraya bir
# satır eklemek yeterli; sync_all_leagues() otomatik olarak hepsini çeker.
LEAGUES = {
    CHAMPIONS_LEAGUE_CODE: {"id": CHAMPIONS_LEAGUE_ID, "ad": "UEFA Champions League", "provider": "football-data"},
    SUPER_LIG_CODE: {"id": SUPER_LIG_ID, "ad": "Süper Lig", "provider": "espn"},  # ücretsiz, kota yok
    NATIONS_LEAGUE_CODE: {"id": NATIONS_LEAGUE_ID, "ad": "UEFA Uluslar Ligi", "provider": "espn-nations"},
}

TAKIP_EDILEN_LIG_ID_LISTESI = [lig["id"] for lig in LEAGUES.values()]

# Grup sayfasındaki lig sekmelerinde görünen kısa adlar.
LIG_KISA_ADLARI = {
    CHAMPIONS_LEAGUE_ID: "Şampiyonlar Ligi",
    SUPER_LIG_ID: "Süper Lig",
    NATIONS_LEAGUE_ID: "Uluslar Ligi",
}

# Grup sayfasında "bu hafta" sayılan süre: bir ligin sıradaki maçı bu kadar
# gün içinde değilse o lig için maç listelenmez.
HAFTA_PENCERESI_GUN = 7

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


def gosterilecek_lig_id_listesi():
    """Kullanıcıya gösterilecek lig id listesi.

    Normalde TAKIP_EDILEN_LIG_ID_LISTESI ile aynıdır; tek fark, Şampiyonlar
    Ligi'nin CHAMPIONS_LEAGUE_GORUNUM_BASLANGICI tarihinden önce bu listeden
    çıkarılmasıdır (bkz. yukarıdaki not).
    """
    if datetime.now(timezone.utc).replace(tzinfo=None) < CHAMPIONS_LEAGUE_GORUNUM_BASLANGICI:
        return [lig_id for lig_id in TAKIP_EDILEN_LIG_ID_LISTESI if lig_id != CHAMPIONS_LEAGUE_ID]
    return TAKIP_EDILEN_LIG_ID_LISTESI


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
            "yanit": None,
        }
        if self.yanit_id:
            y = self.yanitlanan
            veri["yanit"] = ({"id": y.id, "kullanici_adi": y.kullanici.kullanici_adi if y.kullanici else "?",
                              "icerik": y.icerik[:120]} if y else
                             {"id": self.yanit_id, "kullanici_adi": "", "icerik": "Bu mesaj silindi."})
        return veri


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

    # football-data'nın ücretsiz planında maç bittiği halde durum bir süre
    # "IN_PLAY"/"PAUSED"/"TIMED" kalabiliyor; skor gelmiş ve başlama saatinin
    # üzerinden 2,5 saat geçmişse maçı bitmiş say ki puanlar hesaplansın.
    if (mac.durum in ("planlandi", "oynaniyor") and mac.ev_skor is not None
            and mac.deplasman_skor is not None and mac.baslangic
            and utc_now_naive() > mac.baslangic + timedelta(minutes=150)):
        mac.durum = "bitti"

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

    hafta_no = 0
    onceki_tarih = None
    sonuc = []
    for tarih, event in ayristirilan:
        if onceki_tarih is None or (tarih - onceki_tarih).days > (
            ESPN_HAFTA_BOSLUK_GUN if bosluk_gun is None else bosluk_gun
        ):
            hafta_no += 1
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


def recalculate_all_points():
    sistem = Sistem.query.first()
    sifirlama_tarihi = sistem.puan_sifirlama_tarihi if sistem else None
    muaf_mac_id_seti = {m.mac_id for m in MacMuafiyet.query.all()}

    predictions = Tahmin.query.all()
    changed = False
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

        if tahmin.puan != new_points:
            tahmin.puan = new_points
            changed = True
    if changed:
        db.session.commit()


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
        if db.engine.dialect.name == "postgresql":
            try:
                db.session.execute(db.text("ALTER TABLE mesajlar ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE sohbet_okumalari ENABLE ROW LEVEL SECURITY"))
                db.session.execute(db.text("ALTER TABLE senkron_kayitlari ENABLE ROW LEVEL SECURITY"))
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
    return render_template("anasayfa.html", ozet=ozet, yaklasan=yaklasan)


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
        "Allow: /$", "Allow: /puan-durumu", "Allow: /nasil-oynanir", "Allow: /gizlilik", "Allow: /iletisim",
        "Disallow: /admin", "Disallow: /cron", "Disallow: /grup", "Disallow: /sohbet", "Disallow: /api",
        "", f"Sitemap: {request.url_root.rstrip('/')}/sitemap.xml",
    ]
    return Response("\n".join(satirlar) + "\n", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    kok = request.url_root.rstrip("/")
    sayfalar = ["/", "/puan-durumu", "/nasil-oynanir", "/gizlilik", "/iletisim"]
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


@app.route("/cikis")
@login_required
def cikis():
    logout_user()
    flash("Çıkış yapıldı.", "success")
    return redirect(url_for("giris"))


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
    toplam_puan = db.session.query(db.func.coalesce(db.func.sum(Tahmin.puan), 0)).filter(Tahmin.kullanici_id == current_user.id).scalar() or 0
    toplam_tahmin = Tahmin.query.filter_by(kullanici_id=current_user.id).count()

    return render_template(
        "dashboard.html",
        yaklasan_maclar=yaklasan_maclar,
        biten_maclar=biten_maclar,
        tahminler=tahminler,
        gruplar=gruplar,
        toplam_puan=toplam_puan,
        toplam_tahmin=toplam_tahmin,
        tahmin_edilen_maclar=toplam_tahmin
    )


@app.route("/gruplar")
@login_required
def gruplar():
    uyelikler = GrupUyeligi.query.filter_by(kullanici_id=current_user.id).all()
    return render_template("gruplar.html", gruplar=[u.grup for u in uyelikler])


@app.route("/grup-olustur", methods=["POST"])
@login_required
def grup_olustur():
    ad = request.form.get("ad", "").strip()
    aciklama = request.form.get("aciklama", "").strip()
    if not ad:
        flash("Grup adı girin.", "danger")
        return redirect(url_for("gruplar"))

    grup = Grup(ad=ad, aciklama=aciklama, davet_kodu=generate_invite_code(), sezon=2026, kurucu_id=current_user.id)
    db.session.add(grup)
    db.session.flush()
    db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=current_user.id))
    try:
        db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=yz_kullanici_getir().id))
    except Exception:
        pass
    db.session.commit()
    flash("Grup oluşturuldu.", "success")
    return redirect(url_for("grup", grup_id=grup.id))


@app.route("/grup-katil", methods=["POST"])
@login_required
def grup_katil():
    davet_kodu = request.form.get("davet_kodu", "").strip().upper()
    grup = Grup.query.filter_by(davet_kodu=davet_kodu).first()
    if not grup:
        flash("Geçersiz davet kodu.", "danger")
        return redirect(url_for("gruplar"))

    if GrupUyeligi.query.filter_by(grup_id=grup.id, kullanici_id=current_user.id).first():
        flash("Zaten bu grubun üyesisiniz.", "warning")
    else:
        db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=current_user.id))
        db.session.commit()
        flash("Gruba katıldınız.", "success")
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
        return redirect(url_for("giris") if not current_user.is_authenticated else url_for("gruplar"))

    if not current_user.is_authenticated:
        hedef = url_for("katil_link", davet_kodu=davet_kodu)
        flash(f"'{grup.ad}' grubuna katılmak için önce giriş yap ya da kayıt ol.", "success")
        return redirect(url_for("giris", next=hedef))

    if not GrupUyeligi.query.filter_by(grup_id=grup.id, kullanici_id=current_user.id).first():
        db.session.add(GrupUyeligi(grup_id=grup.id, kullanici_id=current_user.id))
        db.session.commit()
        flash(f"'{grup.ad}' grubuna katıldınız. 🎉", "success")

    return redirect(url_for("grup", grup_id=grup.id))


def hafta_anahtari(tarih):
    """Bir maç başlangıç tarihinin ait olduğu takvim haftasının (o haftanın
    Pazartesi 00:00'ı) anahtarını üretir. Lig/hafta numarası yerine gerçek
    takvime göre gruplama yapılır ki Şampiyonlar Ligi ve Süper Lig maçları
    aynı haftada oynanıyorsa TEK bir haftalık tabloda birleşsin."""
    if not tarih:
        return None
    # weekday(): Pazartesi=0 ... Cuma=4 ... Pazar=6.
    # Haftanın başlangıcını Cuma 00:00 kabul ediyoruz (Cuma-Perşembe haftası).
    cumaya_kadar_gecen_gun = (tarih.weekday() - 4) % 7
    return (tarih - timedelta(days=cumaya_kadar_gecen_gun)).replace(hour=0, minute=0, second=0, microsecond=0)


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
            key=lambda x: x["points"],
            reverse=True
        )
        sonuc.append({
            "hafta_basi": hafta_basi,
            "hafta_sonu": (hafta_basi + timedelta(days=6)) if hafta_basi else None,
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
            Mac.lig_id.in_(gosterilecek_lig_id_listesi()),
            Mac.baslangic >= hafta_basi,
            Mac.baslangic < hafta_sonu,
        )
        .all()
    )
    for uid, puan in kayitlar:
        if uid in puanlar:
            puanlar[uid] += (puan or 0)

    return sorted(
        [{"user": kullanici_map[uid], "points": puan} for uid, puan in puanlar.items()],
        key=lambda x: x["points"],
        reverse=True,
    )


@app.route("/grup/<int:grup_id>")
@login_required
def grup(grup_id):
    grup_obj = db.session.get(Grup, grup_id)
    if not grup_obj or not GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first():
        abort(403)

    now = utc_now_naive()

    # Takip edilen her lig için bir sonraki maçı bulup o ligin "aktif haftası"nı
    # belirliyoruz; birden fazla lig aynı anda gösterilebiliyor (örn. Şampiyonlar
    # Ligi + Süper Lig).
    # SADECE BU HAFTANIN maçları: bir ligin sıradaki maçı önümüzdeki
    # HAFTA_PENCERESI_GUN gün içinde değilse o lig bu hafta boş gösterilir
    # (örn. Uluslar Ligi'nin bir sonraki maçı 1 ay sonraysa listelenmez).
    pencere_sonu = now + timedelta(days=HAFTA_PENCERESI_GUN)
    aktif_haftalar = {}
    sonraki_mac_tarihleri = {}
    for lig_id in gosterilecek_lig_id_listesi():
        en_yakin_mac = Mac.query.filter(
            Mac.lig_id == lig_id, Mac.baslangic.isnot(None), Mac.baslangic >= now,
            super_lig_takim_filtresi(),
        ).order_by(Mac.baslangic.asc()).first()
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
            "hafta": aktif_haftalar.get(bilgi["id"]),
            "sonraki_mac": sonraki_mac_tarihleri.get(bilgi["id"]),
            "acik_mac": sum(1 for m in lig_maclari if m.oynanabilir_mi()),
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

    # Sezonluk / tüm zamanlar genel sıralama (kümülatif toplam puan)
    leaderboard = sorted([
        {"user": m.kullanici, "points": int(toplam_puanlar.get(m.kullanici_id, 0))}
        for m in members
    ], key=lambda x: x["points"], reverse=True)

    # Bu haftanın lider tablosu: hangi ligden olursa olsun (Şampiyonlar Ligi +
    # Süper Lig birlikte, ayrı ayrı DEĞİL), gerçek takvim haftasındaki (Pazartesi–
    # Pazar) maçların puanları TEK bir tabloda. Yeni hafta başladığında (o
    # haftanın maçları henüz oynanmamış olsa bile) herkes gerçekten 0'dan başlar.
    cumaya_kadar_gecen_gun = (now.weekday() - 4) % 7
    hafta_basi = (now - timedelta(days=cumaya_kadar_gecen_gun)).replace(hour=0, minute=0, second=0, microsecond=0)
    hafta_sonu = hafta_basi + timedelta(days=7)
    su_anki_hafta_siralamasi = su_anki_hafta_liderligi_hesapla(grup_id, hafta_basi, hafta_sonu)
    oynanabilir_mac_var = any(m.oynanabilir_mi() for m in maclar)

    return render_template(
        "grup.html",
        grup=grup_obj,
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
        oynanabilir_mac_var=oynanabilir_mac_var
    )


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
        if not mac or not mac.oynanabilir_mi():
            atlanan += 1
            continue

        existing = Tahmin.query.filter_by(grup_id=grup_id, mac_id=mac_id, kullanici_id=current_user.id).first()
        if existing:
            existing.ev_skor = ev_skor
            existing.deplasman_skor = deplasman_skor
            existing.guncellenme = datetime.utcnow()
        else:
            db.session.add(Tahmin(
                grup_id=grup_id, mac_id=mac_id, kullanici_id=current_user.id,
                ev_skor=ev_skor, deplasman_skor=deplasman_skor, puan=0
            ))
        kaydedilen += 1

    db.session.commit()

    if kaydedilen and atlanan:
        flash(f"{kaydedilen} maç için tahmininiz kaydedildi, {atlanan} maç atlandı (başlamış olabilir).", "success")
    elif kaydedilen:
        flash(f"{kaydedilen} maç için tahmininiz kaydedildi. 🎯", "success")
    else:
        flash("Kaydedilecek yeni bir tahmin bulunamadı.", "warning")

    return redirect(url_for("grup", grup_id=grup_id))


@app.route("/tahmin/<int:grup_id>/<int:mac_id>", methods=["GET", "POST"])
@login_required
def tahmin(grup_id, mac_id):
    """Tek bir maç için tahmin girme/güncelleme sayfası (örn. geçmiş sayfasından tekil düzenleme için)."""
    membership = GrupUyeligi.query.filter_by(grup_id=grup_id, kullanici_id=current_user.id).first()
    mac = db.session.get(Mac, mac_id)
    if not membership or not mac:
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

        if existing:
            existing.ev_skor = ev_skor
            existing.deplasman_skor = deplasman_skor
            existing.guncellenme = datetime.utcnow()
        else:
            db.session.add(Tahmin(grup_id=grup_id, mac_id=mac_id, kullanici_id=current_user.id, ev_skor=ev_skor, deplasman_skor=deplasman_skor, puan=0))

        db.session.commit()
        flash("Tahmininiz kaydedildi.", "success")
        return redirect(url_for("grup", grup_id=grup_id))

    return render_template("tahmin.html", grup=membership.grup, mac=mac, tahmin=existing)


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
        Mac.lig_id.in_(gosterilecek_lig_id_listesi()), Mac.durum == "bitti",
        super_lig_takim_filtresi(),
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
    total_points = db.session.query(db.func.coalesce(db.func.sum(Tahmin.puan), 0)).filter(Tahmin.kullanici_id == current_user.id).scalar() or 0
    prediction_count = Tahmin.query.filter_by(kullanici_id=current_user.id).count()
    return render_template("profil.html", toplam_puan=total_points, toplam_tahmin=prediction_count)


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
    )


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
        flash(f"{lig_adi} senkronizasyonu tamamlandı. API: {result['api_matches']} | Yeni: {result['inserted']} | Güncellenen: {result['updated']}", "success")
    except Exception as e:
        db.session.rollback()
        ipucu = ""
        provider = LEAGUES.get(lig_kodu, {}).get("provider")
        if provider == "sportapi7":
            ipucu = " — Not: SportAPI7'nin ücretsiz RapidAPI planı aylık istek limitlidir (limit dolduysa bir süre bekleyin) ve SPORTAPI7_KEY'in doğru olduğundan emin olun."
        elif provider == "api-football":
            ipucu = " — Not: API-Football ücretsiz planı günde 100 istekle sınırlıdır; limit dolduysa bir süre sonra tekrar deneyin ve API_FOOTBALL_KEY'in doğru olduğundan emin olun."
        elif provider in ("espn", "espn-nations"):
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
    db.session.commit()
    flash(
        f"'{kullanici.kullanici_adi}' için yeni şifre: {yeni_sifre} — bunu kullanıcıya ilet, "
        "bir daha gösterilmeyecek.",
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
    satirlar = []
    for t in tablo.values():
        t["av"] = t["ag"] - t["yg"]
        t["p"] = t["g"] * 3 + t["b"]
        t["form"] = t["form"][-5:]
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


@app.route("/puan-durumu")
def puan_durumu():
    """Herkese açık: girişsiz ziyaretçiler de liglerin puan tablosunu görebilir."""
    sekmeler = []
    for kod, bilgi in LEAGUES.items():
        if bilgi["id"] not in gosterilecek_lig_id_listesi():
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
    secili = request.args.get("lig") or (sekmeler[1]["kod"] if len(sekmeler) > 1 and sekmeler[1]["satirlar"] else (sekmeler[0]["kod"] if sekmeler else None))
    return render_template("puan_durumu.html", sekmeler=sekmeler, secili=secili)


# ------------------------------------------------------------
# AKILLI OTOMATİK SENKRON
# ------------------------------------------------------------
# cron her 15 dk'da bir /cron/sync'i çağırır. API'lere SADECE sonucu
# beklenen (başlamasının üzerinden MAC_BITIS_DAKIKA geçmiş ama hâlâ
# "bitti" olmayan) bir maç varsa gidilir — ve sadece o maçın ligi / ayı
# çekilir. Günde bir kez de tüm sezon taranır (ertelenen/yeni maçlar için).
MAC_BITIS_DAKIKA = 105          # 90 dk + devre arası + uzatmalar
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
        ~Mac.durum.in_(["bitti", "iptal", "ertelendi"]),
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


def _yz_lig_istatistigi(lig_id, simdi):
    """Ligin geçmiş maçlarından takım bazında gol ortalamaları ve son maç formu."""
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

    takimlar, form = {}, {}
    for m in maclar:
        for ad, rakip, atti, yedi, yer in ((m.ev_sahibi, m.deplasman, m.ev_skor, m.deplasman_skor, "iç"),
                                           (m.deplasman, m.ev_sahibi, m.deplasman_skor, m.ev_skor, "dış")):
            anahtar = takim_adi_normalize(ad)
            t = takimlar.setdefault(anahtar, [0, 0, 0])
            t[0] += 1; t[1] += atti; t[2] += yedi
            harf = "G" if atti > yedi else ("M" if atti < yedi else "B")
            form.setdefault(anahtar, []).append(f"{harf} {atti}-{yedi} {rakip} ({yer})")
    return {"ev_ort": ev_ort, "dep_ort": dep_ort, "takimlar": takimlar, "form": form, "mac_sayisi": len(maclar)}


def yz_beklenen_goller(mac, istatistik):
    """(ev_beklenen, dep_beklenen, ev_mac_sayisi, dep_mac_sayisi)"""
    ort = (istatistik["ev_ort"] + istatistik["dep_ort"]) / 2

    def guc(ad):
        o, atti, yedi = istatistik["takimlar"].get(takim_adi_normalize(ad), (0, 0, 0))
        hucum = (atti + YZ_ONSEL_MAC * ort) / (o + YZ_ONSEL_MAC) / ort
        savunma = (yedi + YZ_ONSEL_MAC * ort) / (o + YZ_ONSEL_MAC) / ort
        return hucum, savunma, o

    ev_h, ev_s, ev_n = guc(mac.ev_sahibi)
    dep_h, dep_s, dep_n = guc(mac.deplasman)
    lam_ev = min(max(istatistik["ev_ort"] * ev_h * dep_s, 0.2), 4.5)
    lam_dep = min(max(istatistik["dep_ort"] * dep_h * ev_s, 0.2), 4.5)
    return lam_ev, lam_dep, ev_n, dep_n


def _yuvarla(x):
    import math
    return int(math.floor(x + 0.4))  # 0.6 ve üstü yukarı: 0.55 gol beklentisi 0 sayılır


def yz_skor_tahmini(mac, istatistik):
    """
    İstatistik modeli: önce en olası sonucu (ev / beraberlik / deplasman) seçer,
    sonra beklenen gol sayılarını yuvarlayıp o sonuca uyan skoru yazar.
    Böylece güçlü-zayıf maçta 3-0, dengeli maçta 1-1 gibi çeşitli skorlar çıkar.
    """
    lam_ev, lam_dep, _, _ = yz_beklenen_goller(mac, istatistik)
    p_ev = p_x = p_dep = 0.0
    for i in range(YZ_MAX_GOL + 2):
        for j in range(YZ_MAX_GOL + 2):
            p = _poisson(i, lam_ev) * _poisson(j, lam_dep)
            if i > j: p_ev += p
            elif i < j: p_dep += p
            else: p_x += p

    # Beraberlik nadiren "en olası" çıkar; dengeli maçlarda beraberliği de yaz
    if abs(lam_ev - lam_dep) < 0.3 or p_x >= max(p_ev, p_dep) * 0.9:
        ortalama = (lam_ev + lam_dep) / 2
        g = 0 if ortalama < 0.75 else (2 if ortalama >= 2.1 else 1)
        return g, g
    if p_ev >= p_dep:
        ev = max(1, _yuvarla(lam_ev))
        dep = min(_yuvarla(lam_dep), ev - 1)
        return ev, dep
    dep = max(1, _yuvarla(lam_dep))
    ev = min(_yuvarla(lam_ev), dep - 1)
    return ev, dep


# ---------- İsteğe bağlı: ücretsiz Google Gemini ----------
# GEMINI_API_KEY tanımlıysa Ziya, istatistikleri Gemini'ye verip skoru ona sorar.
# Anahtar ücretsiz: https://aistudio.google.com/apikey  (kart istemez)
# Gemini cevap vermezse/hata verirse istatistik modelinin tahmini kullanılır.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_TIMEOUT = 40


def _yz_gemini_sor(maclar, istatistikler):
    """maclar: [Mac]. Dönüş: {mac_id: (ev, dep)} — hata olursa boş sözlük."""
    if not GEMINI_API_KEY or not maclar:
        return {}
    import json
    satirlar = []
    for mac in maclar:
        ist = istatistikler[mac.lig_id]
        lam_ev, lam_dep, ev_n, dep_n = yz_beklenen_goller(mac, ist)
        def ozet(ad):
            o, atti, yedi = ist["takimlar"].get(takim_adi_normalize(ad), (0, 0, 0))
            son = ist["form"].get(takim_adi_normalize(ad), [])[-5:]
            return {"mac": o, "atti": atti, "yedi": yedi, "son_5_mac": son}
        satirlar.append({
            "id": mac.id,
            "lig": mac.lig,
            "tarih": to_turkiye_saati(mac.baslangic).strftime("%d.%m.%Y %H:%M") if mac.baslangic else "",
            "ev_sahibi": mac.ev_sahibi,
            "deplasman": mac.deplasman,
            "ev_sahibi_istatistik": ozet(mac.ev_sahibi),
            "deplasman_istatistik": ozet(mac.deplasman),
            "model_beklenen_gol": [round(lam_ev, 2), round(lam_dep, 2)],
        })
    istem = (
        "Sen bir futbol skor tahmin oyununda yarışan oyuncusun. Puanlama: tam skor 5 puan, "
        "sadece doğru sonuç (ev/beraberlik/deplasman) 3 puan. Aşağıdaki her maç için gerçekçi bir skor tahmini yap. "
        "Takımların bu ligdeki istatistiklerini, son maçlarını ve kendi futbol bilgini kullan. "
        "Her maça aynı skoru yazma; beraberlikleri de düşün.\n"
        "Sadece şu biçimde JSON dizi döndür: [{\"id\": <maç id>, \"ev\": <gol>, \"dep\": <gol>}]\n\n"
        + json.dumps(satirlar, ensure_ascii=False)
    )
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": istem}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
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
                sonuc[mid] = (ev, dep)
        return sonuc
    except Exception as e:
        app.logger.warning("Gemini tahmini alınamadı: %s", e)
        return {}


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
            ev, dep = gemini.get(mac.id) or yz_skor_tahmini(mac, istatistikler[mac.lig_id])
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
        })
    return satirlar


def _yz_guvenli_calistir(zorla=False):
    try:
        return yz_tahminleri_uret(zorla=zorla)
    except Exception as e:
        db.session.rollback()
        return {"success": False, "error": str(e)}


def akilli_senkron():
    now = utc_now_naive()
    yz_sonuc = _yz_guvenli_calistir()

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

    if request.args.get("ziya") == "1":
        # Robot Ziya'yı Perşembe'yi beklemeden şimdi çalıştır (bu haftanın maçları)
        return jsonify({"tur": "robot ziya", "yapay_zeka": _yz_guvenli_calistir(zorla=True)}), 200

    if request.args.get("tam") == "1":
        try:
            sonuclar = sync_all_leagues()
            _senkron_zamani_yaz("tam")
            return jsonify({"tur": "tam", "sonuclar": sonuclar, "yapay_zeka": _yz_guvenli_calistir()}), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": str(e)}), 500

    try:
        return jsonify(akilli_senkron()), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
