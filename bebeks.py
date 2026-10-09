#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BEBEK DEV v5.1 — Termux Uyumlu C# Uzmanı

FULL mod  : dotnet varsa → gerçek derleme + test döngüsü
LITE mod  : dotnet yoksa → LLM + dahili C# denetleyici + syntax check

Mimari:
  İstek → Şablon tespit → İskelet → Plan → Kod → [BUILD LOOP] →
  Statik denetim → LLM denetim → [TEST LOOP] → Rapor
"""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("HATA: 'requests' yok. Kur: pip install requests")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════
#  SABİTLER
# ═══════════════════════════════════════════════════════════════════════════
SURUM = "5.1"
OLLAMA_URL = "http://localhost:11434"
EV_DIZINI = Path.home() / ".bebek_dev"
LOG_DOSYASI = EV_DIZINI / "bebek.log"


# ═══════════════════════════════════════════════════════════════════════════
#  RENK
# ═══════════════════════════════════════════════════════════════════════════
class C:
    R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
    RD = "\033[91m"; GR = "\033[92m"; YL = "\033[93m"
    BL = "\033[94m"; MG = "\033[95m"; CY = "\033[96m"; WH = "\033[97m"


# ═══════════════════════════════════════════════════════════════════════════
#  LOG
# ═══════════════════════════════════════════════════════════════════════════
def _log(msg: str) -> None:
    try:
        EV_DIZINI.mkdir(exist_ok=True)
        with open(LOG_DOSYASI, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  KONFİG
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Config:
    db: str = str(EV_DIZINI / "bebek.db")
    ollama_url: str = OLLAMA_URL
    model: str = "qwen2.5-coder:7b"
    timeout: int = 600
    max_tok: int = 4096
    num_ctx: int = 16384
    sicaklik: float = 0.25
    max_build_tur: int = 5
    max_test_tur: int = 3
    otomatik_test: bool = True
    ogrenme: bool = True
    streaming: bool = True
    min_skor: int = 75
    lite_mod: bool = False  # dotnet yoksa True


# ═══════════════════════════════════════════════════════════════════════════
#  ARAMA / KONTROL
# ═══════════════════════════════════════════════════════════════════════════
def dotnet_var() -> bool:
    return shutil.which("dotnet") is not None


def dotnet_surum() -> Optional[str]:
    try:
        r = subprocess.run(["dotnet", "--version"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def node_var() -> bool:
    return shutil.which("node") is not None


# ═══════════════════════════════════════════════════════════════════════════
#  OLLAMA
# ═══════════════════════════════════════════════════════════════════════════
_http = requests.Session()


def ollama_hazir(cfg: Config) -> Tuple[bool, str]:
    try:
        r = _http.get(f"{cfg.ollama_url}/api/tags", timeout=5)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        modeller = [m.get("name", "") for m in r.json().get("models", [])]
        if not modeller:
            return False, "hiç model yok (ollama pull qwen2.5-coder:7b)"
        if cfg.model in modeller:
            return True, cfg.model
        for m in modeller:
            if m.split(":")[0] == cfg.model.split(":")[0]:
                return True, m
        return False, f"model yok: {cfg.model} | mevcut: {', '.join(modeller[:3])}"
    except requests.RequestException as e:
        return False, str(e)


def _json_ayikla(t: str) -> Optional[dict]:
    """LLM çıktısından JSON çıkar. Markdown fence + brace matching."""
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


def ollama_stream(sistem: str, kullanici: str, cfg: Config,
                  sicaklik: Optional[float] = None,
                  json_mod: bool = True,
                  max_tok: Optional[int] = None,
                  canli: bool = True,
                  baslik: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Streaming Ollama çağrısı."""
    govde = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": sistem},
            {"role": "user", "content": kullanici},
        ],
        "stream": True,
        "options": {
            "temperature": sicaklik if sicaklik is not None else cfg.sicaklik,
            "num_predict": max_tok or cfg.max_tok,
            "num_ctx": cfg.num_ctx,
        },
    }
    if json_mod:
        govde["format"] = "json"

    if canli and baslik:
        print(f"  {C.D}▸ {baslik}{C.R}")

    try:
        r = _http.post(f"{cfg.ollama_url}/api/chat", json=govde,
                       timeout=cfg.timeout, stream=True)
    except requests.RequestException as e:
        return None, f"bağlantı: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"

    parcalar: List[str] = []
    son_guncelleme = time.time()
    try:
        for satir in r.iter_lines(decode_unicode=True):
            if not satir:
                continue
            try:
                j = json.loads(satir)
            except json.JSONDecodeError:
                continue
            parca = (j.get("message") or {}).get("content", "")
            if parca:
                parcalar.append(parca)
                if canli:
                    now = time.time()
                    if now - son_guncelleme > 0.05:
                        sys.stdout.write(f"{C.D}{parca}{C.R}")
                        sys.stdout.flush()
                        son_guncelleme = now
            if j.get("done"):
                break
    except requests.RequestException as e:
        return None, f"stream: {e}"

    if canli:
        sys.stdout.write("\n")
        sys.stdout.flush()

    tam = "".join(parcalar)
    if not tam.strip():
        return None, "boş yanıt"
    return tam, None


def ollama_cagri(sistem: str, kullanici: str, cfg: Config,
                 sicaklik: Optional[float] = None,
                 max_tok: Optional[int] = None,
                 canli: bool = True,
                 baslik: str = "",
                 deneme: int = 2) -> Tuple[Optional[dict], Optional[str]]:
    son_hata = None
    for i in range(deneme):
        ham, hata = ollama_stream(
            sistem, kullanici, cfg,
            sicaklik=sicaklik, json_mod=True, max_tok=max_tok,
            canli=canli and i == 0,
            baslik=baslik if i == 0 else f"{baslik} (retry {i})")
        if hata:
            son_hata = hata
            if i < deneme - 1:
                time.sleep(1.0)
            continue
        v = _json_ayikla(ham or "")
        if v:
            return v, None
        son_hata = "JSON parse hatası"
    return None, son_hata or "bilinmeyen"


# ═══════════════════════════════════════════════════════════════════════════
#  MSBUILD PARSER
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class BuildMesaj:
    dosya: str
    satir: int
    kolon: int
    seviye: str
    kod: str
    mesaj: str

    def kisa(self) -> str:
        return f"{self.dosya}({self.satir},{self.kolon}): {self.seviye} {self.kod}: {self.mesaj}"


@dataclass
class BuildSonuc:
    basarili: bool
    cikis: int
    stdout: str
    stderr: str
    hatalar: List[BuildMesaj] = field(default_factory=list)
    uyarilar: List[BuildMesaj] = field(default_factory=list)
    sure: float = 0.0
    tur: int = 0


_MSBUILD_RE = re.compile(
    r"^(?P<dosya>[^\s(]+?)\((?P<satir>\d+),(?P<kolon>\d+)\):\s+"
    r"(?P<seviye>error|warning)\s+(?P<kod>[A-Z]+\d+):\s+(?P<mesaj>.+?)(?:\s+\[.+?\])?$",
    re.MULTILINE)


def msbuild_parse(metin: str) -> Tuple[List[BuildMesaj], List[BuildMesaj]]:
    hatalar, uyarilar = [], []
    for m in _MSBUILD_RE.finditer(metin):
        msj = BuildMesaj(
            dosya=m.group("dosya").strip(),
            satir=int(m.group("satir")),
            kolon=int(m.group("kolon")),
            seviye=m.group("seviye"),
            kod=m.group("kod"),
            mesaj=m.group("mesaj").strip(),
        )
        (hatalar if msj.seviye == "error" else uyarilar).append(msj)
    return hatalar, uyarilar


# ═══════════════════════════════════════════════════════════════════════════
#  DOTNET İŞLEMLERİ
# ═══════════════════════════════════════════════════════════════════════════
SABLONLAR = {
    "console":  ("console",  "Konsol"),
    "classlib": ("classlib", "Sınıf kütüphanesi"),
    "webapi":   ("webapi",   "Web API"),
    "mvc":      ("mvc",      "MVC"),
    "razor":    ("razor",    "Razor Pages"),
    "blazor":   ("blazor",   "Blazor Server"),
    "worker":   ("worker",   "Background Service"),
    "grpc":     ("grpc",     "gRPC"),
    "xunit":    ("xunit",    "xUnit test"),
}


def dotnet_new(kok: Path, sablon: str, ad: str) -> Tuple[bool, str]:
    hedef = kok / ad
    hedef.mkdir(parents=True, exist_ok=True)
    cmd = ["dotnet", "new", sablon, "-n", ad, "-o", str(hedef), "--force"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return False, (r.stderr or r.stdout)[:500]
        return True, str(hedef)
    except Exception as e:
        return False, str(e)


def dotnet_build(proje_dizini: Path, timeout: int = 180,
                 tur: int = 0) -> BuildSonuc:
    t0 = time.time()
    sln = list(proje_dizini.glob("*.sln"))
    hedef = sln[0] if sln else proje_dizini
    try:
        r = subprocess.run(
            ["dotnet", "build", str(hedef), "--nologo",
             "-v", "quiet", "-p:WarningLevel=4"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(proje_dizini),
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return BuildSonuc(False, -1, "", f"build timeout ({timeout}s)",
                          sure=time.time() - t0, tur=tur)
    except Exception as e:
        return BuildSonuc(False, -1, "", str(e), sure=time.time() - t0, tur=tur)

    birlesik = (r.stdout or "") + "\n" + (r.stderr or "")
    hatalar, uyarilar = msbuild_parse(birlesik)
    return BuildSonuc(
        basarili=(r.returncode == 0 and not hatalar),
        cikis=r.returncode,
        stdout=r.stdout or "",
        stderr=r.stderr or "",
        hatalar=hatalar, uyarilar=uyarilar,
        sure=time.time() - t0, tur=tur)


def dotnet_test(proje_dizini: Path, timeout: int = 240,
                tur: int = 0) -> Dict[str, Any]:
    t0 = time.time()
    sln = list(proje_dizini.glob("*.sln"))
    if sln:
        hedef = sln[0]
    else:
        adaylar = list(proje_dizini.glob("**/*.csproj"))
        test_proj = next((p for p in adaylar if "test" in p.name.lower()), None)
        if not test_proj:
            return {"calisti": False, "sebep": "test projesi yok", "sure": 0}
        hedef = test_proj
    try:
        r = subprocess.run(
            ["dotnet", "test", str(hedef), "--nologo", "-v", "quiet"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(proje_dizini), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"calisti": False, "sebep": f"timeout", "sure": time.time() - t0}
    except Exception as e:
        return {"calisti": False, "sebep": str(e), "sure": time.time() - t0}

    birlesik = (r.stdout or "") + "\n" + (r.stderr or "")
    gecen = kalan = 0
    m1 = re.search(r"Passed!\s*[-–]\s*Passed:\s*(\d+)", birlesik)
    m2 = re.search(r"Failed!\s*[-–]\s*Failed:\s*(\d+),\s*Passed:\s*(\d+)", birlesik)
    if m1:
        gecen = int(m1.group(1))
    elif m2:
        kalan = int(m2.group(1)); gecen = int(m2.group(2))
    basarisiz = re.findall(r"^\s*Failed\s+(\S+)", birlesik, re.MULTILINE)
    return {
        "calisti": True, "basarili": r.returncode == 0, "cikis": r.returncode,
        "gecen": gecen, "kalan": kalan,
        "basarisiz_testler": basarisiz[:15],
        "cikti": birlesik[:4000], "sure": time.time() - t0, "tur": tur,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  PROJE DOSYA I/O
# ═══════════════════════════════════════════════════════════════════════════
def proje_oku(kok: Path, max_dosya: int = 30,
              max_boyut: int = 8000) -> List[Dict[str, str]]:
    atla = {"bin", "obj", ".vs", ".vscode", ".git", "node_modules"}
    uzanti_ok = {".cs", ".csproj", ".sln", ".json", ".razor", ".xaml",
                 ".html", ".css", ".js", ".md", ".config"}
    dosyalar = []
    for p in sorted(kok.rglob("*")):
        if not p.is_file():
            continue
        if any(part in atla for part in p.parts):
            continue
        if p.suffix.lower() not in uzanti_ok:
            continue
        try:
            ic = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(ic) > max_boyut:
            ic = ic[:max_boyut] + "\n// ... (kırpıldı)"
        dosyalar.append({"yol": str(p.relative_to(kok)), "icerik": ic})
        if len(dosyalar) >= max_dosya:
            break
    return dosyalar


def projeye_yaz(kok: Path, dosyalar: List[Dict]) -> List[str]:
    yazilan = []
    kok_res = kok.resolve()
    for d in dosyalar:
        yol = str(d.get("yol", "")).strip().lstrip("/\\")
        ic = d.get("icerik", "")
        if not yol or not ic:
            continue
        hedef = (kok / yol).resolve()
        try:
            hedef.relative_to(kok_res)
        except ValueError:
            continue
        hedef.parent.mkdir(parents=True, exist_ok=True)
        try:
            hedef.write_text(str(ic), encoding="utf-8")
            yazilan.append(yol)
        except Exception:
            pass
    return yazilan


# ═══════════════════════════════════════════════════════════════════════════
#  C# STATİK DENETLEYİCİ (Roslyn-style, regex tabanlı)
# ═══════════════════════════════════════════════════════════════════════════
class CSharpDenetleyici:
    KURALLAR: List[Tuple[str, str, str]] = [
        (r"\basync\s+void\b(?!\s+(?:Main|EventHandler|\w*EventHandler))",
         "uyari", "async void kullanma — Task/Task<T> döndür"),
        (r"\.Result\b|\.Wait\(\s*\)", "uyari",
         ".Result / .Wait() deadlock riski — await kullan"),
        (r"\bThread\.Sleep\b", "uyari",
         "Thread.Sleep yerine await Task.Delay kullan"),
        (r"catch\s*\(\s*Exception\s*\)\s*\{\s*\}", "uyari",
         "Boş catch bloğu — logla veya yeniden fırlat"),
        (r"catch\s*\(\s*Exception\s+\w+\s*\)\s*\{[^}]*throw\s+\w+;",
         "uyari", "throw ex; yerine throw; kullan"),
        (r"\bnew\s+Random\s*\(\s*\)", "uyari",
         "Random.Shared kullan (thread-safe)"),
        (r"\.ToList\(\)\s*\.\s*\w+\(", "uyari",
         "Gereksiz ToList() — LINQ zinciri bozulur"),
        (r"\bstring\.Format\s*\(", "uyari",
         "string.Format yerine $\"...\" interpolasyon kullan"),
        (r"\bGC\.Collect\b", "uyari",
         "GC.Collect manuel çağırma"),
        (r"\bThread\.Abort\b", "error",
         "Thread.Abort .NET Core+ üzerinde desteklenmez"),
        (r"\bTODO\b|\bFIXME\b", "uyari",
         "TODO/FIXME bırakma — kodu tamamla"),
        (r"public\s+static\s+\w+\s+HttpClient\b", "uyari",
         "HttpClient static yapma — IHttpClientFactory kullan"),
        (r"\.ConfigureAwait\s*\(\s*false\s*\)", "uyari",
         "ASP.NET Core'da ConfigureAwait(false) gereksiz"),
    ]

    @classmethod
    def denetle(cls, dosyalar: List[Dict]) -> List[Dict]:
        bulgular = []
        for d in dosyalar:
            yol = d.get("yol", "")
            ic = str(d.get("icerik", ""))
            if not yol.endswith(".cs"):
                continue
            # string literal ve yorumları çıkar (basit)
            temiz = re.sub(r'//[^\n]*', '', ic)
            temiz = re.sub(r'/\*.*?\*/', '', temiz, flags=re.DOTALL)
            temiz = re.sub(r'@"(?:[^"]|"")*"', '""', temiz)
            temiz = re.sub(r'"(?:[^"\\]|\\.)*"', '""', temiz)
            for satir_no, satir in enumerate(temiz.splitlines(), 1):
                for regex, seviye, msj in cls.KURALLAR:
                    if re.search(regex, satir):
                        bulgular.append({
                            "dosya": yol, "satir": satir_no,
                            "seviye": seviye, "mesaj": msj,
                        })
        return bulgular


# ═══════════════════════════════════════════════════════════════════════════
#  DAHİLİ C# SYNTAX CHECKER (LLM'siz, kabaca)
# ═══════════════════════════════════════════════════════════════════════════
class CSharpSyntaxKontrol:
    """Kabaca sözdizimi kontrolü — { } ( ) [ ] dengesi, using, namespace."""

    @staticmethod
    def denetle(dosyalar: List[Dict]) -> List[str]:
        problemler = []
        for d in dosyalar:
            yol = d.get("yol", "")
            ic = str(d.get("icerik", ""))
            if not yol.endswith(".cs"):
                continue

            # Yorumları ve string'leri çıkar
            temiz = re.sub(r'@"(?:[^"]|"")*"', '""', ic, flags=re.DOTALL)
            temiz = re.sub(r'"(?:[^"\\]|\\.)*"', '""', temiz)
            temiz = re.sub(r"'(?:[^'\\]|\\.)*'", "''", temiz)
            temiz = re.sub(r'//[^\n]*', '', temiz)
            temiz = re.sub(r'/\*.*?\*/', '', temiz, flags=re.DOTALL)

            # Denge kontrolü
            sayac = {"{": 0, "(": 0, "[": 0}
            esles = {"}": "{", ")": "(", "]": "["}
            for i, ch in enumerate(temiz):
                if ch in sayac:
                    sayac[ch] += 1
                elif ch in esles:
                    sayac[esles[ch]] -= 1
                    if sayac[esles[ch]] < 0:
                        problemler.append(f"{yol}: Fazladan '{ch}' (konum {i})")
                        break
            for k, v in sayac.items():
                if v != 0:
                    problemler.append(f"{yol}: Eksik '{k}' kapanışı ({v} adet)")

            # using System; kontrolü
            if not re.search(r"^\s*using\s+", ic, re.MULTILINE):
                if "namespace " in ic or "class " in ic:
                    problemler.append(f"{yol}: Hiç using yok — emin misin?")

            # namespace var mı?
            if "class " in ic and "namespace " not in ic and "top-level" not in ic.lower():
                # top-level statement olabilir — uyarı verme
                pass

        return problemler


# ═══════════════════════════════════════════════════════════════════════════
#  PROMPT'LAR — GÜVENLİ (Python string kaçışları düzgün)
# ═══════════════════════════════════════════════════════════════════════════

# NOT: C# örneklerinde triple-quote (raw string) kullanılıyorsa
# Python tarafında \"\"\" şeklinde kaçışlı yazılmıştır.

PROJE_TESPIT = textwrap.dedent("""
    Sen kıdemli bir .NET mimarısın. Kullanıcı isteğinden DOĞRU dotnet şablonunu seç.

    Şablonlar: console, classlib, webapi, mvc, razor, blazor, worker, grpc, xunit

    Şu JSON'u döndür:
    {
      "sablon": "webapi",
      "proje_adi": "TodoApi",
      "sebep": "kullanıcı REST API istedi",
      "ozellikler": ["jwt", "ef-core", "swagger"]
    }
    SADECE JSON.
""").strip()


PLAN = textwrap.dedent("""
    Sen kıdemli bir .NET mimarısın. Proje planını çıkar.

    Mevcut iskelet dosyaları:
    {iskelet}

    Kullanıcı isteği: {istek}

    Şu JSON'u döndür:
    {
      "ozet": "tek cümle",
      "gereksinimler": ["madde 1"],
      "mimari": "minimal-api | controller | layered | clean",
      "nuget": ["Microsoft.EntityFrameworkCore.Sqlite"],
      "yazilacak_dosyalar": [
        {{"yol": "Program.cs", "sorumluluk": "..."}}
      ],
      "notlar": "..."
    }
    SADECE JSON.
""").strip()


# C# örnekleri burada — triple-quote YOK, kaçışlı yazılmış
KOD = textwrap.dedent("""
    Sen dünyanın en iyi C# geliştiricisisin. 15+ yıl .NET deneyimin var.
    Aşağıdaki isteği KUSURSUZ, DERLENEBİLİR, PRODÜKSİYON KALİTESİNDE kodla.

    ZORUNLU KURALLAR:

    C# SÖZDİZİMİ:
    - .NET 8, C# 12
    - Dosya-scoped namespace: `namespace X;`
    - Primary constructor: `class Foo(int x) { }`
    - Collection expression: `int[] a = [1, 2, 3];`
    - Record: `public record Todo(int Id, string Title);`
    - Pattern matching: `x switch { > 0 => "poz", _ => "sifir" }`
    - C# 11 raw string yerine normal string kullan (karışıklık olmasın)
    - `required` üye: `public required string Name {{ get; init; }}`

    NULLABLE:
    - `ArgumentNullException.ThrowIfNull(x)`
    - `??`, `?.`, `is null`, `is not null`

    ASYNC:
    - I/O için `async Task<T>` / `ValueTask`
    - `async void` YASAK (event handler hariç)
    - `CancellationToken` parametresi ekle

    KALİTE:
    - Kısaltma YOK, TODO YOK, "..." YOK
    - Public üyeye kısa XML doc
    - DI: constructor injection

    ANTI-PATTERN (YAPMA):
    - catch (Exception) {{ }} boş catch
    - .Result veya .Wait() — deadlock
    - Thread.Sleep — Task.Delay kullan
    - GC.Collect() manuel
    - throw ex; — throw; kullan

    ÖRNEK KALİTE (bu seviyede yaz):

        /// <summary>Kullanıcı servisi.</summary>
        public sealed class UserService(AppDbContext db, ILogger<UserService> log)
        {{
            public async Task<User?> GetAsync(int id, CancellationToken ct = default)
            {{
                ArgumentOutOfRangeException.ThrowIfNegativeOrZero(id);
                return await db.Users.AsNoTracking()
                    .FirstOrDefaultAsync(u => u.Id == id, ct);
            }}
        }}

    MEVCUT İSKELET:
    {iskelet}

    PLAN:
    {plan}

    İSTEK:
    {istek}

    Şu JSON'u döndür:
    {{
      "dosyalar": [
        {{"yol": "Program.cs", "icerik": "TAM İÇERİK"}}
      ],
      "aciklama": "kısa özet"
    }}
    SADECE JSON. `...` yok, `// TODO` yok.
""").strip()


DERLEME_DUZELT = textwrap.dedent("""
    C# kodu derlenmedi. Hataları KÖK NEDENİYLE çöz.

    DERLEME HATALARI:
    {hatalar}

    UYARILAR:
    {uyarilar}

    MEVCUT DOSYALAR:
    {dosyalar}

    TALİMATLAR:
    1. Her hatayı TEK TEK incele
    2. Eksik `using` varsa ekle
    3. Eksik NuGet paketi varsa csproj_guncelle alanına yaz
    4. Tip uyuşmazlıklarını düzelt
    5. Hiçbir şeyi SİLME — sadece düzelt ve ekle
    6. TAM dosyaları döndür

    Şu JSON'u döndür:
    {{
      "dosyalar": [{{"yol": "Program.cs", "icerik": "TAM KOD"}}],
      "csproj_guncelle": [{{"package": "PaketAdi", "surum": "8.0.0"}}],
      "aciklama": "ne düzeltildi"
    }}
    SADECE JSON.
""").strip()


TEST_YAZ = textwrap.dedent("""
    Sen xUnit uzmanısın. Aşağıdaki C# kodu için TAM çalışan testler yaz.

    ZORUNLU KURALLAR:
    - xUnit + FluentAssertions
    - [Fact], [Theory] + [InlineData]
    - Arrange-Act-Assert
    - Her public metot için en az 1 test
    - Null, sınır değerler, exception senaryoları
    - Assert.Throws<ArgumentNullException>(() => ...)
    - Test isimleri: MetotAdi_Senaryo_BeklenenSonuc

    PRODÜKSİYON KODU:
    {kod}

    ANA PROJE: {proje_adi}
    TEST PROJESİ: {proje_adi}.Tests

    Şu JSON'u döndür:
    {{
      "dosyalar": [
        {{"yol": "TodoServiceTests.cs", "icerik": "TAM TEST KODU"}}
      ]
    }}
    SADECE JSON.
""").strip()


TEST_DUZELT = textwrap.dedent("""
    Testler başarısız. Testleri veya kodu düzelt.

    TEST SONUCU:
    {test_sonuc}

    TEST DOSYALARI:
    {test_dosyalar}

    PRODÜKSİYON DOSYALARI:
    {prod_dosyalar}

    KURAL: Prodüksiyon kodu doğruysa testi düzelt. Test doğruysa kodu düzelt.

    Şu JSON'u döndür:
    {{
      "test_dosyalari": [{{"yol": "XTests.cs", "icerik": "..."}}],
      "prod_dosyalari": [{{"yol": "X.cs", "icerik": "..."}}],
      "aciklama": "..."
    }}
    SADECE JSON.
""").strip()


DENETIM = textwrap.dedent("""
    Sen acımasız bir C# kod denetçisisin.

    KOD:
    {kod}

    KONTROL ET:
    - Nullable ihlalleri
    - Async yanlış kullanımı
    - Dispose pattern
    - LINQ verimliliği (N+1)
    - Exception handling
    - DI lifetime hataları
    - Thread safety
    - SQL injection
    - Güvenlik (auth, validation, CORS)

    Şu JSON'u döndür:
    {{
      "kritik": [{{"dosya": "X.cs", "sorun": "...", "cozum": "..."}}],
      "orta": [{{"dosya": "X.cs", "sorun": "..."}}],
      "dusuk": [{{"dosya": "X.cs", "sorun": "..."}}],
      "skor": 85,
      "ozet": "tek cümle"
    }}
    SADECE JSON.
""").strip()


# ═══════════════════════════════════════════════════════════════════════════
#  SQLITE
# ═══════════════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS uretim(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  istek TEXT NOT NULL, sablon TEXT, proje_adi TEXT,
  kod TEXT, skor INTEGER, test_gecen INTEGER, test_kalan INTEGER,
  mod TEXT, tarih TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pattern(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  istek TEXT NOT NULL, anahtar TEXT NOT NULL,
  sablon TEXT, dosyalar TEXT, skor INTEGER, tarih TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pattern_anahtar ON pattern(anahtar);
CREATE TABLE IF NOT EXISTS ayar(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def _tx(self):
        try:
            yield; self.conn.commit()
        except Exception:
            self.conn.rollback(); raise

    def uretim_ekle(self, istek: str, sablon: str, proje_adi: str,
                    kod: str, skor: int, test_gecen: int = 0,
                    test_kalan: int = 0, mod: str = "full") -> int:
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO uretim(istek,sablon,proje_adi,kod,skor,"
                "test_gecen,test_kalan,mod,tarih) VALUES(?,?,?,?,?,?,?,?,?)",
                (istek, sablon, proje_adi, kod, skor, test_gecen,
                 test_kalan, mod,
                 dt.datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid or 0

    def pattern_ekle(self, istek: str, anahtar: str, sablon: str,
                     dosyalar: List[Dict], skor: int) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO pattern(istek,anahtar,sablon,dosyalar,skor,tarih) "
                "VALUES(?,?,?,?,?,?)",
                (istek, anahtar, sablon,
                 json.dumps(dosyalar, ensure_ascii=False)[:8000], skor,
                 dt.datetime.now().isoformat(timespec="seconds")))

    def ayar_set(self, k: str, v: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO ayar(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def ayar_get(self, k: str, d: str = "") -> str:
        r = self.conn.execute("SELECT v FROM ayar WHERE k=?", (k,)).fetchone()
        return r["v"] if r else d

    def son(self, n: int = 10):
        return self.conn.execute(
            "SELECT id,istek,sablon,skor,test_gecen,test_kalan,mod,tarih "
            "FROM uretim ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def sayi(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM uretim").fetchone()[0]

    def kapat(self) -> None:
        try:
            self.conn.commit(); self.conn.close()
        except Exception:
            pass


def _tokenize(s: str) -> set:
    return set(re.findall(r"[\wçğıöşü#+]+", s.lower()))


# ═══════════════════════════════════════════════════════════════════════════
#  SİNYAL
# ═══════════════════════════════════════════════════════════════════════════
_sinyal_izin = False


def _sinyal_handler(sig, frame):
    global _sinyal_izin
    if _sinyal_izin:
        print(f"\n{C.YL}Kapatılıyor...{C.R}")
        sys.exit(0)
    print(f"\n{C.RD}⚠ Ctrl+C engellendi. Çıkmak için: /kapat{C.R}")


# ═══════════════════════════════════════════════════════════════════════════
#  PROJE VERİ YAPISI
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Proje:
    istek: str
    sablon: str = "console"
    proje_adi: str = "BebekProje"
    kok: Optional[Path] = None
    plan: Optional[Dict] = None
    dosyalar: List[Dict] = field(default_factory=list)
    test_dosyalari: List[Dict] = field(default_factory=list)
    test_proje_adi: Optional[str] = None
    build_gecmis: List[BuildSonuc] = field(default_factory=list)
    test_gecmis: List[Dict] = field(default_factory=list)
    statik_bulgular: List[Dict] = field(default_factory=list)
    syntax_problemler: List[str] = field(default_factory=list)
    denetim: Optional[Dict] = None
    skor: int = 0
    calisan: bool = False


# ═══════════════════════════════════════════════════════════════════════════
#  MOTOR
# ═══════════════════════════════════════════════════════════════════════════
class AkilliMotor:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg

    # ── yazdırma ──────────────────────────────────────────
    def _bas(self, no: int, ad: str, aciklama: str = "") -> None:
        print(f"\n{C.MG}{'━' * 78}{C.R}")
        print(f"{C.B}{C.CY}  ADIM {no}  ▸  {ad.upper()}{C.R}")
        if aciklama:
            print(f"{C.D}  {aciklama}{C.R}")
        print(f"{C.MG}{'━' * 78}{C.R}")

    def _ok(self, m: str, sure: float = 0.0) -> None:
        s = f"  {C.D}({sure:.1f}s){C.R}" if sure else ""
        print(f"  {C.GR}✓ {m}{C.R}{s}")

    def _uyari(self, m: str) -> None:
        print(f"  {C.YL}⚠ {m}{C.R}")

    def _hata(self, m: str) -> None:
        print(f"  {C.RD}✗ {m}{C.R}")

    # ── 1) TESPİT ─────────────────────────────────────────
    def _tespit(self, istek: str) -> Tuple[str, str]:
        self._bas(1, "şablon tespiti", "İstekten doğru dotnet şablonunu seç")
        t0 = time.time()
        v, hata = ollama_cagri(
            PROJE_TESPIT, f"İstek: {istek}",
            self.cfg, sicaklik=0.1, max_tok=400,
            canli=self.cfg.streaming, baslik="sablon seçiliyor")
        if hata or not v:
            self._uyari(f"Tespit başarısız ({hata}), console varsayılıyor")
            return "console", "BebekProje"
        sablon = v.get("sablon", "console")
        if sablon not in SABLONLAR:
            sablon = "console"
        ad = re.sub(r"[^A-Za-z0-9_]", "", v.get("proje_adi", "BebekProje")) or "BebekProje"
        if ad[0].isdigit():
            ad = "P" + ad
        self._ok(f"Şablon: {sablon} | Proje: {ad}", time.time() - t0)
        return sablon, ad

    # ── 2) İSKELET ────────────────────────────────────────
    def _iskelet(self, p: Proje) -> bool:
        self._bas(2, "iskelet", f"dotnet new {p.sablon}")
        t0 = time.time()
        if not dotnet_var():
            self._uyari("dotnet yok — LITE mod: iskelet elle oluşturuluyor")
            self._lite_iskelet(p)
            self._ok("Minimum iskelet hazır", time.time() - t0)
            return True
        ok, sonuc = dotnet_new(p.kok, SABLONLAR[p.sablon][0], p.proje_adi)
        if not ok:
            self._hata(f"Şablon kurulamadı: {sonuc}")
            return False
        self._ok(f"İskelet: {sonuc}", time.time() - t0)
        p.dosyalar = proje_oku(p.kok, max_dosya=20)
        return True

    def _lite_iskelet(self, p: Proje) -> None:
        """dotnet yoksa minimum csproj + boş Program.cs üret."""
        proje_dizini = p.kok / p.proje_adi
        proje_dizini.mkdir(parents=True, exist_ok=True)
        sdk = "Microsoft.NET.Sdk.Web" if p.sablon in ("webapi", "mvc", "razor", "blazor") else "Microsoft.NET.Sdk"
        cikti_tipi = "" if p.sablon == "classlib" else "    <OutputType>Exe</OutputType>\n"
        csproj = (
            f'<Project Sdk="{sdk}">\n'
            f'  <PropertyGroup>\n'
            f'{cikti_tipi}'
            f'    <TargetFramework>net8.0</TargetFramework>\n'
            f'    <ImplicitUsings>enable</ImplicitUsings>\n'
            f'    <Nullable>enable</Nullable>\n'
            f'    <RootNamespace>{p.proje_adi}</RootNamespace>\n'
            f'  </PropertyGroup>\n'
            f'</Project>\n'
        )
        (proje_dizini / f"{p.proje_adi}.csproj").write_text(csproj, encoding="utf-8")
        program = (
            "// Program.cs\n"
            "Console.WriteLine(\"Bebek Dev - LITE mod iskelet\");\n"
        )
        (proje_dizini / "Program.cs").write_text(program, encoding="utf-8")
        p.dosyalar = proje_oku(p.kok, max_dosya=20)

    # ── 3) PLAN ───────────────────────────────────────────
    def _plan(self, p: Proje) -> bool:
        self._bas(3, "plan", "Mimari ve dosya yapısı")
        t0 = time.time()
        iskelet = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik'][:1500]}"
            for d in p.dosyalar[:10])
        v, hata = ollama_cagri(
            PLAN.format(iskelet=iskelet, istek=p.istek),
            "Planı çıkar.", self.cfg,
            sicaklik=0.2, max_tok=1500,
            canli=self.cfg.streaming, baslik="plan")
        if hata or not v:
            self._hata(f"Plan başarısız: {hata}")
            return False
        p.plan = v
        self._ok(f"Özet: {v.get('ozet', '?')}", time.time() - t0)
        if v.get("nuget"):
            print(f"     {C.CY}NuGet: {', '.join(v['nuget'][:6])}{C.R}")
        return True

    # ── 4) KOD ────────────────────────────────────────────
    def _kod(self, p: Proje) -> bool:
        self._bas(4, "kod", "Prodüksiyon kalitesinde C# yazılıyor")
        t0 = time.time()
        iskelet = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar)
        v, hata = ollama_cagri(
            KOD.format(
                iskelet=iskelet[:12000],
                plan=json.dumps(p.plan, ensure_ascii=False),
                istek=p.istek),
            f"Kodu üret. İstek: {p.istek}",
            self.cfg, sicaklik=self.cfg.sicaklik, max_tok=self.cfg.max_tok,
            canli=self.cfg.streaming, baslik="kod")
        if hata or not v:
            self._hata(f"Kod üretilemedi: {hata}")
            return False
        yeni = v.get("dosyalar") or []
        if not yeni:
            self._hata("LLM dosya döndürmedi")
            return False

        # Proje dizini içine yaz
        proje_dizini = p.kok / p.proje_adi
        yazilan = projeye_yaz(proje_dizini, yeni)
        satir = sum(str(d.get("icerik", "")).count("\n") + 1 for d in yeni)
        self._ok(f"{len(yazilan)} dosya, {satir} satır", time.time() - t0)

        # NuGet
        nuget = (p.plan or {}).get("nuget") or []
        if nuget and dotnet_var():
            self._nuget_ekle(proje_dizini, nuget)

        p.dosyalar = proje_oku(p.kok, max_dosya=30)
        return True

    def _nuget_ekle(self, proje_dizini: Path, paketler: List[str]) -> None:
        csproj = next(proje_dizini.glob("*.csproj"), None)
        if not csproj:
            return
        print(f"  {C.D}📦 NuGet: {', '.join(paketler[:5])}{C.R}")
        for pk in paketler[:10]:
            pk = pk.strip()
            if not pk:
                continue
            try:
                subprocess.run(
                    ["dotnet", "add", str(csproj), "package", pk, "--no-restore"],
                    capture_output=True, text=True, timeout=60,
                    encoding="utf-8", errors="replace")
            except Exception:
                pass

    # ── 5) DERLEME DÖNGÜSÜ ────────────────────────────────
    def _derleme_dongusu(self, p: Proje) -> bool:
        if not dotnet_var():
            self._bas(5, "derleme", "dotnet yok — atlandı (LITE mod)")
            self._uyari("Kodu PC'ye taşıyıp `dotnet build` çalıştır")
            return False

        self._bas(5, "derleme",
                  f"Gerçek dotnet build — max {self.cfg.max_build_tur} tur")
        for tur in range(1, self.cfg.max_build_tur + 1):
            print(f"\n  {C.CY}▶ Tur {tur}/{self.cfg.max_build_tur} — "
                  f"dotnet build{C.R}")
            sonuc = dotnet_build(p.kok, tur=tur)
            p.build_gecmis.append(sonuc)

            if sonuc.basarili:
                self._ok(f"Derleme TEMİZ ({sonuc.sure:.1f}s)")
                if sonuc.uyarilar:
                    self._uyari(f"{len(sonuc.uyarilar)} uyarı")
                p.calisan = True
                return True

            print(f"  {C.RD}✗ {len(sonuc.hatalar)} derleme hatası{C.R}")
            for h in sonuc.hatalar[:8]:
                kisa = h.dosya.split(os.sep)[-1]
                print(f"     {C.RD}{kisa}({h.satir},{h.kolon}): "
                      f"{h.kod} {h.mesaj[:80]}{C.R}")
            if len(sonuc.hatalar) > 8:
                print(f"     {C.D}... +{len(sonuc.hatalar) - 8} hata{C.R}")

            if tur == self.cfg.max_build_tur:
                self._hata(f"Max tur aşıldı")
                return False

            print(f"\n  {C.YL}↻ Hatalar LLM'e geri besleniyor...{C.R}")
            if not self._hata_duzelt(p, sonuc, tur):
                return False
        return False

    def _hata_duzelt(self, p: Proje, sonuc: BuildSonuc, tur: int) -> bool:
        hatalar_str = "\n".join(f"  {h.kisa()}" for h in sonuc.hatalar[:30])
        uyarilar_str = "\n".join(f"  {u.kisa()}" for u in sonuc.uyarilar[:15])
        dosyalar_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}" for d in p.dosyalar)
        v, hata = ollama_cagri(
            DERLEME_DUZELT.format(
                hatalar=hatalar_str or "(yok)",
                uyarilar=uyarilar_str or "(yok)",
                dosyalar=dosyalar_str[:14000]),
            f"{len(sonuc.hatalar)} hatayı düzelt.",
            self.cfg, sicaklik=0.15, max_tok=self.cfg.max_tok,
            canli=self.cfg.streaming, baslik=f"tur {tur} düzeltme")
        if hata or not v:
            self._hata(f"Düzeltme başarısız: {hata}")
            return False
        yeni = v.get("dosyalar") or []
        if yeni:
            proje_dizini = p.kok / p.proje_adi
            yazilan = projeye_yaz(proje_dizini, yeni)
            self._ok(f"{len(yazilan)} dosya güncellendi")
        for g in (v.get("csproj_guncelle") or []):
            paket = g.get("package", "")
            surum = g.get("surum", "")
            if not paket:
                continue
            csproj = next((p.kok / p.proje_adi).glob("*.csproj"), None)
            if csproj:
                try:
                    cmd = ["dotnet", "add", str(csproj), "package", paket]
                    if surum:
                        cmd += ["--version", surum]
                    cmd += ["--no-restore"]
                    subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=60, encoding="utf-8",
                                   errors="replace")
                    self._ok(f"📦 {paket} {surum}")
                except Exception:
                    pass
        p.dosyalar = proje_oku(p.kok, max_dosya=30)
        return True

    # ── 6) STATİK DENETİM ─────────────────────────────────
    def _statik(self, p: Proje) -> None:
        self._bas(6, "statik denetim", "Kural tabanlı C# analizi")
        t0 = time.time()
        p.statik_bulgular = CSharpDenetleyici.denetle(p.dosyalar)
        p.syntax_problemler = CSharpSyntaxKontrol.denetle(p.dosyalar)

        if p.syntax_problemler:
            self._uyari(f"{len(p.syntax_problemler)} syntax şüphesi")
            for s in p.syntax_problemler[:5]:
                print(f"     {C.YL}⚠ {s}{C.R}")

        if not p.statik_bulgular:
            self._ok(f"Kural ihlali yok ({time.time() - t0:.1f}s)")
            return
        hata_s = sum(1 for b in p.statik_bulgular if b["seviye"] == "error")
        uyari_s = sum(1 for b in p.statik_bulgular if b["seviye"] == "uyari")
        self._ok(f"{hata_s} hata, {uyari_s} uyarı", time.time() - t0)
        for b in p.statik_bulgular[:6]:
            renk = C.RD if b["seviye"] == "error" else C.YL
            print(f"     {renk}{b['dosya']}:{b['satir']} {b['mesaj'][:70]}{C.R}")

    # ── 7) LLM DENETİM ────────────────────────────────────
    def _denetim(self, p: Proje) -> int:
        self._bas(7, "LLM denetimi", "Kod kalitesi ve güvenlik")
        t0 = time.time()
        kod_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar if d["yol"].endswith(".cs"))
        v, hata = ollama_cagri(
            DENETIM.format(kod=kod_str[:14000]),
            "Kodu denetle.", self.cfg,
            sicaklik=0.1, max_tok=1500,
            canli=self.cfg.streaming, baslik="denetim")
        if hata or not v:
            self._uyari(f"Denetim atlandı: {hata}")
            p.skor = 80
            return p.skor
        p.denetim = v
        try:
            p.skor = int(v.get("skor", 0))
        except Exception:
            p.skor = 0
        renk = C.GR if p.skor >= self.cfg.min_skor else C.YL
        print(f"  {renk}Skor: {p.skor}/100{C.R}")
        for k in (v.get("kritik") or [])[:3]:
            print(f"     {C.RD}✗ {str(k.get('sorun', '?'))[:80]}{C.R}")
        for o in (v.get("orta") or [])[:3]:
            print(f"     {C.YL}⚠ {str(o.get('sorun', '?'))[:80]}{C.R}")
        if v.get("ozet"):
            print(f"     {C.CY}→ {str(v['ozet'])[:100]}{C.R}")
        return p.skor

    # ── 8) TEST KURULUM ───────────────────────────────────
    def _test_kur(self, p: Proje) -> bool:
        if not self.cfg.otomatik_test or not dotnet_var():
            return False
        self._bas(8, "test projesi", "xUnit kurulumu")
        t0 = time.time()
        test_ad = f"{p.proje_adi}.Tests"
        ok, _ = dotnet_new(p.kok, "xunit", test_ad)
        if not ok:
            self._hata("Test projesi kurulamadı")
            return False
        test_dir = p.kok / test_ad
        try:
            subprocess.run(
                ["dotnet", "add", str(test_dir),
                 "reference", str(p.kok / p.proje_adi)],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace")
        except Exception:
            pass
        test_csproj = next(test_dir.glob("*.csproj"), None)
        if test_csproj:
            for pk in ["FluentAssertions", "Moq"]:
                try:
                    subprocess.run(
                        ["dotnet", "add", str(test_csproj), "package", pk, "--no-restore"],
                        capture_output=True, text=True, timeout=60,
                        encoding="utf-8", errors="replace")
                except Exception:
                    pass
        p.test_proje_adi = test_ad
        self._ok(f"Test projesi: {test_ad}", time.time() - t0)
        return self._test_yaz(p)

    def _test_yaz(self, p: Proje) -> bool:
        print(f"\n  {C.CY}▶ xUnit testleri üretiliyor...{C.R}")
        t0 = time.time()
        kod_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar
            if d["yol"].endswith(".cs") and "Tests" not in d["yol"])
        v, hata = ollama_cagri(
            TEST_YAZ.format(kod=kod_str[:12000], proje_adi=p.proje_adi),
            f"{p.proje_adi} için xUnit testleri yaz.",
            self.cfg, sicaklik=0.2, max_tok=self.cfg.max_tok,
            canli=self.cfg.streaming, baslik="testler")
        if hata or not v:
            self._hata(f"Test üretilemedi: {hata}")
            return False
        test_dir = p.kok / (p.test_proje_adi or "")
        test_dosyalar = v.get("dosyalar") or []
        if test_dosyalar:
            projeye_yaz(test_dir, test_dosyalar)
            p.test_dosyalari = test_dosyalar
            self._ok(f"{len(test_dosyalar)} test dosyası", time.time() - t0)
            return True
        return False

    # ── 9) TEST DÖNGÜSÜ ───────────────────────────────────
    def _test_dongusu(self, p: Proje) -> Dict[str, Any]:
        if not self.cfg.otomatik_test or not dotnet_var():
            return {"calisti": False, "sebep": "test devre dışı"}
        self._bas(9, "test", f"dotnet test — max {self.cfg.max_test_tur} tur")
        for tur in range(1, self.cfg.max_test_tur + 1):
            print(f"\n  {C.CY}▶ Tur {tur}/{self.cfg.max_test_tur}{C.R}")
            sonuc = dotnet_test(p.kok, tur=tur)
            p.test_gecmis.append(sonuc)
            if not sonuc.get("calisti"):
                self._uyari(f"Test çalıştırılamadı: {sonuc.get('sebep')}")
                return sonuc
            if sonuc.get("basarili"):
                self._ok(f"TÜM TESTLER GEÇTİ ({sonuc['gecen']} test)")
                return sonuc
            print(f"  {C.RD}✗ {sonuc.get('kalan', 0)} kaldı, "
                  f"{sonuc.get('gecen', 0)} geçti{C.R}")
            for t in sonuc.get("basarisiz_testler", [])[:5]:
                print(f"     {C.RD}✗ {t}{C.R}")
            if tur == self.cfg.max_test_tur:
                return sonuc
            print(f"\n  {C.YL}↻ Testler LLM'e geri besleniyor...{C.R}")
            if not self._test_duzelt(p, sonuc):
                return sonuc
        return p.test_gecmis[-1]

    def _test_duzelt(self, p: Proje, sonuc: Dict) -> bool:
        test_dosyalar_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}" for d in p.test_dosyalari)
        prod_dosyalar_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar if d["yol"].endswith(".cs"))
        v, hata = ollama_cagri(
            TEST_DUZELT.format(
                test_sonuc=sonuc.get("cikti", "")[:4000],
                test_dosyalar=test_dosyalar_str[:8000],
                prod_dosyalar=prod_dosyalar_str[:10000]),
            "Test hatasını düzelt.", self.cfg,
            sicaklik=0.2, max_tok=self.cfg.max_tok,
            canli=self.cfg.streaming, baslik="test düzeltme")
        if hata or not v:
            self._hata(f"Test düzeltme başarısız: {hata}")
            return False
        test_dir = p.kok / (p.test_proje_adi or "")
        if v.get("test_dosyalari"):
            projeye_yaz(test_dir, v["test_dosyalari"])
            p.test_dosyalari = v["test_dosyalari"]
        if v.get("prod_dosyalari"):
            projeye_yaz(p.kok / p.proje_adi, v["prod_dosyalari"])
            p.dosyalar = proje_oku(p.kok, max_dosya=30)
            # yeniden derleme
            dr = dotnet_build(p.kok, tur=99)
            if not dr.basarili:
                self._uyari("Prodüksiyon derleme kırıldı")
                return False
        self._ok("Düzeltme uygulandı")
        return True

    # ── ANA AKIŞ ──────────────────────────────────────────
    def uret(self, istek: str) -> Optional[Proje]:
        kok = Path(tempfile.mkdtemp(prefix="bebek_cs_"))
        p = Proje(istek=istek, kok=kok)
        try:
            sablon, ad = self._tespit(istek)
            p.sablon, p.proje_adi = sablon, ad

            if not self._iskelet(p): return p
            if not self._plan(p): return p
            if not self._kod(p): return p

            self._derleme_dongusu(p)
            self._statik(p)
            self._denetim(p)

            if self.cfg.otomatik_test and p.calisan:
                if self._test_kur(p):
                    test_sonuc = self._test_dongusu(p)
                    if test_sonuc.get("basarili"):
                        print(f"\n  {C.GR}{C.B}🎉 PROJE HAZIR — "
                              f"derleme temiz, testler geçti.{C.R}")

            # Öğren
            if self.cfg.ogrenme and p.dosyalar and (p.calisan or not dotnet_var()):
                self.store.pattern_ekle(
                    istek, " ".join(sorted(_tokenize(istek))),
                    p.sablon, p.dosyalar, p.skor)
                print(f"\n  {C.D}💾 Belleğe kaydedildi (skor {p.skor}){C.R}")
            return p
        except Exception as e:
            self._hata(f"Üretim hatası: {e}")
            _log(f"üretim hatası: {e}")
            return p


# ═══════════════════════════════════════════════════════════════════════════
#  UI
# ═══════════════════════════════════════════════════════════════════════════
BANNER = f"""{C.CY}{C.B}
╔══════════════════════════════════════════════════════════════════════════════╗
║   ██████╗ ███████╗██████╗ ███████╗██╗  ██╗    ██████╗ ███████╗██╗   ██╗      ║
║   ██╔══██╗██╔════╝██╔══██╗██╔════╝██║ ██╔╝    ██╔══██╗██╔════╝██║   ██║      ║
║   ██████╔╝█████╗  ██████╔╝█████╗  █████╔╝     ██║  ██║█████╗  ██║   ██║      ║
║   ██╔══██╗██╔══╝  ██╔══██╗██╔══╝  ██╔═██╗     ██║  ██║██╔══╝  ╚██╗ ██╔╝      ║
║   ██████╔╝███████╗██████╔╝███████╗██║  ██╗    ██████╔╝███████╗ ╚████╔╝       ║
║   ╚═════╝ ╚══════╝╚═════╝ ╚══════╝╚═╝  ╚═╝    ╚═════╝ ╚══════╝  ╚═══╝        ║
║                                                                              ║
║              C#  U Z M A N I  •  v{SURUM}  •  Termux Uyumlu                  ║
╚══════════════════════════════════════════════════════════════════════════════╝{C.R}
"""


def bios(cfg: Config, store: Store) -> bool:
    os.system("cls" if os.name == "nt" else "clear")
    print(BANNER)
    time.sleep(0.2)
    print(f"{C.YL}  Sistem kontrolü...{C.R}\n")

    # Python sürümü
    pv = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"  {C.D}[✓]{C.R} Python              {C.GR}{pv}{C.R}")
    print(f"  {C.D}[✓]{C.R} Platform            {C.GR}{platform.system()} "
          f"{platform.machine()}{C.R}")

    # dotnet
    if dotnet_var():
        v = dotnet_surum()
        print(f"  {C.D}[✓]{C.R} .NET SDK            {C.GR}{v}{C.R}")
        print(f"  {C.D}[✓]{C.R} Mod                 {C.GR}FULL (derleme aktif){C.R}")
        cfg.lite_mod = False
    else:
        print(f"  {C.D}[!]{C.R} .NET SDK            {C.YL}YOK → LITE MOD{C.R}")
        cfg.lite_mod = True

    # Node (opsiyonel)
    if node_var():
        print(f"  {C.D}[✓]{C.R} Node.js             {C.GR}var{C.R}")

    # Ollama
    hazir, mesaj = ollama_hazir(cfg)
    if hazir:
        print(f"  {C.D}[✓]{C.R} Ollama              {C.GR}{cfg.ollama_url}{C.R}")
        print(f"  {C.D}[✓]{C.R} Model               {C.GR}{mesaj}{C.R}")
    else:
        print(f"  {C.D}[✗]{C.R} Ollama              {C.RD}{mesaj}{C.R}")
        print(f"\n  {C.CY}Başlat:{C.R} ollama serve")
        print(f"  {C.CY}Model:{C.R}  ollama pull {cfg.model}\n")
        return False

    print(f"  {C.D}[✓]{C.R} Veritabanı          {C.GR}{cfg.db}{C.R}")
    print(f"  {C.D}[✓]{C.R} Üretim sayısı       {C.GR}{store.sayi()}{C.R}")

    if cfg.lite_mod:
        print(f"\n  {C.YL}⚠ LITE MOD: Kod üretilir ama derlenmez.{C.R}")
        print(f"  {C.D}Kodu PC'ye taşıyıp `dotnet build` çalıştır.{C.R}")

    print(f"\n  {C.GR}{C.B}Sistem hazır.{C.R}\n")
    return True


def karsilama(cfg: Config) -> None:
    mod_renk = C.GR if not cfg.lite_mod else C.YL
    mod_ad = "FULL" if not cfg.lite_mod else "LITE"
    print(f"{C.MG}{'═' * 78}{C.R}")
    print(f"{C.B}{C.CY}  🧠  BEBEK DEV v{SURUM} — C# UZMANI{C.R}")
    print(f"{C.MG}{'═' * 78}{C.R}")
    print(f"  {C.D}Model:{C.R} {cfg.model}   "
          f"{C.D}Mod:{C.R} {mod_renk}{mod_ad}{C.R}   "
          f"{C.D}Streaming:{C.R} {'açık' if cfg.streaming else 'kapalı'}")
    print(f"\n{C.WH}{C.B}  Ne yapmak istiyorsun?{C.R}\n")
    if cfg.lite_mod:
        print(f"{C.D}  Akış: Tespit → İskelet → Plan → Kod → Statik → Denetim{C.R}\n")
    else:
        print(f"{C.D}  Akış: Tespit → İskelet → Plan → Kod → BUILD → Düzelt "
              f"→ BUILD → Test → ✓{C.R}\n")
    print(f"{C.D}  Örnekler:{C.R}")
    print(f"    • jwt auth ve ef core sqlite ile todo rest api yaz")
    print(f"    • rabbitmq consumer background service yaz")
    print(f"    • fluentvalidation ile register endpoint'i")
    print(f"    • generic repository pattern ve unit of work")
    print(f"\n{C.D}  Komutlar: {C.CY}/yardim{C.R}  |  Çıkış: {C.RD}/kapat{C.R}\n")


YARDIM = f"""{C.B}{C.CY}KOMUTLAR{C.R}
  {C.CY}/yardim{C.R}              Bu yardım
  {C.CY}/model <ad>{C.R}          Ollama modeli değiştir
  {C.CY}/stream ac|kapa{C.R}      Streaming
  {C.CY}/tur <sayi>{C.R}          Max build turu
  {C.CY}/testtur <sayi>{C.R}      Max test turu
  {C.CY}/test ac|kapa{C.R}        Test üretimi
  {C.CY}/ctx <sayi>{C.R}          Context window
  {C.CY}/kaydet <klasör>{C.R}     Son projeyi kaydet
  {C.CY}/goster <dosya>{C.R}      Son projeden dosya göster
  {C.CY}/gecmis{C.R}              Son üretimler
  {C.CY}/temizle{C.R}             Geçici dosyaları sil
  {C.CY}/kapat{C.R}               Çıkış
"""


def projeyi_goster(p: Proje) -> None:
    print(f"\n{C.MG}{'═' * 78}{C.R}")
    if p.calisan:
        baslik = f"🎯 FİNAL PROJE  {C.GR}(DERLENDİ ✓){C.R}"
    elif dotnet_var():
        baslik = f"🎯 FİNAL PROJE  {C.YL}(derleme başarısız){C.R}"
    else:
        baslik = f"🎯 FİNAL PROJE  {C.YL}(LITE mod){C.R}"
    print(f"{C.B}{C.CY}  {baslik}{C.R}")
    print(f"{C.MG}{'═' * 78}{C.R}")
    print(f"  {C.D}Şablon:{C.R} {p.sablon}   "
          f"{C.D}Proje:{C.R} {p.proje_adi}   "
          f"{C.D}Skor:{C.R} {p.skor}/100")

    b = p.build_gecmis[-1] if p.build_gecmis else None
    if b:
        print(f"  {C.D}Derleme:{C.R} {b.sure:.1f}s, "
              f"{len(b.uyarilar)} uyarı, {len(b.hatalar)} hata")

    test = p.test_gecmis[-1] if p.test_gecmis else None
    if test and test.get("calisti"):
        if test.get("basarili"):
            print(f"  {C.GR}✓ Testler: {test['gecen']} geçti{C.R}")
        else:
            print(f"  {C.RD}✗ Testler: {test['kalan']} kaldı, "
                  f"{test['gecen']} geçti{C.R}")

    # Dosya listesi
    if p.dosyalar:
        print(f"\n  {C.CY}📁 Dosyalar ({len(p.dosyalar)}){C.R}")
        for d in p.dosyalar[:12]:
            satir = str(d["icerik"]).count("\n") + 1
            print(f"     {C.D}• {d['yol']} ({satir} satır){C.R}")

    # Kaydetme önerisi
    if p.calisan:
        print(f"\n  {C.CY}▶ Kaydet:{C.R} /kaydet <klasör>")
        print(f"  {C.CY}▶ Çalıştır:{C.R} cd <klasör>/{p.proje_adi} && dotnet run")
    elif not dotnet_var():
        print(f"\n  {C.YL}⚠ LITE mod: Kodu PC'ye taşı ve `dotnet build` çalıştır.{C.R}")
        print(f"  {C.CY}▶ Kaydet:{C.R} /kaydet <klasör>")

    if p.statik_bulgular:
        print(f"\n  {C.YL}⚠ {len(p.statik_bulgular)} statik bulgu{C.R}")
    print()


# ═══════════════════════════════════════════════════════════════════════════
#  KOMUTLAR
# ═══════════════════════════════════════════════════════════════════════════
def komut(s: str, motor: AkilliMotor, store: Store, cfg: Config,
          son_proje: List[Optional[Proje]]) -> Optional[bool]:
    low = s.strip().lower()
    if low in ("/kapat", "/cik", "exit", "quit"):
        return False
    if low in ("/yardim", "/help", "?", "help"):
        print(YARDIM); return True
    if low == "/gecmis":
        rows = store.son(10)
        if not rows:
            print("(kayıt yok)")
        for r in rows:
            t = ""
            if r["test_gecen"] or r["test_kalan"]:
                t = f"  test:{r['test_gecen']}✓/{r['test_kalan']}✗"
            print(f"  {C.CY}#{r['id']}{C.R} [{r['sablon']}/{r['mod']}] "
                  f"{r['istek'][:45]}  skor:{r['skor']}{t}")
        return True
    if low.startswith("/model "):
        cfg.model = s[7:].strip()
        store.ayar_set("model", cfg.model)
        print(f"{C.GR}✓ Model: {cfg.model}{C.R}")
        h, m = ollama_hazir(cfg)
        if not h: print(f"{C.RD}⚠ {m}{C.R}")
        return True
    if low.startswith("/stream "):
        d = s[8:].strip().lower()
        cfg.streaming = d in ("ac", "aç", "on", "1")
        store.ayar_set("streaming", "1" if cfg.streaming else "0")
        print(f"{C.GR}✓ Streaming: {'AÇIK' if cfg.streaming else 'KAPALI'}{C.R}")
        return True
    if low.startswith("/tur "):
        try:
            cfg.max_build_tur = max(1, min(10, int(s[5:].strip())))
            store.ayar_set("max_build_tur", str(cfg.max_build_tur))
            print(f"{C.GR}✓ Max build turu: {cfg.max_build_tur}{C.R}")
        except ValueError:
            print(f"{C.RD}✗ Geçersiz{C.R}")
        return True
    if low.startswith("/testtur "):
        try:
            cfg.max_test_tur = max(1, min(10, int(s[9:].strip())))
            store.ayar_set("max_test_tur", str(cfg.max_test_tur))
            print(f"{C.GR}✓ Max test turu: {cfg.max_test_tur}{C.R}")
        except ValueError:
            print(f"{C.RD}✗ Geçersiz{C.R}")
        return True
    if low.startswith("/test "):
        d = s[6:].strip().lower()
        cfg.otomatik_test = d in ("ac", "aç", "on", "1")
        store.ayar_set("otomatik_test", "1" if cfg.otomatik_test else "0")
        print(f"{C.GR}✓ Oto test: {'AÇIK' if cfg.otomatik_test else 'KAPALI'}{C.R}")
        return True
    if low.startswith("/ctx "):
        try:
            cfg.num_ctx = max(2048, min(131072, int(s[5:].strip())))
            store.ayar_set("num_ctx", str(cfg.num_ctx))
            print(f"{C.GR}✓ Context: {cfg.num_ctx}{C.R}")
        except ValueError:
            print(f"{C.RD}✗ Geçersiz{C.R}")
        return True
    if low == "/temizle":
        silinen = 0
        try:
            for p in Path(tempfile.gettempdir()).glob("bebek_cs_*"):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    silinen += 1
        except Exception:
            pass
        print(f"{C.GR}✓ {silinen} geçici klasör silindi{C.R}")
        return True
    if low.startswith("/goster"):
        p = son_proje[0]
        if not p or not p.dosyalar:
            print(f"{C.RD}✗ Önce proje üret.{C.R}")
            return True
        parca = s[7:].strip()
        if not parca:
            print(f"{C.CY}Dosyalar:{C.R}")
            for d in p.dosyalar:
                print(f"  • {d['yol']}")
            return True
        esles = [d for d in p.dosyalar if parca.lower() in d["yol"].lower()]
        if not esles:
            print(f"{C.RD}✗ Bulunamadı: {parca}{C.R}")
            return True
        for d in esles[:1]:
            print(f"\n{C.MG}{'─' * 78}{C.R}")
            print(f"{C.B}{C.CY}{d['yol']}{C.R}")
            print(f"{C.MG}{'─' * 78}{C.R}")
            print(f"{C.WH}{d['icerik']}{C.R}")
            print(f"{C.MG}{'─' * 78}{C.R}\n")
        return True
    if low.startswith("/kaydet"):
        p = son_proje[0]
        if not p or not p.dosyalar:
            print(f"{C.RD}✗ Önce proje üret.{C.R}")
            return True
        parca = s[7:].strip()
        if not parca:
            try:
                parca = input(f"  {C.CY}Hedef klasör: {C.R}").strip() or "./cikti"
            except (EOFError, KeyboardInterrupt):
                return True
        hedef = Path(parca).expanduser().resolve()
        hedef.mkdir(parents=True, exist_ok=True)
        atla = {"bin", "obj", ".vs", ".vscode"}
        n = 0
        for src in p.kok.rglob("*"):
            if not src.is_file():
                continue
            if any(part in atla for part in src.parts):
                continue
            rel = src.relative_to(p.kok)
            dst = hedef / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                n += 1
            except Exception:
                pass
        print(f"{C.GR}✓ {n} dosya kopyalandı: {hedef}{C.R}")
        if dotnet_var():
            print(f"  {C.CY}▶ Çalıştır:{C.R} cd {hedef / p.proje_adi} && dotnet run")
        else:
            print(f"  {C.CY}▶ PC'ye taşı ve:{C.R} cd {hedef / p.proje_adi} && dotnet build")
        return True
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  ANA
# ═══════════════════════════════════════════════════════════════════════════
def main() -> int:
    global _sinyal_izin
    signal.signal(signal.SIGINT, _sinyal_handler)
    try:
        signal.signal(signal.SIGTERM, _sinyal_handler)
    except (AttributeError, ValueError):
        pass

    EV_DIZINI.mkdir(exist_ok=True)
    cfg = Config()
    store = Store(cfg.db)

    # Ayarları yükle
    cfg.model = store.ayar_get("model", cfg.model)
    cfg.streaming = store.ayar_get("streaming", "1") == "1"
    cfg.otomatik_test = store.ayar_get("otomatik_test", "1") == "1"
    try:
        cfg.max_build_tur = int(store.ayar_get("max_build_tur", str(cfg.max_build_tur)))
    except ValueError:
        pass
    try:
        cfg.max_test_tur = int(store.ayar_get("max_test_tur", str(cfg.max_test_tur)))
    except ValueError:
        pass
    try:
        cfg.num_ctx = int(store.ayar_get("num_ctx", str(cfg.num_ctx)))
    except ValueError:
        pass

    if not bios(cfg, store):
        store.kapat()
        return 1

    karsilama(cfg)
    motor = AkilliMotor(store, cfg)
    son_proje: List[Optional[Proje]] = [None]

    while True:
        try:
            s = input(f"{C.B}{C.GR}cs>{C.R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YL}Çıkmak için /kapat{C.R}")
            continue
        if not s:
            continue

        try:
            r = komut(s, motor, store, cfg, son_proje)
        except Exception as e:
            print(f"{C.RD}✗ Komut hatası: {e}{C.R}")
            _log(f"komut hatası: {e}")
            continue
        if r is False:
            break
        if r is True:
            continue

        # Üretim
        try:
            p = motor.uret(s)
            if p:
                son_proje[0] = p
                projeyi_goster(p)
                kod_str = "\n\n".join(
                    f"// === {d['yol']} ===\n{d['icerik']}"
                    for d in p.dosyalar)
                test = p.test_gecmis[-1] if p.test_gecmis else {}
                mod = "full" if p.calisan else ("lite" if not dotnet_var() else "fail")
                store.uretim_ekle(
                    s, p.sablon, p.proje_adi, kod_str, p.skor,
                    test.get("gecen", 0), test.get("kalan", 0), mod)
                if p.kok and p.kok.exists():
                    print(f"{C.D}💡 Geçici proje: {p.kok}{C.R}")
                    print(f"{C.D}   Kalıcı kaydet: /kaydet <klasör>{C.R}\n")
        except Exception as e:
            print(f"{C.RD}✗ Üretim hatası: {e}{C.R}")
            _log(f"üretim hatası: {e}")

    _sinyal_izin = True
    print(f"\n{C.CY}Kapatılıyor...{C.R}")
    store.kapat()
    print(f"{C.GR}✓ Görüşürüz.{C.R}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
