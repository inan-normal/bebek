# -*- coding: utf-8 -*-
# bebek.py — 5 dk aralıksız sohbet, API'yi 1 kere kullanır

import os, re, random, requests

API_KEY = os.getenv("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", "")
MODEL   = "openai/gpt-oss-120b"
URL     = "https://api.groq.com/openai/v1/chat/completions"
KLASOR  = "bildikleri"
os.makedirs(KLASOR, exist_ok=True)

KATEGORILER = {
    "selamlasma": ["selam","merhaba","hey","naber","napıyorsun","nasılsın",
                   "günaydın","iyi akşamlar","alo","aleyküm","hoş geldin"],
    "veda":       ["hoşçakal","görüşürüz","bay","güle güle","iyi geceler","kapat"],
    "tesekkur":   ["teşekkür","sağol","eyvallah","minnet"],
    "ozur":       ["özür","pardon","kusura","affet"],
    "duygu":      ["üzgün","mutlu","kızgın","sinirli","sevgi","korku",
                   "yalnız","heyecan","moral"],
    "gunluk":     ["bugün","yarın","dün","sabah","akşam","gece","öğle",
                   "hava","yemek","uyku","iş","okul"],
    "yardim":     ["yardım","nasıl","ne yapmalı","tavsiye","öneri"],
    "matematik":  ["topla","çarp","böl","çıkar","kaç","hesap","kaç eder"],
    "bilgi":      ["nedir","ne demek","kim","nerede","neden"],
}

def llm(system, user, temp=0.8, max_tok=8000):
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
            print(f"[hata {r.status_code}]"); return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[bağlantı] {e}"); return None

def temizle(t):
    m = re.search(r"\[.*\]", t, re.S)
    return m.group(0) if m else "[]"

def kategori_bul(s):
    s = s.lower()
    for kat, kel in KATEGORILER.items():
        if any(k in s for k in kel): return kat
    return None

def ogren(kategori):
    dosya = os.path.join(KLASOR, f"{kategori}.py")
    if os.path.exists(dosya): return True
    print(f"[abiye soruyorum: {kategori}] (1-2 dk sürebilir)")
    sys = f""""{kategori}" kategorisinde 50 farklı kullanıcı mesajı ve her birine 5 farklı cevap üret.
Sadece bu formatta Python listesi yaz:
SONUC = [
    ("soru1", ["cevap1a","cevap1b","cevap1c","cevap1d","cevap1e"]),
    ... 50 tane
]
Cevaplar kısa (1-2 cümle), doğal Türkçe, hepsi farklı. Matematik ise gerçek hesap."""
    cevap = llm(sys, kategori, max_tok=8000)
    if not cevap: return False
    kod = temizle(cevap)
    with open(dosya, "w", encoding="utf-8") as f:
        f.write(f"# {kategori}\n\nSONUC = {kod}\n")
    try:
        ns = {}; exec(open(dosya, encoding="utf-8").read(), ns)
        n = len(ns.get("SONUC", []))
        print(f"[öğrendim] {kategori} → {n} soru, {n*5} cevap")
        return True
    except Exception as e:
        print(f"[hata] {e}"); return False

def cevapla(soru):
    kategori = kategori_bul(soru) or "bilgi"
    ogren(kategori)
    dosya = os.path.join(KLASOR, f"{kategori}.py")
    if not os.path.exists(dosya): return "..."
    ns = {}; exec(open(dosya, encoding="utf-8").read(), ns)
    liste = ns.get("SONUC", [])
    if not liste: return "..."
    s_low = soru.lower().strip()
    uyanlar = []
    for s, cevaplar in liste:
        if s.lower() == s_low: uyanlar = cevaplar; break
    if not uyanlar:
        for s, cevaplar in liste:
            if s_low in s.lower() or s.lower() in s_low:
                uyanlar.extend(cevaplar)
    if not uyanlar:
        _, cevaplar = random.choice(liste); uyanlar = cevaplar
    return random.choice(uyanlar)

def main():
    if not API_KEY:
        print("[hata] export GROQ_API_KEY=gsk_... yap"); return
    eksik = [k for k in KATEGORILER
             if not os.path.exists(os.path.join(KLASOR, f"{k}.py"))]
    if eksik:
        print("=" * 50)
        print(" İlk açılış — kategoriler öğreniliyor (1 kere)")
        print("=" * 50)
        for k in KATEGORILER: ogren(k)
    print("\n" + "=" * 50)
    print(" BEBEK AI — hazır")
    print(" Komutlar: /liste  /cik")
    print("=" * 50)
    while True:
        try: s = input("\nSen : ").strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not s: continue
        if s.lower() in ("/cik","/exit","exit","quit"): break
        if s.lower() == "/liste":
            print(f"[{len(os.listdir(KLASOR))} kategori]")
            for d in sorted(os.listdir(KLASOR)): print(f"  • {d[:-3]}")
            continue
        print(f"Bot : {cevapla(s)}")

if __name__ == "__main__":
    main()
