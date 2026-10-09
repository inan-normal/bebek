#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BEBEK DEV v1.1 — Kod Yazan Uzman AI (DeepSeek tarzı).

Akış: Açılış → Token → "Ne kodlayalım?" → doğal istek → uzman kod.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

SURUM = "1.1"
TOKEN_SECRET = b"bebek-dev-v1-rotate-this"
TOKEN_PENCERE = 60


class C:
    R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
    RD = "\033[91m"; GR = "\033[92m"; YL = "\033[93m"
    BL = "\033[94m"; MG = "\033[95m"; CY = "\033[96m"; WH = "\033[97m"
    CLR = "\033[2J\033[H"


# ═══════════════════════════════════════════════════════════════
#  TOKEN
# ═══════════════════════════════════════════════════════════════
def zaman_token(t: Optional[float] = None) -> str:
    p = int((t if t is not None else time.time()) // TOKEN_PENCERE)
    h = hmac.new(TOKEN_SECRET, str(p).encode(), hashlib.sha256).hexdigest()
    return f"{int(h[:8], 16) % 1_000_000:06d}"


def token_gecerli(g: str) -> bool:
    t = re.sub(r"\D", "", g)
    return len(t) == 6 and t in (zaman_token(), zaman_token(time.time() - TOKEN_PENCERE))


# ═══════════════════════════════════════════════════════════════
#  KONFİG
# ═══════════════════════════════════════════════════════════════
@dataclass
class Config:
    db: str = "bebek_dev.db"
    model: str = "openai/gpt-oss-120b"
    api_url: str = "https://api.groq.com/openai/v1/chat/completions"
    api_key: str = ""
    timeout: int = 180
    retry: int = 3
    max_tok: int = 6000
    dil: str = "python"


def _key_yukle() -> str:
    k = os.environ.get("GROQ_API_KEY", "").strip()
    if k:
        return k
    for f in (".groq_key", "groq_key.txt"):
        if os.path.exists(f):
            try:
                v = open(f, encoding="utf-8").read().strip()
                if v:
                    return v
            except OSError:
                pass
    return ""


# ═══════════════════════════════════════════════════════════════
#  DEPO
# ═══════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS uretilen(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    istek TEXT NOT NULL, dil TEXT NOT NULL, kod TEXT NOT NULL,
    dosya TEXT, tarih TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ayar(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def ekle(self, istek: str, dil: str, kod: str, dosya: Optional[str] = None) -> int:
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO uretilen(istek,dil,kod,dosya,tarih) VALUES(?,?,?,?,?)",
                (istek, dil, kod, dosya,
                 dt.datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid or 0

    def son(self, n: int = 10) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id,istek,dil,tarih,LENGTH(kod) u FROM uretilen "
            "ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def sayi(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM uretilen").fetchone()[0]

    def ayar_set(self, k: str, v: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO ayar(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def ayar_get(self, k: str, d: str = "") -> str:
        r = self.conn.execute("SELECT v FROM ayar WHERE k=?", (k,)).fetchone()
        return r["v"] if r else d

    def kapat(self) -> None:
        try:
            self.conn.commit(); self.conn.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  DEEPSEEK DÜŞÜNCE ZİNCİRİ (uzman kod)
# ═══════════════════════════════════════════════════════════════
SISTEM = """Sen BEBEK DEV adlı, KIDEMLİ (senior) bir YAZILIM MİMARISIN.
DeepSeek-Coder / GPT-5 seviyesinde kod üretirsin.
Sohbet etmezsin, sadece profesyonel kod yazarsın.

━━━ DÜŞÜNCE ZİNCİRİN (içinden uygula, kullanıcıya yazma) ━━━
1. ANLA    → İstek ne? Girdi/çıktı/edge case? Platform? Kısıtlar?
2. MİMARİ  → Kaç dosya? Hangi modüller? Hangi sınıflar/fonksiyonlar?
             Hangi kütüphaneler? Veri modeli ne?
3. PLANLA  → Klasör yapısı, dosya isimleri, sorumluluklar ayrılsın (SRP).
4. YAZ     → TEMİZ, ÇALIŞIR, EKSİKSİZ kod. Kısaltma yok, "..." yok, TODO yok.
5. KONTROL → Import tam mı? Değişken çakışması? None/boş/sıfıra-bölme/
             unicode/dosya-yok/network hatası? Type hint var mı?
             Fonksiyon imzaları tutarlı mı? Docstring var mı?
6. SUN     → Önce MİMARİ ÖZETİ (dosya ağacı), sonra her dosyanın TAM kodu,
             sonra kurulum/çalıştırma talimatı.

━━━ KALİTE KURALLARI ━━━
• Her fonksiyon tek iş yapsın (SRP). Uzun fonksiyonları böl.
• Anlamlı isimler: `kullanici_getir` değil `get_user`, İngilizce.
• Docstring koy (her public fonksiyon/sınıf).
• Type hint koy (parametreler + return).
• Hata yönetimi: try/except, özel exception sınıfları.
• Log kullan (print değil, logging).
• Config'i sabit kodlama, env/dataclass ile ver.
• Test edilebilir yaz (saf fonksiyonlar, dependency injection).
• Modern syntax kullan (f-string, pathlib, dataclass, asyncio gerekirse).

━━━ ÇIKTI FORMATI (SADECE BU JSON) ━━━
{
  "dil": "python",
  "aciklama": "kısa özet + kurulum talimatı",
  "dosyalar": [
    {"yol": "main.py", "icerik": "<tam kod>"},
    {"yol": "utils.py", "icerik": "<tam kod>"}
  ]
}

Kurallar:
- `dosyalar` BOŞ olmasın. Tek dosyaysa tek eleman koy.
- Her `icerik` TAM ve ÇALIŞIR olsun. Markdown fence koyma.
- Türkçe açıklama yaz ama kod İngilizce.
- Uydurma API yok, sadece gerçek kütüphaneler.
"""


# ═══════════════════════════════════════════════════════════════
#  LLM
# ═══════════════════════════════════════════════════════════════
@dataclass
class Yanit:
    dosyalar: List[Dict[str, str]] = None
    dil: str = "python"
    aciklama: str = ""
    hata: Optional[str] = None

    def __post_init__(self):
        if self.dosyalar is None:
            self.dosyalar = []


_session = requests.Session()
_session.headers.update({"User-Agent": f"bebek-dev/{SURUM}"})


def _json_ayikla(t: str) -> Optional[dict]:
    if not t:
        return None
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    i = t.find("{")
    if i >= 0:
        d, ins, esc = 0, False, False
        for k in range(i, len(t)):
            c = t[k]
            if ins:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': ins = False
                continue
            if c == '"': ins = True
            elif c == "{": d += 1
            elif c == "}":
                d -= 1
                if d == 0:
                    try:
                        return json.loads(t[i:k + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def llm(istek: str, cfg: Config, gecmis: List[Tuple[str, str]]) -> Yanit:
    if not cfg.api_key:
        return Yanit(hata="api_key_yok")

    msj: List[Dict[str, str]] = [{"role": "system", "content": SISTEM}]
    for s, c in gecmis[-3:]:
        msj.append({"role": "user", "content": s})
        msj.append({"role": "assistant", "content": c})
    msj.append({"role": "user", "content": f"Hedef dil: {cfg.dil}\nİstek: {istek}"})

    son: Optional[str] = None
    for d in range(cfg.retry):
        try:
            r = _session.post(
                cfg.api_url,
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json={"model": cfg.model, "messages": msj,
                      "temperature": 0.2, "max_tokens": cfg.max_tok,
                      "response_format": {"type": "json_object"}},
                timeout=cfg.timeout)
        except requests.RequestException as e:
            son = f"ağ: {e}"
            time.sleep(min(2 ** d, 8))
            continue
        if r.status_code == 401:
            return Yanit(hata="auth")
        if r.status_code == 429:
            time.sleep(min(5 * (d + 1), 30))
            continue
        if r.status_code != 200:
            son = f"http {r.status_code}: {r.text[:150]}"
            time.sleep(1.5)
            continue
        try:
            ham = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            son = f"parse: {e}"
            continue
        v = _json_ayikla(ham)
        if not v:
            return Yanit(hata="json_parse")
        dosyalar = v.get("dosyalar") or []
        if not isinstance(dosyalar, list):
            dosyalar = []
        # Eski format desteği: {"kod": "..."}
        if not dosyalar and v.get("kod"):
            dosyalar = [{"yol": "main.py", "icerik": v["kod"]}]
        temiz: List[Dict[str, str]] = []
        for d_ in dosyalar:
            if not isinstance(d_, dict):
                continue
            yol = str(d_.get("yol") or "main.py")
            ic = str(d_.get("icerik") or "")
            ic = re.sub(r"^```[\w+-]*\n?", "", ic)
            ic = re.sub(r"\n?```\s*$", "", ic)
            if ic.strip():
                temiz.append({"yol": yol, "icerik": ic})
        return Yanit(
            dosyalar=temiz,
            dil=(v.get("dil") or cfg.dil).strip(),
            aciklama=(v.get("aciklama") or "").strip())
    return Yanit(hata=son or "bilinmeyen")


# ═══════════════════════════════════════════════════════════════
#  DOSYA
# ═══════════════════════════════════════════════════════════════
def dosya_yaz(yol: str, icerik: str) -> Tuple[bool, str]:
    p = Path(yol).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(icerik, encoding="utf-8")
        return True, str(p.resolve())
    except Exception as e:
        return False, str(e)


def python_calistir(yol: str, timeout: int = 60) -> Tuple[int, str, str]:
    p = Path(yol).expanduser()
    if not p.exists():
        return -1, "", f"dosya yok: {yol}"
    try:
        r = subprocess.run([sys.executable, str(p)],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"zaman aşımı ({timeout}s)"
    except Exception as e:
        return -1, "", str(e)


# ═══════════════════════════════════════════════════════════════
#  SİNYAL
# ═══════════════════════════════════════════════════════════════
_IZIN = False


def _sinyal(sig, frame):
    if _IZIN:
        print(f"\n{C.YL}Kapatılıyor...{C.R}"); sys.exit(0)
    print(f"\n{C.RD}⚠ Ctrl+C engellendi. Çıkmak için: /kapat{C.R}")


# ═══════════════════════════════════════════════════════════════
#  AÇILIŞ
# ═══════════════════════════════════════════════════════════════
BANNER = f"""{C.CY}{C.B}
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║    ██████╗ ███████╗██████╗ ███████╗██╗  ██╗    ██████╗ ███████╗██╗   ██╗     ║
║    ██╔══██╗██╔════╝██╔══██╗██╔════╝██║ ██╔╝    ██╔══██╗██╔════╝██║   ██║     ║
║    ██████╔╝█████╗  ██████╔╝█████╗  █████╔╝     ██║  ██║█████╗  ██║   ██║     ║
║    ██╔══██╗██╔══╝  ██╔══██╗██╔══╝  ██╔═██╗     ██║  ██║██╔══╝  ╚██╗ ██╔╝     ║
║    ██████╔╝███████╗██████╔╝███████╗██║  ██╗    ██████╔╝███████╗ ╚████╔╝      ║
║    ╚═════╝ ╚══════╝╚═════╝ ╚══════╝╚═╝  ╚═╝    ╚═════╝ ╚══════╝  ╚═══╝       ║
║                                                                              ║
║              K O D   Y A Z A N   U Z M A N   A I                              ║
║                       v{SURUM}  •  Senior Mode                                   ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝{C.R}
"""


def _bios(etiket: str, deger: str, g: float = 0.06) -> None:
    sys.stdout.write(f"  {C.D}[ ]{C.R} {etiket:<22} ")
    sys.stdout.flush(); time.sleep(g)
    sys.stdout.write(f"{C.GR}OK{C.R}  {C.D}{deger}{C.R}\n")
    sys.stdout.flush()


def acilis(cfg: Config, store: Store) -> None:
    os.system("cls" if os.name == "nt" else "clear")
    print(BANNER)
    time.sleep(0.2)
    print(f"{C.YL}  BIOS başlatılıyor...{C.R}\n")
    _bios("Bellek", "kod üretim modu")
    _bios("Veritabanı", cfg.db)
    _bios("Üretilen kod", f"{store.sayi()} kayıt")
    _bios("LLM", "hazır" if cfg.api_key else "KAPALI")
    _bios("Hedef dil", cfg.dil)
    _bios("Mod", "Senior / DeepSeek")
    time.sleep(0.15)
    print(f"\n  {C.GR}Sistem hazır.{C.R}\n")


def token_girisi() -> bool:
    print(f"{C.MG}{'─' * 78}{C.R}")
    print(f"{C.B}  🔐  GÜVENLİK DOĞRULAMASI{C.R}")
    print(f"{C.MG}{'─' * 78}{C.R}")
    for d in range(3):
        kod = zaman_token()
        kalan = TOKEN_PENCERE - int(time.time()) % TOKEN_PENCERE
        print(f"  {C.CY}Zaman token'ın:{C.R} {C.B}{C.YL}"
              f"{kod[:3]} {kod[3:]}{C.R}  {C.D}({kalan} sn){C.R}")
        try:
            g = input(f"  {C.B}Token gir (6 hane): {C.R}").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  [iptal]"); return False
        if token_gecerli(g):
            print(f"  {C.GR}✓ Erişim onaylandı.{C.R}\n")
            return True
        print(f"  {C.RD}✗ Hatalı token. {2 - d} hak kaldı.{C.R}")
    return False


def karsilama() -> None:
    print(f"{C.MG}{'═' * 78}{C.R}")
    print(f"{C.B}{C.CY}  🤖  Merhaba! Ben BEBEK DEV — kod yazan uzman asistan.{C.R}")
    print(f"{C.MG}{'═' * 78}{C.R}\n")
    print(f"{C.WH}  {C.B}Kodunu yaz.{C.R} Ne yapmak istiyorsun?\n")
    print(f"{C.D}  Örnekler:{C.R}")
    print(f"    {C.CY}•{C.R} bana bir site yap")
    print(f"    {C.CY}•{C.R} bana bir oyun yap (yılan oyunu)")
    print(f"    {C.CY}•{C.R} bana bir hesap makinesi yap")
    print(f"    {C.CY}•{C.R} bana bir todo listesi uygulaması yap")
    print(f"    {C.CY}•{C.R} bana discord botu yap")
    print(f"    {C.CY}•{C.R} bana flask ile blog yap\n")
    print(f"{C.D}  Komutlar için: {C.CY}/yardim{C.R}")
    print(f"{C.D}  Çıkış: {C.RD}/kapat{C.R}\n")


# ═══════════════════════════════════════════════════════════════
#  UZMAN
# ═══════════════════════════════════════════════════════════════
class Uzman:
    def __init__(self, store: Store, cfg: Config):
        self.store, self.cfg = store, cfg
        self.gecmis: List[Tuple[str, str]] = []
        self.son_dosyalar: List[Dict[str, str]] = []

    def uret(self, istek: str) -> None:
        print(f"\n{C.D}▸ Düşünüyorum...{C.R}", flush=True)
        y = llm(istek, self.cfg, self.gecmis)

        if y.hata == "auth":
            print(f"{C.RD}✗ API anahtarı geçersiz.{C.R}")
            print(f"{C.D}  → console.groq.com/keys → yeni anahtar → "
                  f"GROQ_API_KEY'e ekle{C.R}\n")
            return
        if y.hata == "api_key_yok":
            print(f"{C.RD}✗ GROQ_API_KEY yok.{C.R}")
            print(f"{C.D}  → export GROQ_API_KEY=\"gsk_...\"{C.R}\n")
            return
        if y.hata:
            print(f"{C.RD}✗ LLM hatası: {y.hata}{C.R}\n")
            return
        if not y.dosyalar:
            print(f"{C.RD}✗ LLM kod döndürmedi.{C.R}\n")
            return

        self.son_dosyalar = y.dosyalar

        # Başlık
        print(f"\n{C.MG}{'═' * 78}{C.R}")
        print(f"{C.B}{C.CY}  📦  ÜRETİLEN PROJE{C.R}")
        print(f"{C.MG}{'═' * 78}{C.R}")
        print(f"  {C.D}Dil:{C.R} {y.dil}   "
              f"{C.D}Dosya:{C.R} {len(y.dosyalar)}   "
              f"{C.D}İstek:{C.R} {istek[:50]}")
        print(f"{C.MG}{'═' * 78}{C.R}\n")

        # Her dosya
        for i, d in enumerate(y.dosyalar, 1):
            yol = d["yol"]
            ic = d["icerik"]
            satir = ic.count("\n") + 1
            print(f"{C.B}{C.CY}[{i}/{len(y.dosyalar)}] "
                  f"📄 {yol}{C.R}  {C.D}({len(ic)}b, {satir} satır){C.R}")
            print(f"{C.MG}{'─' * 78}{C.R}")
            print(f"{C.WH}{ic}{C.R}")
            print(f"{C.MG}{'─' * 78}{C.R}\n")

        # Açıklama
        if y.aciklama:
            print(f"{C.CY}📝 {y.aciklama}{C.R}\n")

        # Kaydetme ipucu
        print(f"{C.D}💾 Kaydetmek için: /kaydet <klasör>  "
              f"(tüm dosyalar oraya yazılır){C.R}")
        print(f"{C.D}▶️  Python ana dosyayı çalıştır: /calistir <yol>{C.R}\n")

        # Kayıt
        tum = "\n\n".join(
            f"# === {d['yol']} ===\n{d['icerik']}" for d in y.dosyalar)
        self.store.ekle(istek, y.dil, tum)
        self.gecmis.append((istek, y.aciklama or "kod üretildi"))
        self.gecmis = self.gecmis[-6:]

    def kaydet(self, klasor: str) -> None:
        if not self.son_dosyalar:
            print(f"{C.RD}✗ Önce kod üret.{C.R}")
            return
        kok = Path(klasor).expanduser()
        yazilan: List[str] = []
        hatalar: List[str] = []
        for d in self.son_dosyalar:
            hedef = kok / d["yol"]
            ok, m = dosya_yaz(str(hedef), d["icerik"])
            if ok:
                yazilan.append(m)
            else:
                hatalar.append(f"{d['yol']}: {m}")
        if yazilan:
            print(f"{C.GR}✓ {len(yazilan)} dosya yazıldı:{C.R}")
            for y in yazilan:
                print(f"   {y}")
        for h in hatalar:
            print(f"{C.RD}✗ {h}{C.R}")
        print()

    def calistir(self, yol: str) -> None:
        if not Path(yol).suffix == ".py":
            print(f"{C.YL}⚠ Sadece .py çalıştırılır.{C.R}")
            return
        print(f"{C.D}[{yol} çalışıyor...]{C.R}\n")
        kod, out, err = python_calistir(yol)
        if kod == 0:
            print(f"{C.GR}✓ Çıkış kodu: 0{C.R}")
        else:
            print(f"{C.RD}✗ Çıkış kodu: {kod}{C.R}")
        if out:
            print(f"{C.WH}{out}{C.R}")
        if err:
            print(f"{C.RD}{err}{C.R}")


# ═══════════════════════════════════════════════════════════════
#  KOMUTLAR
# ═══════════════════════════════════════════════════════════════
YARDIM = f"""{C.B}{C.CY}KOMUTLAR{C.R}
  {C.CY}/yardim{C.R}              Bu yardım
  {C.CY}/kaydet <klasör>{C.R}     Son üretilen dosyaları kaydet
  {C.CY}/calistir <yol>{C.R}      Python dosyasını çalıştır
  {C.CY}/dil <dil>{C.R}           Hedef dil (python/js/ts/go/rust/cpp/java)
  {C.CY}/gecmis{C.R}              Son üretimler
  {C.CY}/istatistik{C.R}          Genel istatistik
  {C.RD}/kapat{C.R}               Çıkış

{C.D}Doğal yaz, komut olmadan da çalışır:{C.R}
  bana bir site yap
  bana yılan oyunu yap
  bana hesap makinesi yap
"""


def komut(s: str, uz: Uzman, store: Store, cfg: Config) -> Optional[bool]:
    low = s.strip().lower()

    if low in ("/kapat", "/cik", "/exit", "exit", "quit"):
        return False
    if low in ("/yardim", "/help", "?", "help"):
        print(YARDIM); return True
    if low == "/gecmis":
        rows = store.son(10)
        if not rows:
            print("(üretim yok)")
        else:
            for r in rows:
                print(f"  {C.CY}#{r['id']}{C.R} [{r['dil']}] "
                      f"{r['istek'][:60]}  {C.D}{r['tarih']} ({r['u']}b){C.R}")
        return True
    if low == "/istatistik":
        print(f"  Üretim:   {store.sayi()}")
        print(f"  Dil:      {cfg.dil}")
        print(f"  Model:    {cfg.model}")
        print(f"  API:      {'hazır' if cfg.api_key else 'YOK'}")
        return True
    if low.startswith("/dil "):
        yeni = s[5:].strip().lower()
        if yeni:
            cfg.dil = yeni
            store.ayar_set("dil", yeni)
            print(f"{C.GR}✓ Dil: {yeni}{C.R}")
        return True
    if low.startswith("/kaydet"):
        parca = s[7:].strip()
        if not parca:
            parca = input(f"  {C.CY}Hedef klasör: {C.R}").strip() or "./cikti"
        uz.kaydet(parca); return True
    if low.startswith("/calistir "):
        uz.calistir(s[10:].strip()); return True

    return None  # komut değil


# ═══════════════════════════════════════════════════════════════
#  ANA
# ═══════════════════════════════════════════════════════════════
def main() -> int:
    global _IZIN

    signal.signal(signal.SIGINT, _sinyal)
    try:
        signal.signal(signal.SIGTERM, _sinyal)
    except (AttributeError, ValueError):
        pass

    cfg = Config()
    cfg.api_key = _key_yukle()
    store = Store(cfg.db)
    cfg.dil = store.ayar_get("dil", "python")

    acilis(cfg, store)

    if not token_girisi():
        print(f"{C.RD}✗ Erişim reddedildi.{C.R}")
        store.kapat(); return 1

    karsilama()
    uz = Uzman(store, cfg)

    while True:
        try:
            s = input(f"{C.B}{C.GR}dev>{C.R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YL}Çıkmak için /kapat yaz.{C.R}")
            continue
        if not s:
            continue

        # Komut mu?
        try:
            r = komut(s, uz, store, cfg)
        except Exception as e:
            print(f"{C.RD}✗ Komut hatası: {e}{C.R}")
            continue
        if r is False:
            break
        if r is True:
            continue

        # Komut değilse → doğal dilde kod isteği
        try:
            uz.uret(s)
        except Exception as e:
            print(f"{C.RD}✗ Üretim hatası: {e}{C.R}")

    _IZIN = True
    print(f"\n{C.CY}Bebek Dev kapatılıyor...{C.R}")
    store.kapat()
    print(f"{C.GR}✓ Görüşürüz.{C.R}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
