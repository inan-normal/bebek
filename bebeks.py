# -*- coding: utf-8 -*-
# bebeks.py — Ultra Bebek v3

import os, re, random, pickle, requests, datetime, math, time, ast

# ⚙️ AYARLAR
API_KEY = "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"
MODEL   = "openai/gpt-oss-120b"
URL     = "https://api.groq.com/openai/v1/chat/completions"
KLASOR  = "ultra_bilgi"
HAFIZA  = "ultra_hafiza.pkl"
BILGI   = "ultra_bilgi.pkl"     # öğrenilen cevaplar (pickle)
os.makedirs(KLASOR, exist_ok=True)

C = {"k":"\033[91m","y":"\033[92m","s":"\033[93m",
     "m":"\033[94m","p":"\033[95m","c":"\033[96m","n":"\033[0m"}
def r(t, c="y"): return f"{C[c]}{t}{C['n']}"

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
def llm(system, user, temp=0.8, max_tok=3000, retry=3):
    for attempt in range(retry):
        try:
            rq = requests.post(URL,
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": MODEL,
                      "messages":[{"role":"system","content":system},
                                  {"role":"user","content":user}],
                      "temperature": temp, "max_tokens": max_tok},
                timeout=120)
            if rq.status_code == 429:
                bekle = 5 * (attempt + 1)
                print(r(f"  [limit, {bekle}s bekle]", "s"))
                time.sleep(bekle); continue
            if rq.status_code == 413:
                print(r("  [istek çok büyük, küçültülüyor]", "s"))
                return None
            if rq.status_code != 200:
                print(r(f"  [hata {rq.status_code}]", "k"))
                time.sleep(3); continue
            return rq.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(r(f"  [bağlantı] {e}", "k"))
            time.sleep(3)
    return None

def guvenli_parse(text):
    """AI'nın çıktısını güvenli şekilde Python listesine çevir."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m: return []
    try:
        data = ast.literal_eval(m.group(0))
        # format kontrolü
        if not isinstance(data, list): return []
        temiz = []
        for item in data:
            if (isinstance(item, (tuple, list)) and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], list)
                and all(isinstance(c, str) for c in item[1])):
                temiz.append((item[0], item[1]))
        return temiz
    except Exception:
        return []

def kategori_bul(s):
    s = s.lower()
    for kat, kel in KATEGORILER.items():
        if any(k in s for k in kel): return kat
    return "bilgi"

# ------------------------------------------------------------
class Bilgi:
    """Öğrenilen cevaplar (pickle)."""
    def __init__(self):
        self.data = {}   # {"selamlasma": [(s,[c1,c2,c3]), ...], ...}
        self._yukle()
    def _yukle(self):
        if os.path.exists(BILGI):
            try:
                with open(BILGI,"rb") as f: self.data = pickle.load(f)
            except Exception: pass
    def kaydet(self):
        with open(BILGI,"wb") as f:
            pickle.dump(self.data, f, protocol=pickle.HIGHEST_PROTOCOL)
    def var(self, kat): return kat in self.data and len(self.data[kat]) > 0
    def al(self, kat): return self.data.get(kat, [])
    def ekle(self, kat, liste):
        self.data[kat] = liste; self.kaydet()

class Hafiza:
    def __init__(self):
        self.data = {"bilgi": {}, "gecmis": [], "ogrettiklerim": {},
                     "notlar": [], "mod": "normal"}
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
    def ekle_bilgi(self, k, v): self.data["bilgi"][k] = v; self.kaydet()
    def ekle_gecmis(self, s, c):
        self.data["gecmis"].append((s, c, datetime.datetime.now().isoformat()))
        self.data["gecmis"] = self.data["gecmis"][-1000:]
        self.kaydet()

# ------------------------------------------------------------
def ogren(kategori, bilgi):
    if bilgi.var(kategori): return True
    print(r(f"[abiye soruyorum: {kategori}] ...", "s"))
    sys = f""""{kategori}" kategorisinde 25 farklı kullanıcı mesajı ve her birine 3 farklı cevap üret.
Sadece şu formatta Python listesi yaz, başka hiçbir şey yazma:
[
    ("mesaj1", ["cevap1a","cevap1b","cevap1c"]),
    ("mesaj2", ["cevap2a","cevap2b","cevap2c"]),
    ... 25 tane
]
Kurallar:
- Cevaplar kısa, doğal Türkçe, 1 cümle
- Her cevap farklı
- Selamlaşma ise varyasyon (selam, Selam, SELAM, selamün aleyküm)
- Matematik ise gerçek hesap
- Sadece liste, açıklama yok, kod bloğu yok"""
    cevap = llm(sys, kategori, max_tok=2500)
    if not cevap:
        print(r(f"  [başarısız: {kategori}]", "k")); return False
    liste = guvenli_parse(cevap)
    if not liste:
        print(r(f"  [parse hatası: {kategori}]", "k")); return False
    bilgi.ekle(kategori, liste)
    print(r(f"  [öğrendim] {kategori} → {len(liste)} soru × 3 = {len(liste)*3} cevap", "y"))
    return True

# ------------------------------------------------------------
def benzerlik(a, b):
    ka = set(re.findall(r"\w+", a.lower()))
    kb = set(re.findall(r"\w+", b.lower()))
    if not ka or not kb: return 0
    return len(ka & kb) / len(ka | kb)

def en_iyi_cevap(soru, liste):
    if not liste: return "..."
    s_low = soru.lower().strip()
    for s, cevaplar in liste:
        if s.lower() == s_low: return random.choice(cevaplar)
    uyanlar = []
    for s, cevaplar in liste:
        if s_low in s.lower() or s.lower() in s_low: uyanlar.extend(cevaplar)
    if uyanlar: return random.choice(uyanlar)
    en_iyi, en_skor = None, 0
    for s, cevaplar in liste:
        sk = benzerlik(s_low, s)
        if sk > en_skor: en_skor = sk; en_iyi = cevaplar
    if en_iyi and en_skor > 0.15: return random.choice(en_iyi)
    return random.choice(random.choice(liste)[1])

def matematik_coz(soru):
    try:
        if "karekök" in soru.lower():
            n = float(re.search(r"-?\d+\.?\d*", soru).group())
            return f"√{n} = {math.sqrt(n):.4f}"
        ifade = re.sub(r"[^\d+\-*/(). ]", "", soru)
        if ifade.strip() and any(op in ifade for op in "+-*/"):
            sonuc = eval(ifade, {"__builtins__":{}}, {})
            return f"{ifade.strip()} = {sonuc}"
    except Exception: pass
    return None

def not_ekle(hafiza, notu):
    hafiza.data["notlar"].append({
        "not": notu, "tarih": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
    hafiza.kaydet()
    return f"Not eklendi: {notu}"

def notlari_goster(hafiza):
    if not hafiza.data["notlar"]: return "Not yok."
    out = ["📝 Notlar:"]
    for i, n in enumerate(hafiza.data["notlar"][-20:], 1):
        out.append(f"  {i}. {n['not']}  ({n['tarih']})")
    return "\n".join(out)

def cevapla(soru, hafiza, bilgi):
    s_low = soru.lower().strip()
    mod = hafiza.data.get("mod", "normal")

    # Selam → bazen bebek başlatır
    if s_low in ("merhaba","selam","hey","naber") and random.random() < 0.2:
        return random.choice([
            "Merhaba! Bugün nasılsın?",
            "Selam! Ne yapıyorsun?",
            "Hey! Ne var ne yok?"])

    # Saat/tarih
    if "saat" in s_low and ("kaç" in s_low or "ne" in s_low):
        return "Saat: " + datetime.datetime.now().strftime("%H:%M:%S")
    if "tarih" in s_low or "bugün ayın" in s_low:
        return "Tarih: " + datetime.datetime.now().strftime("%d %B %Y, %A")

    # Not
    if s_low.startswith("not ekle ") or s_low.startswith("not al "):
        return not_ekle(hafiza, soru[9:].strip())
    if s_low in ("notlar","notları göster","notlarım"):
        return notlari_goster(hafiza)

    # Mod
    if s_low.startswith("mod "):
        hafiza.data["mod"] = soru[4:].strip(); hafiza.kaydet()
        return f"Mod: {hafiza.data['mod']}"

    # Kişisel bilgi
    m = re.match(r"(?:benim\s+)?(adım|ismim|yaşım|şehrim|mesleğim|okulum|doğum günüm)\s+(.+)", s_low)
    if m:
        hafiza.ekle_bilgi(m.group(1), m.group(2).strip())
        return f"Tamam, {m.group(1)} = {m.group(2).strip()} kaydettim."
    if "adım ne" in s_low or "ismim ne" in s_low:
        return f"Adın: {hafiza.data['bilgi'].get('adım','bilmiyorum')}"
    if "yaşım kaç" in s_low:
        return f"Yaşın: {hafiza.data['bilgi'].get('yaşım','bilmiyorum')}"

    # Matematik direkt
    if any(k in s_low for k in ["kaç eder","kaç yapar","hesapla","karekök"]) or \
       re.search(r"\d+\s*[+\-*/]\s*\d+", s_low):
        mat = matematik_coz(soru)
        if mat: return mat

    # Öğrettiklerim
    if s_low in hafiza.data["ogrettiklerim"]:
        return random.choice(hafiza.data["ogrettiklerim"][s_low])

    # Kategori → öğren → cevap
    kategori = kategori_bul(soru)
    if not bilgi.var(kategori):
        ogren(kategori, bilgi)
        time.sleep(4)  # API'yi yormamak için
    liste = bilgi.al(kategori)
    cevap = en_iyi_cevap(soru, liste)

    if mod == "komik" and random.random() < 0.2: cevap += " 😄"
    elif mod == "samimi" and random.random() < 0.2: cevap += " kanka"
    return cevap

# ------------------------------------------------------------
def main():
    hafiza = Hafiza()
    bilgi  = Bilgi()

    print(r("=" * 55, "c"))
    print(r(" 🤖 BEBEK ULTRA v3", "c"))
    print(r("=" * 55, "c"))

    eksik = [k for k in KATEGORILER if not bilgi.var(k)]
    if eksik:
        print(r(f"[{len(eksik)} kategori öğrenilecek, ~{len(eksik)*6//60+1} dk]", "s"))
        print(r("[sabret, tek seferlik]\n", "s"))
        for i, k in enumerate(KATEGORILER, 1):
            print(r(f"\n[{i}/{len(KATEGORILER)}]", "p"))
            if bilgi.var(k):
                print(r(f"  [zaten var: {k}]", "y"))
                continue
            ok = ogren(k, bilgi)
            if ok: time.sleep(4)  # rate limit için

    print(r("\n[tamam! artık hazır]\n", "y"))
    print(r("Komutlar:", "c"))
    print("  /liste /stat /bilgi /notlar /temizle")
    print("  /ogret soru => cevap")
    print("  /mod komik|samimi|ciddi|normal")
    print("  /ogren <kategori>  (yeni kategori)")
    print("  /cik")
    print(r("=" * 55, "c"))

    while True:
        try: s = input(r("\nSen : ", "m")).strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not s: continue
        low = s.lower()

        if low in ("/cik","/exit","exit","quit"): break

        if low == "/liste":
            print(r(f"[{len(bilgi.data)} kategori]", "c"))
            for k, v in sorted(bilgi.data.items()):
                print(f"  • {k} ({len(v)} soru)")
            continue

        if low == "/stat":
            print(r(f"[kategori] {len(bilgi.data)}", "c"))
            print(r(f"[sohbet]  {len(hafiza.data['gecmis'])}", "c"))
            print(r(f"[not]     {len(hafiza.data['notlar'])}", "c"))
            print(r(f"[bilgi]   {hafiza.data['bilgi']}", "c"))
            print(r(f"[mod]     {hafiza.data.get('mod','normal')}", "c"))
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
            print(r("[geçmiş silindi]", "s")); continue

        if low.startswith("/mod "):
            hafiza.data["mod"] = s[5:].strip(); hafiza.kaydet()
            print(r(f"[mod: {hafiza.data['mod']}]", "y")); continue

        if low.startswith("/ogren "):
            kat = s[7:].strip().lower()
            if ogren(kat, bilgi):
                print(r(f"[{kat} öğrenildi]", "y"))
            continue

        if low.startswith("/ogret "):
            try:
                q, a = s[7:].split("=>", 1)
                q_low = q.strip().lower()
                if q_low not in hafiza.data["ogrettiklerim"]:
                    hafiza.data["ogrettiklerim"][q_low] = []
                hafiza.data["ogrettiklerim"][q_low].append(a.strip())
                hafiza.kaydet()
                print(r(f"[öğretildi] {q.strip()}", "y"))
            except Exception:
                print(r("kullanım: /ogret soru => cevap", "k"))
            continue

        try:
            cevap = cevapla(s, hafiza, bilgi)
            print(r(f"Bot : {cevap}", "y"))
            hafiza.ekle_gecmis(s, cevap)
        except Exception as e:
            print(r(f"[hata] {e}", "k"))

if __name__ == "__main__":
    main()
