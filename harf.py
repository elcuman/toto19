"""Harf Çarkı: her gün herkese aynı alfabe çarkı. Her harf için bir ipucu,
cevap o harfle başlar (Ğ için: Ğ harfini içerir). Süre bitince ya da tüm
harfler cevaplanınca oyun biter. Cevaplar istemciye oyun bitene kadar gitmez."""
import os
import random

_DIZIN = os.path.dirname(os.path.abspath(__file__))

HARFLER = list("ABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZ")
ICERIR = {"Ğ"}          # bu harflerle kelime başlamaz, "içerir" kuralı
SURE = 300              # saniye
_TR_HARF = set("ABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZ")

_BANKA = None


def _oku(dosya):
    sonuc = []
    try:
        with open(os.path.join(_DIZIN, "kelime", dosya), encoding="utf-8") as f:
            for satir in f:
                satir = satir.strip()
                if not satir or satir.startswith("#") or "|" not in satir:
                    continue
                k, ipucu = satir.split("|", 1)
                k, ipucu = k.strip(), ipucu.strip()
                # Resimli (tek emoji) ipuçları bu oyunda kullanılmaz
                if len(k) >= 3 and set(k) <= _TR_HARF and any(ch.isalpha() for ch in ipucu):
                    sonuc.append((k, ipucu))
    except OSError:
        pass
    return sonuc


def banka():
    """{harf: [(kelime, ipucu), ...]} — her harfin listesi sabit tohumla karıştırılır."""
    global _BANKA
    if _BANKA is None:
        gorulen, tum = set(), []
        for k, i in _oku("harf.txt") + _oku("cengel.txt"):
            if k not in gorulen:
                gorulen.add(k)
                tum.append((k, i))
        b = {}
        for h in HARFLER:
            if h in ICERIR:
                liste = [(k, i) for k, i in tum if h in k]
            else:
                liste = [(k, i) for k, i in tum if k[0] == h]
            liste.sort()
            random.Random("harf-" + h).shuffle(liste)
            b[h] = liste
        _BANKA = b
    return _BANKA


def gunun_sorulari(gun_no):
    """Gün numarasına göre her harf için bir soru. Her harfin listesi baştan sona
    dolaşılır, yani bir harfin kelimesi listesi bitmeden tekrar etmez."""
    b = banka()
    sorular, kullanilan = [], set()
    for h in HARFLER:
        liste = b.get(h) or []
        if not liste:
            continue
        for adim in range(len(liste)):      # aynı gün iki harfe aynı kelime düşmesin (örn. D ve Ğ: DAĞ)
            k, ipucu = liste[(gun_no - 1 + adim) % len(liste)]
            if k not in kullanilan:
                break
        kullanilan.add(k)
        sorular.append({"h": h, "kural": "icerir" if h in ICERIR else "baslar", "k": k, "i": ipucu})
    return sorular


_DUZELT = str.maketrans({"Â": "A", "Î": "İ", "Û": "U", "â": "a", "î": "i", "û": "u"})


def normalle(metin):
    m = (metin or "").translate(_DUZELT).replace("i", "İ").replace("ı", "I").upper()
    return "".join(ch for ch in m if ch in _TR_HARF)


def dogru_mu(soru, cevap):
    return normalle(cevap) == soru["k"]
