"""Çengel Düello: oklu bulmaca (ipuçları kutuların içinde) üzerinde iki kişilik harf yarışı.

Kurallar
- Her oyuncunun ıstakasında 5 harf var. Harfler sadece bulmacanın çözümünde geçen harflerden çekilir.
- Sırası gelen oyuncu ıstakasından bir harfi boş bir kutuya koyar:
    doğruysa +1 puan, o harf koyulunca bir kelime tamamlanırsa + kelimenin harf sayısı kadar bonus;
    yanlışsa puan yok, harf ıstakada kalır.
  Her hamleden sonra sıra rakibe geçer.
- "Değiştir": ıstakadaki harfleri torbadakilerle değiştirir (sıra geçer). "Pas": sıra geçer.
- Her oyuncunun oyun başına 1 ipucu hakkı var: ıstakadaki bir harfin doğru yerini gösterir (sıra geçmez).
- Bulmaca dolunca oyun biter; çok puanı olan kazanır.

Çözüm harfleri istemciye ASLA gönderilmez; her hamleyi sunucu kontrol eder.
"""
import json
import os
import random

SATIR, SUTUN = 9, 7
ISTAKA = 5
TR_HARFLER = set("ABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZ")
_DIZIN = os.path.dirname(os.path.abspath(__file__))
_BANKA = None


def banka():
    """[(kelime, ipucu)] — kelime/cengel.txt dosyasından (bir kez okunur)."""
    global _BANKA
    if _BANKA is None:
        liste, gorulen = [], set()
        try:
            with open(os.path.join(_DIZIN, "kelime", "cengel.txt"), encoding="utf-8") as f:
                for satir in f:
                    satir = satir.strip()
                    if not satir or satir.startswith("#") or "|" not in satir:
                        continue
                    k, ipucu = satir.split("|", 1)
                    k = k.strip()
                    if 2 <= len(k) <= SATIR - 1 and set(k) <= TR_HARFLER and k not in gorulen:
                        gorulen.add(k)
                        liste.append((k, ipucu.strip()))
        except OSError:
            pass
        _BANKA = liste
    return _BANKA


# ------------------------------------------------------------------ üretici
YON = {"a": (0, 1), "d": (1, 0)}     # a: soldan sağa, d: yukarıdan aşağı


def _yerlesebilir(k, r, c, y, harfler, sahip, ipucu_hucre):
    """Kelime (r,c)'den y yönünde konabilir mi? Döner: kesişme sayısı ya da None."""
    dr, dc = YON[y]
    L = len(k)
    if not (0 <= r < SATIR and 0 <= c < SUTUN):
        return None
    sr, sc = r + (L - 1) * dr, c + (L - 1) * dc
    if not (sr < SATIR and sc < SUTUN):
        return None
    # İpucu kutusu: başlangıcın hemen öncesi
    ir, ic = r - dr, c - dc
    if ir < 0 or ic < 0 or (ir, ic) in harfler:
        return None
    if (ir, ic) in ipucu_hucre and y in ipucu_hucre[(ir, ic)]:
        return None
    # Bitişin hemen sonrası harf olmamalı
    br, bc = r + L * dr, c + L * dc
    if br < SATIR and bc < SUTUN and (br, bc) in harfler:
        return None
    kesisme = 0
    for i, h in enumerate(k):
        rr, cc = r + i * dr, c + i * dc
        if (rr, cc) == (ir, ic):
            return None
        if (rr, cc) in ipucu_hucre:
            return None
        if (rr, cc) in harfler:
            if harfler[(rr, cc)] != h or y in sahip[(rr, cc)]:
                return None
            kesisme += 1
        else:
            # Yan komşular harf olmamalı (istenmeyen kelime oluşmasın)
            for yr, yc in ((rr + dc, cc + dr), (rr - dc, cc - dr)):
                if (yr, yc) in harfler:
                    return None
    if kesisme == L:
        return None
    return kesisme


def _dene(rnd, kelimeler, hedef_kelime=16):
    harfler, sahip, ipucu_hucre, yerlesen, kullanilan = {}, {}, {}, [], set()

    def koy(k, ip, r, c, y):
        dr, dc = YON[y]
        for i, h in enumerate(k):
            harfler[(r + i * dr, c + i * dc)] = h
            sahip.setdefault((r + i * dr, c + i * dc), set()).add(y)
        ipucu_hucre.setdefault((r - dr, c - dc), {})[y] = len(yerlesen)
        yerlesen.append({"k": k, "i": ip, "r": r, "c": c, "y": y})
        kullanilan.add(k)

    # İlk kelime: uzunca, yatay
    adaylar = [w for w in kelimeler if 4 <= len(w[0]) <= SUTUN - 1]
    k, ip = rnd.choice(adaylar)
    r = rnd.randint(1, SATIR - 1)
    c = rnd.randint(1, SUTUN - len(k))
    if _yerlesebilir(k, r, c, "a", harfler, sahip, ipucu_hucre) is None:
        return None
    koy(k, ip, r, c, "a")

    basarisiz = 0
    while len(yerlesen) < hedef_kelime and basarisiz < 260:
        k, ip = rnd.choice(kelimeler)
        if k in kullanilan:
            basarisiz += 1
            continue
        secenekler = []
        for (hr, hc), harf in list(harfler.items()):
            for i, h in enumerate(k):
                if h != harf:
                    continue
                for y, (dr, dc) in YON.items():
                    if y in sahip[(hr, hc)]:
                        continue
                    r0, c0 = hr - i * dr, hc - i * dc
                    ks = _yerlesebilir(k, r0, c0, y, harfler, sahip, ipucu_hucre)
                    if ks:
                        secenekler.append((ks, r0, c0, y))
        if not secenekler:
            basarisiz += 1
            continue
        en = max(s[0] for s in secenekler)
        _, r0, c0, y = rnd.choice([s for s in secenekler if s[0] == en])
        koy(k, ip, r0, c0, y)
        basarisiz = 0
    return harfler, ipucu_hucre, yerlesen


def bulmaca_uret(tohum=None, deneme=70):
    """Yeni bir bulmaca üretir. Döner: JSON'a yazılabilir sözlük (çözüm dahil — sunucuda kalır)."""
    rnd = random.Random(tohum)
    kelimeler = banka()
    en_iyi, en_iyi_puan = None, -1
    for _ in range(deneme):
        s = _dene(rnd, kelimeler)
        if not s:
            continue
        harfler, ipucu_hucre, yerlesen = s
        puan = len(harfler) * 2 + len(yerlesen) * 3
        if len(yerlesen) >= 7 and puan > en_iyi_puan:
            en_iyi, en_iyi_puan = s, puan
    if en_iyi is None:
        raise RuntimeError("Bulmaca üretilemedi")
    harfler, ipucu_hucre, yerlesen = en_iyi
    return {
        "satir": SATIR, "sutun": SUTUN,
        "cozum": {f"{r},{c}": h for (r, c), h in harfler.items()},
        "ipuclari": {f"{r},{c}": v for (r, c), v in ipucu_hucre.items()},
        "kelimeler": yerlesen,
    }


def kelime_hucreleri(kel):
    dr, dc = YON[kel["y"]]
    return [f"{kel['r'] + i * dr},{kel['c'] + i * dc}" for i in range(len(kel["k"]))]


# ------------------------------------------------------------------ oyun durumu
_HAVUZ = None


def bulmaca_havuzu():
    """Önceden üretilmiş en dolu bulmacalar (kelime/cengel_bulmacalar.json)."""
    global _HAVUZ
    if _HAVUZ is None:
        try:
            with open(os.path.join(_DIZIN, "kelime", "cengel_bulmacalar.json"), encoding="utf-8") as f:
                _HAVUZ = json.load(f)
        except (OSError, ValueError):
            _HAVUZ = []
    return _HAVUZ


def yeni_oyun(tohum=None, haric=()):
    """haric: oyuncuların yakın zamanda oynadığı bulmaca numaraları (tekrar gelmesin)."""
    rnd = random.Random(tohum)
    havuz = bulmaca_havuzu()
    if havuz:
        secilebilir = [i for i in range(len(havuz)) if i not in set(haric)] or list(range(len(havuz)))
        no = rnd.choice(secilebilir)
        b = json.loads(json.dumps(havuz[no]))
        b["no"] = no
    else:
        b = bulmaca_uret(tohum=rnd.random())
    torba = list(b["cozum"].values())
    rnd.shuffle(torba)
    raf = [[torba.pop() for _ in range(min(ISTAKA, len(torba)))] for _ in range(2)]
    return {"bulmaca": b, "dolu": {}, "puan": [0, 0], "sira": 0, "raf": raf, "torba": torba,
            "ipucu": [False, False], "son": [], "bitti": False, "hamle": 0}


def _kayit(d, kayit):
    d["son"] = (d["son"] + [kayit])[-6:]
    d["hamle"] += 1


TUR_HAKKI = 3   # bir turda en fazla kaç harf denenebilir


def tur_hakki(d):
    return d.get("tur_hak", TUR_HAKKI)


def _sira_gec(d):
    d["sira"] = 1 - d["sira"]
    d["tur_hak"] = TUR_HAKKI


def _hak_kullan(d):
    """Bir deneme hakkı düşer; hak bitince sıra rakibe geçer."""
    d["tur_hak"] = tur_hakki(d) - 1
    if d["tur_hak"] <= 0 and not d["bitti"]:
        _sira_gec(d)


def _bitti_mi(d):
    if len(d["dolu"]) >= len(d["bulmaca"]["cozum"]):
        d["bitti"] = True
    return d["bitti"]


def harf_koy(d, oyuncu, raf_i, hucre, rnd=None):
    """Döner: (ok, mesaj). Her deneme (doğru/yanlış) tur hakkından düşer; 3 deneme sonunda sıra geçer."""
    rnd = rnd or random.Random()
    if d["bitti"]:
        return False, "Oyun bitti."
    if d["sira"] != oyuncu:
        return False, "Sıra sende değil."
    raf = d["raf"][oyuncu]
    if not (0 <= raf_i < len(raf)):
        return False, "Önce bir harf seç."
    cozum = d["bulmaca"]["cozum"]
    if hucre not in cozum:
        return False, "Harfler sadece beyaz kutulara konur."
    if hucre in d["dolu"]:
        return False, "Bu kutu dolu."
    harf = raf[raf_i]
    if cozum[hucre] != harf:
        _kayit(d, {"o": oyuncu, "t": "yanlis", "h": harf, "x": hucre, "p": 0})
        _hak_kullan(d)
        return True, "yanlis"
    d["dolu"][hucre] = oyuncu
    puan, tamam = 1, []
    for kel in d["bulmaca"]["kelimeler"]:
        hc = kelime_hucreleri(kel)
        if hucre in hc and all(h in d["dolu"] for h in hc):
            puan += len(kel["k"])
            tamam.append(kel["k"])
    d["puan"][oyuncu] += puan
    raf.pop(raf_i)
    if d["torba"]:
        raf.append(d["torba"].pop(rnd.randrange(len(d["torba"]))))
    _kayit(d, {"o": oyuncu, "t": "dogru", "h": harf, "x": hucre, "p": puan, "k": tamam})
    _bitti_mi(d)
    if not d["bitti"]:
        if not raf:
            _sira_gec(d)     # ıstaka boşaldı, bu turda koyacak harf kalmadı
        else:
            _hak_kullan(d)
    return True, "dogru"


def degistir(d, oyuncu, rnd=None):
    rnd = rnd or random.Random()
    if d["bitti"] or d["sira"] != oyuncu:
        return False, "Sıra sende değil."
    if tur_hakki(d) < TUR_HAKKI:
        return False, "Harf koyduğun turda değiştiremezsin. Turu bitir."
    if not d["torba"]:
        return False, "Torbada değiştirecek harf kalmadı."
    eski = d["raf"][oyuncu]
    d["torba"].extend(eski)
    rnd.shuffle(d["torba"])
    d["raf"][oyuncu] = [d["torba"].pop() for _ in range(min(len(eski), len(d["torba"])))]
    _kayit(d, {"o": oyuncu, "t": "degistir", "p": 0})
    _sira_gec(d)
    return True, "degistir"


PAS_HAKKI = 1   # oyun başına harf koymadan pas geçme hakkı


def pas_kaldi(d, oyuncu):
    return PAS_HAKKI - d.get("pas", [0, 0])[oyuncu]


def pas(d, oyuncu, otomatik=False):
    """otomatik: ıstaka boşken ya da robotun çaresiz kaldığı durumda sıra geçer, hak düşmez.
    Harf koyduktan sonra turu bitirmek pas sayılmaz."""
    if d["bitti"] or d["sira"] != oyuncu:
        return False, "Sıra sende değil."
    if not otomatik and tur_hakki(d) == TUR_HAKKI:
        if pas_kaldi(d, oyuncu) <= 0:
            return False, "Pas hakkını kullandın. Bir harf dene ya da harflerini değiştir."
        d.setdefault("pas", [0, 0])[oyuncu] += 1
    _kayit(d, {"o": oyuncu, "t": "pas" if tur_hakki(d) == TUR_HAKKI else "bitir", "p": 0})
    _sira_gec(d)
    return True, "pas"


def ipucu(d, oyuncu, rnd=None):
    """Istakadaki bir harfin doğru yerini döner (sıra geçmez). Oyun başına 1 hak."""
    rnd = rnd or random.Random()
    if d["bitti"] or d["sira"] != oyuncu:
        return False, "Sıra sende değil.", None
    if d["ipucu"][oyuncu]:
        return False, "İpucu hakkını kullandın.", None
    secenek = _dogru_hamleler(d, oyuncu)
    if not secenek:
        return False, "Istakandaki harfler için yer yok, değiştirmeyi dene.", None
    d["ipucu"][oyuncu] = True
    i, hucre, _ = rnd.choice(secenek)
    return True, "ipucu", {"i": i, "x": hucre}


def _dogru_hamleler(d, oyuncu):
    cozum, dolu = d["bulmaca"]["cozum"], d["dolu"]
    sonuc = []
    for i, h in enumerate(d["raf"][oyuncu]):
        for hucre, dogru in cozum.items():
            if dogru == h and hucre not in dolu:
                bonus = 0
                for kel in d["bulmaca"]["kelimeler"]:
                    hc = kelime_hucreleri(kel)
                    if hucre in hc and all(x in dolu or x == hucre for x in hc):
                        bonus += len(kel["k"])
                sonuc.append((i, hucre, bonus))
    return sonuc


def kazanan(d):
    """0 / 1 ya da -1 (berabere). Oyundan çekilen puanı ne olursa olsun kaybeder."""
    if d.get("cekilen") in (0, 1):
        return 1 - d["cekilen"]
    p = d["puan"]
    return -1 if p[0] == p[1] else (0 if p[0] > p[1] else 1)


def cekil(d, oyuncu):
    if d["bitti"]:
        return False, "Oyun zaten bitti."
    d["bitti"] = True
    d["cekilen"] = oyuncu
    _kayit(d, {"o": oyuncu, "t": "cekil", "p": 0})
    return True, "cekil"


BOT_ZORLUK = {"kolay": 0.45, "orta": 0.68, "zor": 0.88}


def bot_oyna(d, oyuncu=1, zorluk="orta", rnd=None):
    """Robot Ziya'nın hamlesi: zorluğa göre doğru yer bulur (kelime tamamlayanı tercih eder) ya da yanılır."""
    rnd = rnd or random.Random()
    if d["bitti"] or d["sira"] != oyuncu:
        return
    if not d["raf"][oyuncu]:
        pas(d, oyuncu, otomatik=True)       # ıstaka boş: kalan harfler rakipte
        return
    secenek = _dogru_hamleler(d, oyuncu)
    if secenek and rnd.random() < BOT_ZORLUK.get(zorluk, 0.68):
        en = max(s[2] for s in secenek)
        havuz = [s for s in secenek if s[2] == en] if (en and rnd.random() < 0.8) else secenek
        i, hucre, _ = rnd.choice(havuz)
        harf_koy(d, oyuncu, i, hucre, rnd)
        return
    bos = [h for h in d["bulmaca"]["cozum"] if h not in d["dolu"]]
    if not secenek:
        if d["torba"] and tur_hakki(d) == TUR_HAKKI:
            degistir(d, oyuncu, rnd)
        else:
            pas(d, oyuncu, otomatik=True)
        return
    # Yanılma: yanlış bir kutuya harf dener
    for _ in range(20):
        i = rnd.randrange(len(d["raf"][oyuncu]))
        hucre = rnd.choice(bos)
        if d["bulmaca"]["cozum"][hucre] != d["raf"][oyuncu][i]:
            harf_koy(d, oyuncu, i, hucre, rnd)
            return
    pas(d, oyuncu, otomatik=True)


def istemci_gorunumu(d, oyuncu):
    """Oyuncuya gönderilecek durum: çözüm YOK, rakibin ıstakası YOK."""
    b = d["bulmaca"]
    hucreler = {}
    for hucre in b["cozum"]:
        if hucre in d["dolu"]:
            hucreler[hucre] = {"h": b["cozum"][hucre], "o": d["dolu"][hucre]}
        else:
            hucreler[hucre] = {}
    ipuclari = {}
    for hucre, v in b["ipuclari"].items():
        ipuclari[hucre] = {y: b["kelimeler"][i]["i"] for y, i in v.items()}
    tamam = []
    for kel in b["kelimeler"]:
        hc = kelime_hucreleri(kel)
        if all(h in d["dolu"] for h in hc):
            tamam.extend(hc)
    son = []
    for s in d["son"]:
        s2 = dict(s)
        if s2.get("t") == "yanlis":
            s2.pop("h", None)        # yanlış denenen harf de gizli kalsın (ipucu olmasın)
        son.append(s2)
    return {"satir": b["satir"], "sutun": b["sutun"], "hucreler": hucreler, "ipuclari": ipuclari,
            "tamam": sorted(set(tamam)), "puan": d["puan"], "sira": d["sira"], "ben": oyuncu,
            "raf": d["raf"][oyuncu] if oyuncu in (0, 1) else [], "torba": len(d["torba"]),
            "ipucu_kaldi": (not d["ipucu"][oyuncu]) if oyuncu in (0, 1) else False,
            "son": son, "bitti": d["bitti"], "hamle": d["hamle"], "cekilen": d.get("cekilen"), "hak": tur_hakki(d), "tur_hakki": TUR_HAKKI,
            "pas_kaldi": pas_kaldi(d, oyuncu) if oyuncu in (0, 1) else 0,
            "kalan": len(b["cozum"]) - len(d["dolu"])}
