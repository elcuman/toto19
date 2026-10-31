"""Haftanın Kralı paylaşım kartı: WhatsApp/Instagram'a gönderilecek PNG görsel.

Saf Pillow ile çizilir (tarayıcı/harici servis yok). 1080x1350 (4:5), sitenin
renkleriyle: çim yeşili zemin, sarı kart = kral, kırmızı kart = cenabet.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

GEN, YUK = 1080, 1350
FONT_DIZINI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")

CIM_KOYU = (8, 74, 46)
CIM = (14, 122, 76)
CIZGI = (255, 255, 255, 26)
BEYAZ = (255, 255, 255)
MUREKKEP = (20, 32, 26)
GRI = (94, 107, 100)
SARI = (243, 198, 35)
SARI_ACIK = (253, 244, 211)
KIRMIZI = (214, 69, 56)
CIM_ACIK = (229, 242, 235)

_font_onbellek = {}


def _font(ad, boy):
    anahtar = (ad, boy)
    if anahtar not in _font_onbellek:
        try:
            _font_onbellek[anahtar] = ImageFont.truetype(os.path.join(FONT_DIZINI, ad), boy)
        except OSError:
            _font_onbellek[anahtar] = ImageFont.load_default(boy)
    return _font_onbellek[anahtar]


def BASLIK(boy):
    return _font("BarlowCondensed-Bold.ttf", boy)


def BASLIK_YARI(boy):
    return _font("BarlowCondensed-SemiBold.ttf", boy)


def YAZI(boy):
    return _font("Barlow-SemiBold.ttf", boy)


def YAZI_ORTA(boy):
    return _font("Barlow-Medium.ttf", boy)


def _genislik(d, metin, font):
    x0, _, x1, _ = d.textbbox((0, 0), metin, font=font)
    return x1 - x0


def _sigdir(d, metin, font_fn, boy, max_gen, min_boy=28):
    """Metni max_gen'e sığana kadar küçültür; yine sığmazsa sonunu '…' ile keser."""
    while boy > min_boy and _genislik(d, metin, font_fn(boy)) > max_gen:
        boy -= 2
    font = font_fn(boy)
    if _genislik(d, metin, font) > max_gen:
        while len(metin) > 1 and _genislik(d, metin + "…", font) > max_gen:
            metin = metin[:-1]
        metin += "…"
    return metin, font


def _ortala(d, y, metin, font, renk):
    d.text(((GEN - _genislik(d, metin, font)) / 2, y), metin, font=font, fill=renk)


def _kart_isareti(d, x, y, renk, gen=34, yuk=46, aci=0):
    """Hakem kartı: küçük yuvarlatılmış dikdörtgen (döndürmeli)."""
    k = Image.new("RGBA", (gen + 8, yuk + 8), (0, 0, 0, 0))
    kd = ImageDraw.Draw(k)
    kd.rounded_rectangle((4, 4, gen + 4, yuk + 4), radius=6, fill=renk)
    if aci:
        k = k.rotate(aci, expand=True, resample=Image.BICUBIC)
    return k, (int(x), int(y))


def _tac(d, cx, ust, gen=170, renk=SARI):
    """Basit taç: tabanı dikdörtgen, üstte üç sivri uç ve uçlarda toplar."""
    yuk = gen * 0.62
    sol, sag = cx - gen / 2, cx + gen / 2
    taban_ust = ust + yuk * 0.72
    alt = ust + yuk
    noktalar = [
        (sol, taban_ust), (sol, ust + yuk * 0.18), (cx - gen * 0.22, ust + yuk * 0.48),
        (cx, ust), (cx + gen * 0.22, ust + yuk * 0.48), (sag, ust + yuk * 0.18), (sag, taban_ust),
    ]
    d.polygon(noktalar, fill=renk)
    d.rounded_rectangle((sol, taban_ust - 2, sag, alt), radius=6, fill=renk)
    r = gen * 0.06
    for px, py in [(sol, ust + yuk * 0.18), (cx, ust), (sag, ust + yuk * 0.18)]:
        d.ellipse((px - r, py - r, px + r, py + r), fill=renk)
    # tabandaki taşlar
    for i, px in enumerate([cx - gen * 0.25, cx, cx + gen * 0.25]):
        tr = gen * 0.045
        ty = (taban_ust + alt) / 2
        d.ellipse((px - tr, ty - tr, px + tr, ty + tr), fill=CIM_KOYU if i != 1 else KIRMIZI)


def _saha_cizgileri(img):
    """Arka plana silik saha çizgileri: orta çizgi, orta yuvarlak, ceza sahaları."""
    katman = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(katman)
    w = 4
    d.rectangle((40, 40, GEN - 40, YUK - 40), outline=CIZGI, width=w)
    d.line((40, YUK / 2, GEN - 40, YUK / 2), fill=CIZGI, width=w)
    r = 150
    d.ellipse((GEN / 2 - r, YUK / 2 - r, GEN / 2 + r, YUK / 2 + r), outline=CIZGI, width=w)
    d.rectangle((GEN / 2 - 260, 40, GEN / 2 + 260, 230), outline=CIZGI, width=w)
    d.rectangle((GEN / 2 - 260, YUK - 230, GEN / 2 + 260, YUK - 40), outline=CIZGI, width=w)
    img.alpha_composite(katman)


def kart_ciz(veri):
    """
    veri = {
      "grup_ad": str, "ust_baslik": "HAFTANIN KRALI" | "ŞU AN ZİRVEDE",
      "tarih": "10–17 Ekim", "kral": {"ad", "puan"}, "ortak_kral": int (aynı puandaki diğer kişi sayısı),
      "liste": [{"sira","ad","puan","bot"}...] (en fazla 5), "cenabet": {"ad","puan"} | None,
      "alt_yazi": "skorbilen.com",
    }
    """
    img = Image.new("RGBA", (GEN, YUK), CIM_KOYU + (255,))
    # Yukarıdan aşağı hafif koyulaşan zemin
    zemin = Image.new("RGBA", (1, YUK))
    for y in range(YUK):
        t = y / YUK
        zemin.putpixel((0, y), (int(CIM[0] * (1 - t) + CIM_KOYU[0] * t),
                                int(CIM[1] * (1 - t) + CIM_KOYU[1] * t),
                                int(CIM[2] * (1 - t) + CIM_KOYU[2] * t), 255))
    img.alpha_composite(zemin.resize((GEN, YUK)))
    _saha_cizgileri(img)
    d = ImageDraw.Draw(img)

    # Üst: marka + grup adı
    _ortala(d, 78, "SKORBİLEN", BASLIK_YARI(34), (255, 255, 255, 200))
    grup, f = _sigdir(d, veri.get("grup_ad") or "", BASLIK, 58, GEN - 160)
    _ortala(d, 120, grup, f, BEYAZ)
    if veri.get("tarih"):
        _ortala(d, 190, veri["tarih"], YAZI_ORTA(30), (255, 255, 255, 190))

    # Taç + başlık
    _tac(d, GEN / 2, 268, gen=150)
    baslik = veri.get("ust_baslik") or "HAFTANIN KRALI"
    bf = BASLIK(54)
    bw = _genislik(d, baslik, bf) + 56
    d.rounded_rectangle(((GEN - bw) / 2, 380, (GEN + bw) / 2, 446), radius=33, fill=SARI)
    _ortala(d, 381, baslik, bf, MUREKKEP)

    # Kralın adı ve puanı
    kral = veri.get("kral") or {}
    ad, f = _sigdir(d, kral.get("ad", "?"), BASLIK, 132, GEN - 140, min_boy=60)
    _ortala(d, 460, ad, f, BEYAZ)
    puan_yazi = f"{kral.get('puan', 0)} PUAN"
    if veri.get("ortak_kral"):
        puan_yazi += f"  ·  +{veri['ortak_kral']} ortak lider"
    _ortala(d, 612, puan_yazi, BASLIK_YARI(46), SARI)

    # Sıralama paneli
    liste = veri.get("liste") or []
    px0, px1, py0 = 110, GEN - 110, 690
    satir_h = 70
    py1 = py0 + 30 + satir_h * max(1, len(liste)) + 10
    d.rounded_rectangle((px0, py0, px1, py1), radius=28, fill=BEYAZ)
    y = py0 + 22
    for i, s in enumerate(liste):
        birinci = s.get("sira", i + 1) == 1
        if birinci:
            d.rounded_rectangle((px0 + 14, y - 6, px1 - 14, y + satir_h - 14), radius=18, fill=SARI_ACIK)
        sira = str(s.get("sira", i + 1))
        d.text((px0 + 44, y + 4), sira, font=BASLIK(46), fill=MUREKKEP if birinci else GRI)
        ad_metni = s.get("ad", "?") + ("  (bot)" if s.get("bot") else "")
        ad_kes, af = _sigdir(d, ad_metni, YAZI, 40, px1 - px0 - 260, min_boy=26)
        d.text((px0 + 110, y + 8), ad_kes, font=af, fill=MUREKKEP)
        p = str(s.get("puan", 0))
        pf = BASLIK(50)
        d.text((px1 - 44 - _genislik(d, p, pf), y), p, font=pf, fill=CIM if birinci else MUREKKEP)
        y += satir_h

    # Cenabet
    cen = veri.get("cenabet")
    cy = py1 + 34
    if cen:
        k, (kx, ky) = _kart_isareti(d, 0, 0, KIRMIZI, gen=40, yuk=56, aci=-12)
        metin = f"Haftanın cenabeti: {cen.get('ad', '?')} ({cen.get('puan', 0)})"
        metin, cf = _sigdir(d, metin, YAZI, 38, GEN - 280, min_boy=26)
        tw = _genislik(d, metin, cf)
        x0 = (GEN - (tw + 70)) / 2
        img.alpha_composite(k, (int(x0), int(cy - 6)))
        d.text((x0 + 70, cy), metin, font=cf, fill=BEYAZ)

    # Alt yazı
    alt = veri.get("alt_yazi") or "skorbilen.com"
    _ortala(d, YUK - 124, "Sen de tahminini gir, kralı tahtından indir!", YAZI_ORTA(34), (255, 255, 255, 215))
    _ortala(d, YUK - 84, alt, BASLIK(44), SARI)

    cikti = io.BytesIO()
    img.convert("RGB").save(cikti, "PNG", optimize=True)
    return cikti.getvalue()
