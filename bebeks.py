# -*- coding: utf-8 -*-
# bebeks.py — Ultra Bebek AI

import os, re, random, pickle, requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ⚙️ AYARLAR
API_KEY = "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"
MODEL   = "openai/gpt-oss-120b"
URL     = "https://api.groq.com/openai/v1/chat/completions"
KLASOR  = "ultra_bilgi"
HAFIZA  = "ultra_hafiza.pkl"
os.makedirs(KLASOR, exist_ok=True)

KATEGORILER = {
    "selamlasma": ["selam","merhaba","hey","naber","napıyorsun","nasılsın",
                   "günaydın","iyi akşamlar","alo","aleyküm","hoş geldin","meraba"],
    "veda":       ["hoşçakal","görüşürüz","bay","güle güle","iyi geceler","kapat","çıkıyorum"],
    "tesekkur":   ["teşekkür","sağol","eyvallah","minnet"],
    "ozur":       ["özür","pardon","kusura","affet"],
    "duygu":      ["üzgün","mutlu","kızgın","sinirli","sevgi","korku",
                   "yalnız","heyecan","moral","stres","kaygı","depres"],
    "gunluk":     ["bugün","yarın","dün","sabah","akşam","gece","öğle",
                   "hava","yemek","uyku","iş","okul","tatil"],
    "yardim":     ["yardım","nasıl","ne yapmalı","tavsiye","öneri","fikir"],
    "matematik":  ["topla","çarp","böl","çıkar","kaç","hesap","yüzde","karekök"],
    "bilgi":      ["nedir","ne demek","kim","nerede","neden","niçin"],
    "kod":        ["python","javascript","kod","program","yazılım","hata","bug","fonksiyon"],
    "oyun":       ["oyun","minecraft","roblox","valorant","csgo","fifa"],
    "yemek":      ["tarif","yemek","pizza","makarna","çorba","tatlı","kahvaltı"],
    "spor":       ["futbol","basketbol","maç","takım","antrenman","koşu"],
    "tarih":      ["tarih","osmanlı","savaş","cumhuriyet","eski","yıl"],
    "saglik":     ["sağlık","hasta","doktor","ilaç","ağrı","baş","mide"],
    "teknoloji":  ["telefon","bilgisayar","internet","yapay zeka","ai","robot"],
    "sanat":      ["müzik","resim","film","dizi","kitap","şiir"],
    "ask":        ["aşk","sevgili","ilişki","kalp","flört","evlilik"],
    "para":       ["para","maaş","kariyer","yatırım","borsa"],
    "egitim":     ["okul","üniversite","sınav","ders","ödev","öğretmen"],
}

# ------------------------------------------------------------
def llm(system, user, temp=0.85, max_tok=8000):
    try:
        r = requests.post(URL,
            headers={"Authorization": f"Bearer {API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": MODEL,
                  "messages":[{"role":"system","content":system},
                              {"role":"user","content":user}],
                  "temperature": temp, "max_tokens": max_tok},
            timeout=180)
        if r.status_code != 200:
            print(f"[hata {r.status_code}] {r.text[:150]}"); return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[bağlantı] {e}"); return None

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
        self.data = {"kategoriler": {}, "bilgi": {}, "gecmis": [], "ogrettiklerim": {}}
        self._yukle()
    def _yukle(self):
        if os.path.exists(HAFIZA):
            try:
                with open(HAFIZA,"rb") as f: self.data = pickle.load(f)
            except Exception: pass
    def kaydet(self):
        with open(HAFIZA,"wb") as f:
            pickle.dump(self.data, f, protocol=pickle.HIGHEST_PROTOCOL)
    def ekle_bilgi(self, k, v): self.data["bilgi"][k] = v; self.kaydet()
    def ekle_gecmis(self, s, c):
        self.data["gecmis"].append((s,c))
        self.data["gecmis"] = self.data["gecmis"][-500:]
        self.kaydet()

# ------------------------------------------------------------
def ogren(kategori, hafiza):
    if kategori in hafiza.data["kategoriler"]: return True
    dosya = os.path.join(KLASOR, f"{kategori}.py")
    if os.path.exists(dosya):
        hafiza.data["kategoriler"][kategori] = True; hafiza.kaydet(); return True
    print(f"[abiye soruyorum: {kategori}] ...")
    sys = f""""{kategori}" kategorisinde 100 farklı kullanıcı mesajı ve her birine 5 farklı cevap üret.
Sadece bu formatta Python listesi yaz:
SONUC = [
    ("mesaj1", ["cevap1a","cevap1b","cevap1c","cevap1d","cevap1e"]),
    ... 100 tane
]
Kurallar:
- Cevaplar kısa, doğal Türkçe, 1-2 cümle
- Her cevap farklı
- Selamlaşma ise varyasyonlar olsun (selam, Selam, SELAM, selamün aleyküm...)
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
        print(f"[öğrendim] {kategori} → {n} soru × 5 = {n*5} cevap")
        return True
    except Exception as e:
        print(f"[hata] {e}"); return False

def kategori_liste_al(kategori):
    dosya = os.path.join(KLASOR, f"{kategori}.py")
    if not os.path.exists(dosya): return []
    ns = {}; exec(open(dosya, encoding="utf-8").read(), ns)
    return ns.get("SONUC", [])

# ------------------------------------------------------------
def en_iyi_cevap(soru, liste):
    if not liste: return "..."
    sorular = [s for s,_ in liste]
    try:
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2,4))
        mat = vec.fit_transform(sorular + [soru])
        sims = cosine_similarity(mat[-1], mat[:-1]).flatten()
        idx = sims.argsort()[::-1]
        adaylar = []
        for i in idx[:5]:
            if sims[i] > 0.15: adaylar.extend(liste[i][1])
        if adaylar: return random.choice(adaylar)
    except Exception: pass
    s_low = soru.lower()
    uyanlar = []
    for s, cevaplar in liste:
        if s_low in s.lower() or s.lower() in s_low: uyanlar.extend(cevaplar)
    if uyanlar: return random.choice(uyanlar)
    return random.choice(random.choice(liste)[1])

def cevapla(soru, hafiza):
    # Kişisel bilgi kaydet
    m = re.match(r"(?:benim\s+)?(adım|ismim|yaşım|şehrim|mesleğim|okulum)\s+(.+)",
                 soru.lower())
    if m:
        hafiza.ekle_bilgi(m.group(1), m.group(2))
        return f"Tamam, {m.group(1)} = {m.group(2)} kaydettim."
    if "adım ne" in soru.lower() or "ismim ne" in soru.lower():
        return f"Adın: {hafiza.data['bilgi'].get('adım','bilmiyorum')}"

    # Öğrettiklerime bak
    soru_low = soru.lower().strip()
    if soru_low in hafiza.data["ogrettiklerim"]:
        return random.choice(hafiza.data["ogrettiklerim"][soru_low])

    # Kategori bul + öğren + cevapla
    kategori = kategori_bul(soru)
    ogren(kategori, hafiza)
    liste = kategori_liste_al(kategori)
    return en_iyi_cevap(soru, liste)

# ------------------------------------------------------------
def main():
    hafiza = Hafiza()
    print("=" * 55)
    print(" BEBEK ULTRA — Akıllı Sohbet")
    print("=" * 55)

    eksik = [k for k in KATEGORILER if k not in hafiza.data["kategoriler"]]
    if eksik:
        print(f"[{len(eksik)} kategori öğrenilecek]")
        print("[5-15 dk sürebilir, sabret...]\n")
        for k in KATEGORILER: ogren(k, hafiza)
        print("\n[tamam! artık hazır]\n")

    print("Komutlar: /liste /stat /bilgi /ogret soru=>cevap /sil /cik")
    print("=" * 55)

    while True:
        try: s = input("\nSen : ").strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not s: continue
        low = s.lower()

        if low in ("/cik","/exit","exit","quit"): break

        if low == "/liste":
            print(f"[{len(hafiza.data['kategoriler'])} kategori]")
            for k in sorted(hafiza.data["kategoriler"]):
                print(f"  • {k} ({len(kategori_liste_al(k))} soru)")
            continue

        if low == "/stat":
            print(f"[kategori] {len(hafiza.data['kategoriler'])}")
            print(f"[sohbet] {len(hafiza.data['gecmis'])}")
            print(f"[bilgi] {hafiza.data['bilgi']}")
            print(f"[öğreti] {len(hafiza.data['ogrettiklerim'])}")
            continue

        if low == "/bilgi":
            if not hafiza.data["bilgi"]: print("(boş)")
            else:
                for k,v in hafiza.data["bilgi"].items(): print(f"  • {k}: {v}")
            continue

        if low == "/sil":
            hafiza.data["gecmis"] = []; hafiza.kaydet()
            print("[sohbet geçmişi silindi]"); continue

        if low.startswith("/ogret "):
            try:
                q, a = s[7:].split("=>", 1)
                q_low = q.strip().lower()
                if q_low not in hafiza.data["ogrettiklerim"]:
                    hafiza.data["ogrettiklerim"][q_low] = []
                hafiza.data["ogrettiklerim"][q_low].append(a.strip())
                hafiza.kaydet()
                print(f"[öğretildi] {q.strip()}")
            except Exception:
                print("kullanım: /ogret soru => cevap")
            continue

        cevap = cevapla(s, hafiza)
        print(f"Bot : {cevap}")
        hafiza.ekle_gecmis(s, cevap)

if __name__ == "__main__":
    main()
