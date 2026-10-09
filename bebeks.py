# -*- coding: utf-8 -*-
# bebeks.py — Ultra Bebek AI v2

import os, re, random, pickle, requests, datetime, math

# ⚙️ AYARLAR
API_KEY = "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"
MODEL   = "openai/gpt-oss-120b"
URL     = "https://api.groq.com/openai/v1/chat/completions"
KLASOR  = "ultra_bilgi"
HAFIZA  = "ultra_hafiza.pkl"
os.makedirs(KLASOR, exist_ok=True)

# Renkler
C = {"kirmizi":"\033[91m","yesil":"\033[92m","sari":"\033[93m",
     "mavi":"\033[94m","mor":"\033[95m","cyan":"\033[96m","sifir":"\033[0m"}

def r(text, renk="yesil"):
    return f"{C[renk]}{text}{C['sifir']}"

KATEGORILER = {
    "selamlasma": ["selam","merhaba","hey","naber","napıyorsun","nasılsın",
                   "günaydın","iyi akşamlar","alo","aleyküm","meraba","slm"],
    "veda":       ["hoşçakal","görüşürüz","bay","güle güle","iyi geceler","çıkıyorum"],
    "tesekkur":   ["teşekkür","sağol","eyvallah","minnet","tşk"],
    "ozur":       ["özür","pardon","kusura","affet"],
    "duygu":      ["üzgün","mutlu","kızgın","sinirli","sevgi","korku",
                   "yalnız","heyecan","moral","stres","kaygı","depres","sıkıldım"],
    "gunluk":     ["bugün","yarın","dün","sabah","akşam","gece",
                   "hava","yemek","uyku","iş","okul","tatil"],
    "yardim":     ["yardım","nasıl","ne yapmalı","tavsiye","öneri","fikir"],
    "matematik":  ["topla","çarp","böl","çıkar","kaç","hesap","yüzde","karekök"],
    "bilgi":      ["nedir","ne demek","kim","nerede","neden","niçin"],
    "kod":        ["python","javascript","kod","program","yazılım","hata","bug","fonksiyon"],
    "oyun":       ["oyun","minecraft","roblox","valorant","csgo","fifa","pubg"],
    "yemek":      ["tarif","yemek","pizza","makarna","çorba","tatlı","kahvaltı"],
    "spor":       ["futbol","basketbol","maç","takım","antrenman","koşu"],
    "tarih":      ["tarih","osmanlı","savaş","cumhuriyet","eski","yıl"],
    "saglik":     ["sağlık","hasta","doktor","ilaç","ağrı","mide"],
    "teknoloji":  ["telefon","bilgisayar","internet","yapay zeka","ai","robot"],
    "sanat":      ["müzik","resim","film","dizi","kitap","şiir"],
    "ask":        ["aşk","sevgili","ilişki","kalp","flört","evlilik"],
    "para":       ["para","maaş","kariyer","yatırım","borsa"],
    "egitim":     ["okul","üniversite","sınav","ders","ödev","öğretmen"],
}

# ------------------------------------------------------------
def llm(system, user, temp=0.85, max_tok=8000):
    try:
        r_ = requests.post(URL,
            headers={"Authorization": f"Bearer {API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": MODEL,
                  "messages":[{"role":"system","content":system},
                              {"role":"user","content":user}],
                  "temperature": temp, "max_tokens": max_tok},
            timeout=180)
        if r_.status_code != 200:
            print(r(f"[hata {r_.status_code}] {r_.text[:120]}", "kirmizi"))
            return None
        return r_.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(r(f"[bağlantı] {e}", "kirmizi")); return None

def temizle_liste(t):
    m = re.search(r"\[.*\]", t, re.S)
    return m.group(0) if m else "[]"

def kategori_bul(s):
    s = s.lower()
    for kat, kel in KATEGORILER.items():
        if any(k in s for k in kel): return kat
    return "bilgi"

# ------------------------------------------------------------
class Hafiza:
    def __init__(self):
        self.data = {
            "kategoriler": {}, "bilgi": {}, "gecmis": [],
            "ogrettiklerim": {}, "notlar": [], "kelimeler": {},
            "mod": "normal", "dogum": None
        }
        self._yukle()
    def _yukle(self):
        if os.path.exists(HAFIZA):
            try:
                with open(HAFIZA,"rb") as f:
                    y = pickle.load(f)
                for k,v in y.items():
                    if k in self.data: self.data[k] = v
            except Exception: pass
    def kaydet(self):
        with open(HAFIZA,"wb") as f:
            pickle.dump(self.data, f, protocol=pickle.HIGHEST_PROTOCOL)
    def ekle_bilgi(self, k, v):
        self.data["bilgi"][k] = v; self.kaydet()
    def ekle_gecmis(self, s, c):
        self.data["gecmis"].append((s,c, datetime.datetime.now().isoformat()))
        self.data["gecmis"] = self.data["gecmis"][-1000:]
        self.kaydet()

# ------------------------------------------------------------
def ogren(kategori, hafiza):
    if kategori in hafiza.data["kategoriler"]: return True
    dosya = os.path.join(KLASOR, f"{kategori}.py")
    if os.path.exists(dosya):
        hafiza.data["kategoriler"][kategori] = True; hafiza.kaydet(); return True
    print(r(f"[abiye soruyorum: {kategori}] ...", "sari"))
    sys = f""""{kategori}" kategorisinde 100 farklı kullanıcı mesajı ve her birine 5 farklı cevap üret.
Sadece bu formatta Python listesi yaz:
SONUC = [
    ("mesaj1", ["cevap1a","cevap1b","cevap1c","cevap1d","cevap1e"]),
    ... 100 tane
]
Kurallar:
- Cevaplar kısa, doğal Türkçe, 1-2 cümle
- Her cevap farklı
- Selamlaşma ise varyasyonlar (selam, Selam, SELAM, selamün aleyküm)
- Matematik ise gerçek hesap
- Sadece liste"""
    cevap = llm(sys, kategori, max_tok=8000)
    if not cevap: return False
    kod = temizle_liste(cevap)
    with open(dosya, "w", encoding="utf-8") as f:
        f.write(f"# -*- coding: utf-8 -*-\nSONUC = {kod}\n")
    try:
        ns = {}; exec(open(dosya, encoding="utf-8").read(), ns)
        n = len(ns.get("SONUC", []))
        hafiza.data["kategoriler"][kategori] = True; hafiza.kaydet()
        print(r(f"[öğrendim] {kategori} → {n} soru × 5 = {n*5} cevap", "yesil"))
        return True
    except Exception as e:
        print(r(f"[hata] {e}", "kirmizi")); return False

def kategori_liste_al(kategori):
    dosya = os.path.join(KLASOR, f"{kategori}.py")
    if not os.path.exists(dosya): return []
    ns = {}; exec(open(dosya, encoding="utf-8").read(), ns)
    return ns.get("SONUC", [])

# ------------------------------------------------------------
def benzerlik(a, b):
    """Basit kelime benzerliği 0-1."""
    ka = set(re.findall(r"\w+", a.lower()))
    kb = set(re.findall(r"\w+", b.lower()))
    if not ka or not kb: return 0
    return len(ka & kb) / len(ka | kb)

def en_iyi_cevap(soru, liste):
    if not liste: return "..."
    s_low = soru.lower().strip()
    # 1) Birebir
    for s, cevaplar in liste:
        if s.lower() == s_low:
            return random.choice(cevaplar)
    # 2) Kısmi
    uyanlar = []
    for s, cevaplar in liste:
        if s_low in s.lower() or s.lower() in s_low:
            uyanlar.extend(cevaplar)
    if uyanlar: return random.choice(uyanlar)
    # 3) Benzerlik
    en_iyi, en_skor = None, 0
    for s, cevaplar in liste:
        sk = benzerlik(s_low, s)
        if sk > en_skor:
            en_skor = sk; en_iyi = cevaplar
    if en_iyi and en_skor > 0.15:
        return random.choice(en_iyi)
    # 4) Rastgele
    return random.choice(random.choice(liste)[1])

# ------------------------------------------------------------
def matematik_coz(soru, hafiza):
    """Direkt hesaplama."""
    try:
        if "karekök" in soru.lower():
            n = float(re.search(r"-?\d+\.?\d*", soru).group())
            return f"√{n} = {math.sqrt(n):.4f}"
        # Basit ifade
        ifade = re.sub(r"[^\d+\-*/(). ]", "", soru)
        if ifade.strip() and any(op in ifade for op in "+-*/"):
            sonuc = eval(ifade, {"__builtins__":{}}, {})
            return f"{ifade.strip()} = {sonuc}"
    except Exception: pass
    return None

def not_ekle(hafiza, notu):
    hafiza.data["notlar"].append({
        "not": notu, "tarih": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    hafiza.kaydet()
    return f"Not eklendi: {notu}"

def notlari_goster(hafiza):
    if not hafiza.data["notlar"]: return "Not yok."
    out = ["📝 Notlar:"]
    for i, n in enumerate(hafiza.data["notlar"][-20:], 1):
        out.append(f"  {i}. {n['not']}  ({n['tarih']})")
    return "\n".join(out)

def mod_ayarla(hafiza, mod):
    hafiza.data["mod"] = mod
    hafiza.kaydet()
    return f"Mod: {mod}"

def cevapla(soru, hafiza):
    s_low = soru.lower().strip()

    # 🔹 Mod kontrolü — komik/ciddi/samimi
    mod = hafiza.data.get("mod", "normal")

    # 🔹 Selamlama başlat (rastgele)
    if s_low in ("merhaba","selam","hey","naber") and random.random() < 0.3:
        return random.choice([
            "Merhaba! Bugün nasılsın?",
            "Selam! Ne yapıyorsun?",
            "Hey! Ne var ne yok?"
        ])

    # 🔹 Saat
    if "saat" in s_low and ("kaç" in s_low or "ne" in s_low):
        return "Saat: " + datetime.datetime.now().strftime("%H:%M:%S")

    # 🔹 Tarih
    if "tarih" in s_low or "bugün ayın" in s_low:
        return "Tarih: " + datetime.datetime.now().strftime("%d %B %Y, %A")

    # 🔹 Not ekle
    if s_low.startswith("not ekle ") or s_low.startswith("not al "):
        return not_ekle(hafiza, soru[9:].strip())

    if s_low in ("notlar","notları göster","notlarım"):
        return notlari_goster(hafiza)

    # 🔹 Mod
    if s_low.startswith("mod "):
        return mod_ayarla(hafiza, soru[4:].strip())

    # 🔹 Kişisel bilgi kaydet
    m = re.match(r"(?:benim\s+)?(adım|ismim|yaşım|şehrim|mesleğim|okulum|doğum günüm)\s+(.+)", s_low)
    if m:
        hafiza.ekle_bilgi(m.group(1), m.group(2).strip())
        return f"Tamam, {m.group(1)} = {m.group(2).strip()} kaydettim."

    if "adım ne" in s_low or "ismim ne" in s_low:
        return f"Adın: {hafiza.data['bilgi'].get('adım','bilmiyorum')}"
    if "yaşım kaç" in s_low:
        return f"Yaşın: {hafiza.data['bilgi'].get('yaşım','bilmiyorum')}"

    # 🔹 Matematik direkt
    if any(k in s_low for k in ["kaç eder","kaç yapar","hesapla","+","-","*","/","karekök"]):
        mat = matematik_coz(soru, hafiza)
        if mat: return mat

    # 🔹 Öğrettiklerim
    if s_low in hafiza.data["ogrettiklerim"]:
        return random.choice(hafiza.data["ogrettiklerim"][s_low])

    # 🔹 Kategori bul + öğren + cevapla
    kategori = kategori_bul(soru)
    ogren(kategori, hafiza)
    liste = kategori_liste_al(kategori)
    cevap = en_iyi_cevap(soru, liste)

    # Mod'a göre süsle
    if mod == "komik" and random.random() < 0.3:
        cevap += " 😄"
    elif mod == "samimi" and random.random() < 0.3:
        cevap += " kanka"

    return cevap

# ------------------------------------------------------------
def main():
    hafiza = Hafiza()
    print(r("=" * 55, "cyan"))
    print(r(" 🤖 BEBEK ULTRA v2", "cyan"))
    print(r("=" * 55, "cyan"))

    eksik = [k for k in KATEGORILER if k not in hafiza.data["kategoriler"]]
    if eksik:
        print(r(f"[{len(eksik)} kategori öğrenilecek]", "sari"))
        print(r("[5-15 dk sürebilir, sabret...]\n", "sari"))
        for k in KATEGORILER: ogren(k, hafiza)
        print(r("\n[tamam! artık hazır]\n", "yesil"))

    print(r("Komutlar:", "cyan"))
    print("  /liste /stat /bilgi /notlar /temizle /cik")
    print("  /ogret soru => cevap")
    print("  /mod komik|samimi|ciddi|normal")
    print(r("=" * 55, "cyan"))

    while True:
        try: s = input(r("\nSen : ", "mavi")).strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not s: continue
        low = s.lower()

        if low in ("/cik","/exit","exit","quit"): break

        if low == "/liste":
            print(r(f"[{len(hafiza.data['kategoriler'])} kategori]", "cyan"))
            for k in sorted(hafiza.data["kategoriler"]):
                print(f"  • {k} ({len(kategori_liste_al(k))} soru)")
            continue

        if low == "/stat":
            print(r(f"[kategori] {len(hafiza.data['kategoriler'])}", "cyan"))
            print(r(f"[sohbet]  {len(hafiza.data['gecmis'])}", "cyan"))
            print(r(f"[not]     {len(hafiza.data['notlar'])}", "cyan"))
            print(r(f"[bilgi]   {hafiza.data['bilgi']}", "cyan"))
            print(r(f"[mod]     {hafiza.data.get('mod','normal')}", "cyan"))
            continue

        if low == "/bilgi":
            if not hafiza.data["bilgi"]: print("(boş)")
            else:
                for k,v in hafiza.data["bilgi"].items(): print(f"  • {k}: {v}")
            continue

        if low == "/notlar":
            print(notlari_goster(hafiza)); continue

        if low == "/temizle":
            hafiza.data["gecmis"] = []; hafiza.kaydet()
            print(r("[geçmiş silindi]", "sari")); continue

        if low.startswith("/mod "):
            print(mod_ayarla(hafiza, s[5:].strip())); continue

        if low.startswith("/ogret "):
            try:
                q, a = s[7:].split("=>", 1)
                q_low = q.strip().lower()
                if q_low not in hafiza.data["ogrettiklerim"]:
                    hafiza.data["ogrettiklerim"][q_low] = []
                hafiza.data["ogrettiklerim"][q_low].append(a.strip())
                hafiza.kaydet()
                print(r(f"[öğretildi] {q.strip()}", "yesil"))
            except Exception:
                print(r("kullanım: /ogret soru => cevap", "kirmizi"))
            continue

        try:
            cevap = cevapla(s, hafiza)
            print(r(f"Bot : {cevap}", "yesil"))
            hafiza.ekle_gecmis(s, cevap)
        except Exception as e:
            print(r(f"[hata] {e}", "kirmizi"))

if __name__ == "__main__":
    main()
