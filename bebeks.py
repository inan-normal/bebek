# -*- coding: utf-8 -*-
# bebek.py — Her şeyi öğrenen bebek AI (Termux uyumlu)

import os, re, requests

# ⚠️ BURAYA YENİ ANAHTARINI YAPIŞTIR
API_KEY = "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"
KLASOR  = "bildikleri"
MODEL = "openai/gpt-oss-120b"
URL     = "https://api.groq.com/openai/v1/chat/completions"

os.makedirs(KLASOR, exist_ok=True)

# ------------------------------------------------------------
def llm(system, user, temp=0.7, max_tok=6000):
    try:
        r = requests.post(URL,
            headers={"Authorization": f"Bearer {API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": MODEL,
                  "messages":[{"role":"system","content":system},
                              {"role":"user","content":user}],
                  "temperature": temp,
                  "max_tokens": max_tok},
            timeout=120)
        if r.status_code != 200:
            print(f"[hata {r.status_code}] {r.text[:200]}")
            return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[bağlantı hatası] {e}")
        return None

def temizle(t, tip="dict"):
    if tip == "dict":
        m = re.search(r"\{.*\}", t, re.S)
    else:
        m = re.search(r"\[.*\]", t, re.S)
    return m.group(0) if m else ("{}" if tip=="dict" else "[]")

def dosya_adi(konu):
    return re.sub(r"[^\w]", "_", konu.lower())[:40]

# ------------------------------------------------------------
def ogren(konu):
    """Bir konuyu abiye sorup 100 örnekle öğren."""
    dosya = os.path.join(KLASOR, f"{dosya_adi(konu)}.py")

    if os.path.exists(dosya):
        print(f"[zaten biliyorum: {konu}]")
        return True

    print(f"[abiye soruyorum: {konu}]")

    sys = f"""Sen bir öğretmensin. '{konu}' konusunda kapsamlı bilgi üret.
Sadece şu formatta Python sözlüğü yaz, başka hiçbir şey yazma:

BILGI = {{
    "tanim": "kısa tanım",
    "ornekler": ["örnek 1", "örnek 2", "... 100 tane"],
    "turler": ["tür 1", "tür 2", "... en az 20 tane"],
    "kurallar": ["kural 1", "... en az 20 tane"],
    "ilgili": ["konu1", "konu2", "... en az 10 tane"]
}}

Kurallar:
- Matematik ise gerçek hesaplar yap (2+2=4 gibi)
- Her liste 100'e yakın olsun
- Türkçe yaz
- Sadece Python sözlüğü, açıklama yazma"""

    cevap = llm(sys, konu)
    if not cevap:
        return False

    kod = temizle(cevap, "dict")

    with open(dosya, "w", encoding="utf-8") as f:
        f.write(f"# -*- coding: utf-8 -*-\n")
        f.write(f"# Konu: {konu}\n\n")
        f.write(f"BILGI = {kod}\n")

    # Kaç örnek öğrendi?
    try:
        d = eval(kod)
        n = len(d.get("ornekler", [])) + len(d.get("turler", []))
        print(f"[öğrendim] {konu} → {n} bilgi parçası, dosya: {dosya}")
        return True
    except Exception as e:
        print(f"[kayıt hatası] {e}")
        return False

def hatirla(konu):
    """Öğrenilmiş bilgiyi dosyadan oku."""
    dosya = os.path.join(KLASOR, f"{dosya_adi(konu)}.py")
    if not os.path.exists(dosya):
        return None
    try:
        ns = {}
        exec(open(dosya, encoding="utf-8").read(), ns)
        return ns.get("BILGI")
    except Exception:
        return None

def cevapla(soru):
    """Soruyu öğrenilmiş bilgiyle cevapla, yoksa abiye sor."""
    # Konuyu çıkar
    konu = soru.lower()
    for k in ["nedir", "ne demek", "öğren", "anlat", "kaç", "nasıl"]:
        konu = konu.replace(k, "")
    konu = konu.strip(" ?.!,")[:40]

    # Önce bildiğine bak
    bilgi = hatirla(konu)
    if bilgi:
        print(f"[bildiklerimden cevaplıyorum: {konu}]")
        # Basit arama
        soru_low = soru.lower()
        for ornek in bilgi.get("ornekler", []):
            if soru_low in ornek.lower() or ornek.lower() in soru_low:
                return ornek
        return f"{bilgi.get('tanim','')} | Örnek: {bilgi.get('ornekler',[''])[0]}"

    # Bilmiyorsa öğren
    print(f"[bilmiyorum, öğreniyorum: {konu}]")
    if ogren(konu):
        bilgi = hatirla(konu)
        if bilgi:
            return f"Öğrendim! {bilgi.get('tanim','')}"

    # Hiç olmadı, abiye direkt sor
    cevap = llm("Kısa ve net Türkçe cevap ver.", soru, max_tok=500)
    return cevap or "Cevap bulamadım."

# ------------------------------------------------------------
def main():
    print("=" * 50)
    print(" BEBEK AI — her şeyi öğrenir")
    print(" Komutlar: /liste  /bilgi <konu>  /cik")
    print("=" * 50)

    while True:
        try:
            s = input("\nSen : ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not s: continue

        low = s.lower()

        if low in ("/cik","/exit","exit","quit"):
            break

        if low == "/liste":
            dosyalar = os.listdir(KLASOR)
            print(f"[öğrendiklerim: {len(dosyalar)} konu]")
            for d in sorted(dosyalar):
                print(f"  • {d.replace('.py','')}")
            continue

        if low.startswith("/bilgi "):
            konu = s[7:].strip()
            bilgi = hatirla(konu)
            if bilgi:
                print(f"Tanım: {bilgi.get('tanim','')}")
                print(f"Örnekler ({len(bilgi.get('ornekler',[]))}): {bilgi.get('ornekler',[])[:5]}")
                print(f"Türler ({len(bilgi.get('turler',[]))}): {bilgi.get('turler',[])[:5]}")
            else:
                print(f"[bilmiyorum: {konu}]")
            continue

        # Normal soru
        cevap = cevapla(s)
        print(f"Bot : {cevap}")

if __name__ == "__main__":
    main()
