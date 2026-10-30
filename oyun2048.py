"""2048: sunucuda hamle tekrarı ile skor doğrulama.
Yeni taşlar tohumlu mulberry32 üreteciyle gelir; tarayıcıdaki JS ile birebir aynı.
İstemci sadece hamle dizisini (L/R/U/D) gönderir, skor burada yeniden hesaplanır."""

M = 0xFFFFFFFF
BOYUT = 4


def _imul(a, b):
    return ((a & M) * (b & M)) & M


class Uretec:
    """JS: function mulberry32(a){ return function(){ a|=0; a=a+0x6D2B79F5|0;
    var t=Math.imul(a^a>>>15,1|a); t=t+Math.imul(t^t>>>7,61|t)^t; return ((t^t>>>14)>>>0)/4294967296; } }"""

    def __init__(self, tohum):
        self.a = tohum & M

    def __call__(self):
        self.a = (self.a + 0x6D2B79F5) & M
        a = self.a
        t = _imul(a ^ (a >> 15), 1 | a)
        t = ((t + _imul(t ^ (t >> 7), 61 | t)) & M) ^ t
        return ((t ^ (t >> 14)) & M) / 4294967296


def _ekle(tahta, rnd):
    bos = [i for i, v in enumerate(tahta) if v == 0]
    if not bos:
        return
    i = bos[int(rnd() * len(bos))]
    tahta[i] = 4 if rnd() < 0.1 else 2


def _kaydir(satir):
    """Bir satırı sola kaydırıp birleştirir. Döner: (yeni_satır, kazanılan_puan)."""
    dolu = [v for v in satir if v]
    sonuc, puan, i = [], 0, 0
    while i < len(dolu):
        if i + 1 < len(dolu) and dolu[i] == dolu[i + 1]:
            sonuc.append(dolu[i] * 2)
            puan += dolu[i] * 2
            i += 2
        else:
            sonuc.append(dolu[i])
            i += 1
    return sonuc + [0] * (BOYUT - len(sonuc)), puan


def _satirlar(yon):
    """Hamle yönüne göre, kaydırma yönü baştan sona olacak şekilde hücre indeksleri."""
    B = BOYUT
    if yon == "L":
        return [[r * B + c for c in range(B)] for r in range(B)]
    if yon == "R":
        return [[r * B + c for c in reversed(range(B))] for r in range(B)]
    if yon == "U":
        return [[r * B + c for r in range(B)] for c in range(B)]
    return [[r * B + c for r in reversed(range(B))] for c in range(B)]


def hamle(tahta, yon):
    """Tahtayı yerinde değiştirir. Döner: (değişti_mi, puan)."""
    degisti, toplam = False, 0
    for idx in _satirlar(yon):
        eski = [tahta[i] for i in idx]
        yeni, puan = _kaydir(eski)
        if yeni != eski:
            degisti = True
            for i, v in zip(idx, yeni):
                tahta[i] = v
        toplam += puan
    return degisti, toplam


def hamle_var_mi(tahta):
    if 0 in tahta:
        return True
    for y in "LURD":
        if hamle(list(tahta), y)[0]:
            return True
    return False


def yeni_tahta(tohum):
    rnd = Uretec(tohum)
    tahta = [0] * (BOYUT * BOYUT)
    _ekle(tahta, rnd)
    _ekle(tahta, rnd)
    return tahta, rnd


def oynat(tohum, hamleler):
    """Hamle dizisini baştan oynatır. Döner: {skor, en_buyuk, hamle, bitti, tahta}."""
    tahta, rnd = yeni_tahta(tohum)
    skor = sayi = 0
    for y in hamleler:
        if y not in "LURD":
            continue
        degisti, puan = hamle(tahta, y)
        if degisti:
            skor += puan
            sayi += 1
            _ekle(tahta, rnd)
    return {"skor": skor, "en_buyuk": max(tahta), "hamle": sayi, "bitti": not hamle_var_mi(tahta), "tahta": tahta}
