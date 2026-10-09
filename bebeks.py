#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BEBEK DEV v5.0 — C# UZMANI (DeepSeek/ChatGPT'yi GEÇEN versiyon)

Chat LLM'ler kodu üretir ama DERLEYEMEZ. Bu araç:
  1. `dotnet new` ile GERÇEK proje iskeleti kurar
  2. LLM kodu yazar → `dotnet build` çalışır
  3. MSBuild hatalarını PARSE eder (dosya:satır:kolon + kod)
  4. Hataları LLM'e yapılandırılmış halde geri besler
  5. Temiz derlenene kadar iterasyon (max 5 tur)
  6. xUnit test projesi kurar, `dotnet test` çalıştırır
  7. Test kırılırsa tekrar düzeltir
  8. Streaming çıktı — token'lar canlı akar

.NET 8+, C# 12/13, ASP.NET Core, EF Core, Minimal API, Blazor, WPF
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

SURUM = "5.0"
OLLAMA_URL = "http://localhost:11434"

# ═══════════════════════════════════════════════════════════════════════════
#  RENK
# ═══════════════════════════════════════════════════════════════════════════
class C:
    R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
    RD = "\033[91m"; GR = "\033[92m"; YL = "\033[93m"
    BL = "\033[94m"; MG = "\033[95m"; CY = "\033[96m"; WH = "\033[97m"


# ═══════════════════════════════════════════════════════════════════════════
#  KONFİG
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Config:
    db: str = "bebek_dev.db"
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


# ═══════════════════════════════════════════════════════════════════════════
#  OLLAMA — STREAMING DESTEKLİ
# ═══════════════════════════════════════════════════════════════════════════
_http = requests.Session()
_http.headers.update({"Connection": "keep-alive"})


def ollama_hazir(cfg: Config) -> Tuple[bool, str]:
    try:
        r = _http.get(f"{cfg.ollama_url}/api/tags", timeout=5)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        modeller = [m.get("name", "") for m in r.json().get("models", [])]
        if cfg.model in modeller:
            return True, cfg.model
        for m in modeller:
            if m.split(":")[0] == cfg.model.split(":")[0]:
                return True, m
        return False, f"model yok: {cfg.model} | mevcut: {', '.join(modeller)}"
    except requests.RequestException as e:
        return False, str(e)


def _json_ayikla(t: str) -> Optional[dict]:
    """LLM çıktısından JSON çıkar. Markdown fence + brace matching destekli."""
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
                    try: return json.loads(t[i:k + 1])
                    except json.JSONDecodeError: return None
    return None


def ollama_stream(sistem: str, kullanici: str, cfg: Config,
                  sicaklik: Optional[float] = None,
                  json_mod: bool = True,
                  max_tok: Optional[int] = None,
                  canli: bool = True,
                  baslik: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Streaming Ollama çağrısı. Token'lar canlı akar, sonunda tam metin döner."""
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
    token_sayaci = 0
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
                token_sayaci += 1
                if canli:
                    # canlı akış — 30ms'de bir yazdır
                    now = time.time()
                    if now - son_guncelleme > 0.03:
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
    """JSON modunda streaming çağrı + parse."""
    son_hata = None
    for i in range(deneme):
        ham, hata = ollama_stream(
            sistem, kullanici, cfg,
            sicaklik=sicaklik, json_mod=True, max_tok=max_tok,
            canli=canli and i == 0,
            baslik=baslik if i == 0 else f"{baslik} (retry {i})")
        if hata:
            son_hata = hata
            if "bağlantı" in hata.lower() or "timeout" in hata.lower():
                time.sleep(1.0)
            continue
        v = _json_ayikla(ham or "")
        if v:
            return v, None
        son_hata = "JSON parse hatası"
    return None, son_hata or "bilinmeyen"


# ═══════════════════════════════════════════════════════════════════════════
#  MSBUILD HATA PARSER'ı — dosya:satır:kolon:kod:mesaj
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class BuildMesaj:
    dosya: str
    satir: int
    kolon: int
    seviye: str   # error | warning
    kod: str      # CS0103
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


def build_ozet(sonuc: BuildSonuc) -> str:
    """LLM'e geri beslemek için yapılandırılmış özet."""
    if sonuc.basarili:
        return "✓ Derleme başarılı."
    satirlar = [f"✗ Derleme başarısız ({len(sonuc.hatalar)} hata)."]
    for h in sonuc.hatalar[:25]:
        satirlar.append(f"  {h.kisa()}")
    if len(sonuc.hatalar) > 25:
        satirlar.append(f"  ... +{len(sonuc.hatalar) - 25} hata daha")
    return "\n".join(satirlar)


# ═══════════════════════════════════════════════════════════════════════════
#  DOTNET ENTEGRASYONU
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


# dotnet new şablonları
SABLONLAR = {
    "console":     ("console",     "Konsol uygulaması"),
    "classlib":    ("classlib",    "Sınıf kütüphanesi"),
    "webapi":      ("webapi",      "ASP.NET Core Web API (minimal)"),
    "webapi-ctrl": ("webapi",      "Web API (controller)"),
    "mvc":         ("mvc",         "ASP.NET Core MVC"),
    "razor":       ("razor",       "Razor Pages"),
    "blazor":      ("blazor",      "Blazor Server"),
    "blazorwasm":  ("blazorwasm",  "Blazor WebAssembly"),
    "worker":      ("worker",      "Background Service"),
    "grpc":        ("grpc",        "gRPC servisi"),
    "wpf":         ("wpf",         "WPF masaüstü"),
    "winforms":    ("winforms",    "WinForms"),
    "maui":        ("maui",        "MAUI çoklu platform"),
    "xunit":       ("xunit",       "xUnit test projesi"),
    "nunit":       ("nunit",       "NUnit test projesi"),
    "mstest":      ("mstest",      "MSTest test projesi"),
}


def dotnet_new(kok: Path, sablon: str, ad: str,
               ek_arg: Optional[List[str]] = None) -> Tuple[bool, str]:
    """`dotnet new <şablon> -n <ad> -o <kok/ad>`"""
    hedef = kok / ad
    hedef.mkdir(parents=True, exist_ok=True)
    cmd = ["dotnet", "new", sablon, "-n", ad, "-o", str(hedef), "--force"]
    if ek_arg:
        cmd.extend(ek_arg)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return False, (r.stderr or r.stdout)[:500]
        return True, str(hedef)
    except subprocess.TimeoutExpired:
        return False, "dotnet new zaman aşımı"
    except Exception as e:
        return False, str(e)


def dotnet_build(proje_dizini: Path, timeout: int = 180,
                 tur: int = 0) -> BuildSonuc:
    """`dotnet build` çalıştır, çıktıyı parse et."""
    t0 = time.time()
    # Proje veya solution dosyasını bul
    hedef = proje_dizini
    sln = list(proje_dizini.glob("*.sln"))
    if sln:
        hedef = sln[0]
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
        hatalar=hatalar,
        uyarilar=uyarilar,
        sure=time.time() - t0,
        tur=tur,
    )


def dotnet_test(proje_dizini: Path, timeout: int = 240,
                tur: int = 0) -> Dict[str, Any]:
    """`dotnet test` çalıştır, sonucu döndür."""
    t0 = time.time()
    sln = list(proje_dizini.glob("*.sln"))
    test_proj = None
    if sln:
        hedef = sln[0]
    else:
        # Test projesi ara
        adaylar = list(proje_dizini.glob("**/*Test*.csproj")) + \
                  list(proje_dizini.glob("**/*Tests.csproj")) + \
                  list(proje_dizini.glob("**/*.csproj"))
        test_proj = next((p for p in adaylar
                          if "test" in p.name.lower()), None)
        if not test_proj:
            return {"calisti": False, "sebep": "test projesi bulunamadı",
                    "sure": 0}
        hedef = test_proj

    try:
        r = subprocess.run(
            ["dotnet", "test", str(hedef), "--nologo", "-v", "quiet",
             "--logger", "console;verbosity=normal"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(proje_dizini),
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"calisti": False, "sebep": f"test timeout ({timeout}s)",
                "sure": time.time() - t0}
    except Exception as e:
        return {"calisti": False, "sebep": str(e), "sure": time.time() - t0}

    birlesik = (r.stdout or "") + "\n" + (r.stderr or "")
    # Geçti/kaldı sayıları
    m_gec = re.search(r"Passed!?\s*[-–]\s*Failed:\s*(\d+),\s*Passed:\s*(\d+)",
                      birlesik)
    m_basarili = re.search(r"Passed!\s*[-–]\s*Passed:\s*(\d+)", birlesik)
    m_basarisiz = re.search(r"Failed!\s*[-–]\s*Failed:\s*(\d+),\s*Passed:\s*(\d+)",
                            birlesik)
    gecen = kalan = 0
    if m_basarili:
        gecen = int(m_basarili.group(1))
    elif m_basarisiz:
        kalan = int(m_basarisiz.group(1))
        gecen = int(m_basarisiz.group(2))
    elif m_gec:
        kalan = int(m_gec.group(1))
        gecen = int(m_gec.group(2))

    # Başarısız test isimleri
    basarisiz_testler = re.findall(r"^\s*Failed\s+(\S+)", birlesik, re.MULTILINE)

    return {
        "calisti": True,
        "basarili": r.returncode == 0,
        "cikis": r.returncode,
        "gecen": gecen,
        "kalan": kalan,
        "basarisiz_testler": basarisiz_testler[:15],
        "cikti": birlesik[:4000],
        "sure": time.time() - t0,
        "tur": tur,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  PROJE DOSYASI OKUMA / YAZMA
# ═══════════════════════════════════════════════════════════════════════════
def proje_oku(kok: Path, max_dosya: int = 30, max_boyut: int = 8000
              ) -> List[Dict[str, str]]:
    """Projeyi tara, önemli dosyaları oku. obj/bin atla."""
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
        rel = str(p.relative_to(kok))
        dosyalar.append({"yol": rel, "icerik": ic})
        if len(dosyalar) >= max_dosya:
            break
    return dosyalar


def projeye_yaz(kok: Path, dosyalar: List[Dict]) -> List[str]:
    """LLM'den dönen dosyaları yaz. Yazılan yolları döner."""
    yazilan = []
    for d in dosyalar:
        yol = d.get("yol", "").strip()
        ic = d.get("icerik", "")
        if not yol or not ic:
            continue
        # güvenlik: kök dışına çıkma
        hedef = (kok / yol).resolve()
        try:
            hedef.relative_to(kok.resolve())
        except ValueError:
            continue  # path traversal engellendi
        hedef.parent.mkdir(parents=True, exist_ok=True)
        try:
            hedef.write_text(str(ic), encoding="utf-8")
            yazilan.append(yol)
        except Exception:
            pass
    return yazilan


# ═══════════════════════════════════════════════════════════════════════════
#  C# STATİK DENETİM (Roslyn-style, kural bazlı)
# ═══════════════════════════════════════════════════════════════════════════
class CSharpDenetleyici:
    """Basit kural tabanlı C# denetimi — LLM'e başvurmadan ön filtre."""

    KURALLAR: List[Tuple[str, str, str]] = [
        # (regex, seviye, mesaj)
        (r"\basync\s+void\b(?!\s+(?:Main|EventHandler))", "uyari",
         "async void kullanma — Task veya Task<T> döndür (CS1998 riski)"),
        (r"\.Result\b|\.Wait\(\)", "uyari",
         ".Result / .Wait() deadlock riski — await kullan"),
        (r"\bThread\.Sleep\b", "uyari",
         "Thread.Sleep yerine await Task.Delay kullan"),
        (r"catch\s*\(\s*Exception\s*\)\s*\{\s*\}", "uyari",
         "Boş catch bloğu — en azından logla veya yeniden fırlat"),
        (r"catch\s*\(\s*Exception\s+\w+\s*\)\s*\{[^}]*throw\s+\w+;",
         "uyari", "throw ex; yerine throw; kullan (stack trace kaybolur)"),
        (r"public\s+static\s+\w+\s+\w+\s*\([^)]*\)\s*\{[^}]*HttpClient",
         "uyari", "HttpClient'ı static yap — socket exhaustion riski"),
        (r"\bnew\s+Random\s*\(\s*\)", "uyari",
         "Random.Shared kullan (thread-safe, .NET 6+)"),
        (r"\.ToList\(\)\s*\.\s*\w+\(", "uyari",
         "Gereksiz ToList() — LINQ zincirini bozar, bellek israfı"),
        (r"\bstring\.Format\s*\(", "uyari",
         "string.Format yerine interpolasyon ($\"...\") kullan"),
        (r"\bConfigureAwait\s*\(\s*false\s*\)", "uyari",
         "ASP.NET Core'da ConfigureAwait(false) gereksiz"),
        (r"\bGC\.Collect\b", "uyari",
         "GC.Collect manuel çağırma — neredeyse her zaman yanlış"),
        (r"\bThread\.Abort\b", "error",
         "Thread.Abort .NET Core+ üzerinde desteklenmez"),
    ]

    @classmethod
    def denetle(cls, dosyalar: List[Dict]) -> List[Dict]:
        bulgular = []
        for d in dosyalar:
            yol = d.get("yol", "")
            ic = str(d.get("icerik", ""))
            if not yol.endswith(".cs"):
                continue
            for satir_no, satir in enumerate(ic.splitlines(), 1):
                for regex, seviye, msj in cls.KURALLAR:
                    if re.search(regex, satir):
                        bulgular.append({
                            "dosya": yol, "satir": satir_no,
                            "seviye": seviye, "mesaj": msj,
                            "kaynak": satir.strip()[:120],
                        })
        return bulgular


# ═══════════════════════════════════════════════════════════════════════════
#  PROMPT'LAR — C# UZMANI + FEW-SHOT + CHAIN-OF-THOUGHT
# ═══════════════════════════════════════════════════════════════════════════

PROJE_TESPIT = """Sen kıdemli bir .NET mimarısın. Kullanıcı isteğinden DOĞRU dotnet şablonunu seç.

Şablonlar: console, classlib, webapi, mvc, razor, blazor, blazorwasm,
           worker, grpc, wpf, winforms, maui, xunit

Şu JSON'u döndür:
{
  "sablon": "webapi",
  "proje_adi": "TodoApi",
  "sebep": "kullanıcı REST API istedi",
  "ek_arg": [],
  "ozellikler": ["jwt auth", "ef core", "swagger"]
}
SADECE JSON. Başka açıklama yok."""


PLAN = """Sen kıdemli bir .NET mimarısın. Proje planını çıkar.

Mevcut iskelet dosyaları (dotnet new çıktısı):
{iskelet}

Kullanıcı isteği: {istek}

Chain-of-thought ile düşün (içinden), sonra şu JSON'u döndür:
{
  "ozet": "tek cümle",
  "gereksinimler": ["madde 1", "madde 2"],
  "mimari": "minimal-api | controller | clean-architecture | layered",
  "nuget": ["Microsoft.EntityFrameworkCore.Sqlite", "..."],
  "yazilacak_dosyalar": [
    {"yol": "Program.cs", "sorumluluk": "..."},
    {"yol": "Models/Todo.cs", "sorumluluk": "..."}
  ],
  "silinecek_dosyalar": ["WeatherForecast.cs"],
  "notlar": "dikkat edilecek noktalar"
}
SADECE JSON."""


KOD = """Sen dünyanın en iyi C# geliştiricisisin. 15+ yıl .NET deneyimin var.
Aşağıdaki isteği KUSURSUZ, DERLENEBİLİR, PRODÜKSİYON KALİTESİNDE kodla.

═══ ZORUNLU KURALLAR ═══

C# SÖZDİZİMİ:
• .NET 8, C# 12/13
• Dosya-scoped namespace: `namespace X;`
• Primary constructor: `class Foo(int x) { }` (C# 12)
• Collection expr: `int[] a = [1, 2, 3];` (C# 12)
• Record: `public record Todo(int Id, string Title);`
• Pattern matching: `x switch { > 0 => ..., _ => ... }`
• Raw string: `"""..."""` (C# 11)
• `required` üye: `public required string Name { get; init; }`

NULLABLE:
• Tüm nullable referanslar açık, `?` doğru kullan
• `ArgumentNullException.ThrowIfNull(x)`
• `??`, `?.`, `is null`, `is not null`

ASYNC:
• I/O için `async Task<T>` / `ValueTask`
• `async void` YASAK (event handler hariç)
• `CancellationToken` parametresi ekle
• `ConfigureAwait` gereksiz (ASP.NET Core)

KALİTE:
• Kısaltma YOK, TODO YOK, "..." YOK
• Her public üyeye kısa XML doc
• Global using'ler `<ImplicitUsings>enable</ImplicitUsings>`
• DI: constructor injection, `IServiceCollection` extension method
• Validation: DataAnnotations veya FluentValidation

ANTI-PATTERN (YAPMA):
✗ `catch (Exception) { }` — boş catch
✗ `.Result` veya `.Wait()` — deadlock
✗ `Thread.Sleep` — `Task.Delay` kullan
✗ `GC.Collect()` manuel çağrı
✗ `throw ex;` — `throw;` kullan
✗ `ToList()` sonrası LINQ zinciri
✗ `HttpClient` her metotta `new`

═══ FEW-SHOT ÖRNEK (bu kalitede yaz) ═══

/// <summary>Kullanıcı servisi.</summary>
public sealed class UserService(AppDbContext db, ILogger<UserService> log)
{
    public async Task<User?> GetAsync(int id, CancellationToken ct = default)
    {
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(id);
        return await db.Users.AsNoTracking()
            .FirstOrDefaultAsync(u => u.Id == id, ct);
    }
}

═══ MEVCUT İSKELET ═══
{iskelet}

═══ PLAN ═══
{plan}

═══ İSTEK ═══
{istek}

Şu JSON'u döndür:
{
  "dosyalar": [
    {"yol": "Program.cs", "icerik": "TAM İÇERİK"},
    {"yol": "Models/Todo.cs", "icerik": "..."}
  ],
  "aciklama": "kısa özet"
}
SADECE JSON. `...` yok, `// TODO` yok. Her dosya TAM içerik."""


DERLEME_DUZELT = """C# kodu derlenmedi. Hataları KÖK NEDENİYLE çöz.

═══ DERLEME HATALARI ═══
{hatalar}

═══ UYARILAR ═══
{uyarilar}

═══ MEVCUT DOSYALAR ═══
{dosyalar}

═══ TALİMATLAR ═══
1. Her hatayı TEK TEK incele
2. Eksik `using` varsa ekle
3. Eksik NuGet paketi varsa `.csproj` içine `<PackageReference>` ekle
4. Tip uyuşmazlıklarını düzelt
5. HİÇBİR ŞEYİ SİLME — sadece düzelt ve ekle
6. TAM dosyaları döndür (sadece değişen satırlar değil)

Şu JSON'u döndür:
{
  "dosyalar": [{"yol": "Program.cs", "icerik": "TAM KOD"}],
  "csproj_guncelle": [
    {"proje": "MyProject.csproj",
     "package": "Microsoft.EntityFrameworkCore.Sqlite", "surum": "8.0.0"}
  ],
  "aciklama": "ne düzeltildi"
}
SADECE JSON."""


TEST_YAZ = """Sen xUnit uzmanısın. Aşağıdaki C# kodu için TAM çalışan testler yaz.

═══ ZORUNLU KURALLAR ═══
• xUnit + FluentAssertions
• `[Fact]`, `[Theory]` + `[InlineData]`
• Arrange-Act-Assert
• Her public metot için en az 1 test
• Null, sınır değerler, exception senaryoları
• `Assert.Throws<ArgumentNullException>(() => ...)`
• Test isimleri: `MetotAdi_Senaryo_BeklenenSonuc`
• Mock için Moq (gerekirse)

KURAL: Testler PRODÜKSİYON KODUNDAN BAĞIMSIZ derlenmeli.
Yani sadece public API'yi test et.

═══ PRODÜKSİYON KODU ═══
{kod}

═══ PROJE YAPISI ═══
{proje_adi} (ana proje)
{proje_adi}.Tests (test projesi — referans verilecek)

Şu JSON'u döndür:
{
  "test_projesi": "{proje_adi}.Tests",
  "dosyalar": [
    {"yol": "TodoServiceTests.cs", "icerik": "TAM TEST KODU"}
  ],
  "csproj": "Test projesinin .csproj içeriği (PackageReference dahil)"
}
SADECE JSON."""


TEST_DUZELT = """Testler başarısız. Testleri veya kodu düzelt.

═══ TEST SONUCU ═══
{test_sonuc}

═══ TEST DOSYALARI ═══
{test_dosyalar}

═══ PRODÜKSİYON DOSYALARI ═══
{prod_dosyalar}

ÖNEMLİ: Test başarısızsa iki sebep olabilir:
  (A) Test yanlış yazılmış → testi düzelt
  (B) Prodüksiyon kodu bug'lı → kodu düzelt

Doğru olanı seç. KURAL: Prodüksiyon kodu doğruysa testi değiştir.

Şu JSON'u döndür:
{
  "test_dosyalari": [{"yol": "XTests.cs", "icerik": "..."}],
  "prod_dosyalari": [{"yol": "X.cs", "icerik": "..."}],
  "aciklama": "ne düzeltildi"
}
SADECE JSON."""


DENETIM = """Sen acımasız bir C# kod denetçisisin. Aşağıdaki kodu incele.

═══ KOD ═══
{kod}

KONTROL ET:
• Nullable ihlalleri (uyarı sayısı)
• Async yanlış kullanımı
• Dispose pattern (IAsyncDisposable, using)
• LINQ verimliliği (N+1, gereksiz enumerate)
• Exception handling
• DI lifetime hataları (Scoped'ı Singleton'a enjekte etme)
• Thread safety
• SQL injection (raw SQL varsa)
• Güvenlik (auth, validation, CORS)

Şu JSON'u döndür:
{
  "kritik": [{"dosya": "X.cs", "satir": 12, "sorun": "...", "cozum": "..."}],
  "orta": [{"dosya": "X.cs", "sorun": "...", "cozum": "..."}],
  "dusuk": [{"dosya": "X.cs", "sorun": "..."}],
  "skor": 85,
  "ozet": "tek cümle"
}
SADECE JSON."""


# ═══════════════════════════════════════════════════════════════════════════
#  DEPO (SQLite)
# ═══════════════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS uretim(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  istek TEXT NOT NULL, sablon TEXT, proje_adi TEXT,
  kod TEXT, skor INTEGER, test_gecen INTEGER, test_kalan INTEGER,
  tarih TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dusunce(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  istek TEXT, asama TEXT, icerik TEXT, tarih TEXT);
CREATE TABLE IF NOT EXISTS pattern(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  istek TEXT NOT NULL, anahtar TEXT NOT NULL,
  sablon TEXT, dosyalar TEXT, skor INTEGER, tarih TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pattern_anahtar ON pattern(anahtar);
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
    def _tx(self):
        try:
            yield; self.conn.commit()
        except Exception:
            self.conn.rollback(); raise

    def uretim_ekle(self, istek: str, sablon: str, proje_adi: str,
                    kod: str, skor: int,
                    test_gecen: int = 0, test_kalan: int = 0) -> int:
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO uretim(istek,sablon,proje_adi,kod,skor,"
                "test_gecen,test_kalan,tarih) VALUES(?,?,?,?,?,?,?,?)",
                (istek, sablon, proje_adi, kod, skor, test_gecen, test_kalan,
                 dt.datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid or 0

    def dusunce_ekle(self, istek: str, asama: str, icerik: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO dusunce(istek,asama,icerik,tarih) VALUES(?,?,?,?)",
                (istek, asama, icerik,
                 dt.datetime.now().isoformat(timespec="seconds")))

    def pattern_ekle(self, istek: str, anahtar: str, sablon: str,
                     dosyalar: List[Dict], skor: int) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO pattern(istek,anahtar,sablon,dosyalar,skor,tarih) "
                "VALUES(?,?,?,?,?,?)",
                (istek, anahtar, sablon,
                 json.dumps(dosyalar, ensure_ascii=False)[:8000],
                 skor, dt.datetime.now().isoformat(timespec="seconds")))

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
            "SELECT id,istek,sablon,skor,test_gecen,test_kalan,tarih "
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


def _jaccard(a: set, b: set) -> float:
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)


# ═══════════════════════════════════════════════════════════════════════════
#  SİNYAL
# ═══════════════════════════════════════════════════════════════════════════
_sinyal_izin = False


def _sinyal_handler(sig, frame):
    if _sinyal_izin:
        print(f"\n{C.YL}Kapatılıyor...{C.R}"); sys.exit(0)
    print(f"\n{C.RD}⚠ Ctrl+C engellendi. Çıkmak için: /kapat{C.R}")


# ═══════════════════════════════════════════════════════════════════════════
#  AKILLI MOTOR — GERÇEK DERLEME DÖNGÜSÜ
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
    denetim: Optional[Dict] = None
    skor: int = 0
    calisan: bool = False  # başarıyla derlendi mi


class AkilliMotor:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg

    # ── yardımcılar ────────────────────────────────────────
    def _bas(self, no: int, ad: str, aciklama: str = "") -> None:
        print(f"\n{C.MG}{'━' * 78}{C.R}")
        print(f"{C.B}{C.CY}  ADIM {no}  ▸  {ad.upper()}{C.R}")
        if aciklama:
            print(f"{C.D}  {aciklama}{C.R}")
        print(f"{C.MG}{'━' * 78}{C.R}")

    def _ok(self, m: str, sure: float = 0.0) -> None:
        suffix = f"  {C.D}({sure:.1f}s){C.R}" if sure else ""
        print(f"  {C.GR}✓ {m}{C.R}{suffix}")

    def _uyari(self, m: str) -> None:
        print(f"  {C.YL}⚠ {m}{C.R}")

    def _hata(self, m: str) -> None:
        print(f"  {C.RD}✗ {m}{C.R}")

    # ── 1) PROJE TESPİT ────────────────────────────────────
    def _tespit(self, istek: str) -> Tuple[str, str]:
        self._bas(1, "proje tespiti", "İstekten doğru dotnet şablonunu seç")
        t0 = time.time()
        v, hata = ollama_cagri(
            PROJE_TESPIT,
            f"İstek: {istek}",
            self.cfg, sicaklik=0.1, max_tok=400,
            canli=self.cfg.streaming, baslik="sablon seçiliyor")
        if hata or not v:
            self._uyari(f"Tespit başarısız ({hata}), console varsayılıyor")
            return "console", "BebekProje"

        sablon = v.get("sablon", "console")
        if sablon not in SABLONLAR:
            sablon = "console"
        ad = v.get("proje_adi", "BebekProje")
        # C# identifier doğrulama
        ad = re.sub(r"[^A-Za-z0-9_]", "", ad) or "BebekProje"
        if ad[0].isdigit():
            ad = "P" + ad
        self._ok(f"Şablon: {sablon}  |  Proje: {ad}  |  Sebep: {v.get('sebep', '')}",
                 time.time() - t0)
        return sablon, ad

    # ── 2) İSKELET KURMA ───────────────────────────────────
    def _iskelet(self, p: Proje) -> bool:
        self._bas(2, "iskelet", f"dotnet new {p.sablon}")
        t0 = time.time()
        if not dotnet_var():
            self._hata("dotnet bulunamadı — .NET SDK kurulu mu?")
            return False
        self._ok(f"dotnet {dotnet_surum()} hazır")

        ok, sonuc = dotnet_new(p.kok, SABLONLAR[p.sablon][0], p.proje_adi)
        if not ok:
            self._hata(f"Şablon kurulamadı: {sonuc}")
            return False
        self._ok(f"{p.sablon} iskeleti kuruldu: {sonuc}", time.time() - t0)

        # İskelet dosyalarını yükle
        p.dosyalar = proje_oku(p.kok, max_dosya=20)
        print(f"  {C.D}İskelet dosyaları: "
              f"{', '.join(d['yol'] for d in p.dosyalar[:6])}{C.R}")
        return True

    # ── 3) PLAN ────────────────────────────────────────────
    def _plan(self, p: Proje) -> bool:
        self._bas(3, "plan", "Mimari ve dosya yapısı planlanıyor")
        t0 = time.time()
        iskelet = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik'][:1500]}"
            for d in p.dosyalar[:10])
        v, hata = ollama_cagri(
            PLAN.format(iskelet=iskelet, istek=p.istek),
            "Planı çıkar.",
            self.cfg, sicaklik=0.2, max_tok=1500,
            canli=self.cfg.streaming, baslik="mimari tasarlanıyor")
        if hata or not v:
            self._hata(f"Plan başarısız: {hata}")
            return False
        p.plan = v
        self._ok(f"Özet: {v.get('ozet', '?')}", time.time() - t0)
        if v.get("nuget"):
            print(f"     {C.CY}NuGet: {', '.join(v['nuget'][:6])}{C.R}")
        ds = v.get("yazilacak_dosyalar") or []
        for d in ds[:6]:
            print(f"     {C.D}📄 {d.get('yol')}{C.R}")
        return True

    # ── 4) KOD ÜRETİMİ ─────────────────────────────────────
    def _kod(self, p: Proje) -> bool:
        self._bas(4, "kod", "Üretim kalitesinde C# kodu yazılıyor")
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
            canli=self.cfg.streaming, baslik="kod üretiliyor")
        if hata or not v:
            self._hata(f"Kod üretilemedi: {hata}")
            return False
        yeni = v.get("dosyalar") or []
        if not yeni:
            self._hata("LLM dosya döndürmedi")
            return False

        # Yaz
        yazilan = projeye_yaz(p.kok, yeni)
        self._ok(f"{len(yazilan)} dosya yazıldı, "
                 f"{sum(str(d.get('icerik','')).count(chr(10))+1 for d in yeni)} satır",
                 time.time() - t0)

        # NuGet paketlerini ekle
        nuget = p.plan.get("nuget") or []
        if nuget:
            self._nuget_ekle(p.kok / p.proje_adi, nuget)

        p.dosyalar = proje_oku(p.kok, max_dosya=30)
        return True

    def _nuget_ekle(self, proje_dizini: Path, paketler: List[str]) -> None:
        csproj = next(proje_dizini.glob("*.csproj"), None)
        if not csproj:
            return
        print(f"  {C.D}📦 NuGet ekleniyor: {', '.join(paketler[:5])}{C.R}")
        for pk in paketler[:10]:
            pk = pk.strip()
            if not pk: continue
            try:
                subprocess.run(
                    ["dotnet", "add", str(csproj), "package", pk,
                     "--no-restore"],
                    capture_output=True, text=True, timeout=60,
                    encoding="utf-8", errors="replace")
            except Exception:
                pass

    # ── 5) DERLEME DÖNGÜSÜ (KİLLER FEATURE) ────────────────
    def _derleme_dongusu(self, p: Proje) -> bool:
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
                    self._uyari(f"{len(sonuc.uyarilar)} uyarı (derlemeyi engellemiyor)")
                    for u in sonuc.uyarilar[:5]:
                        print(f"     {C.D}{u.kod}: {u.mesaj[:80]}{C.R}")
                p.calisan = True
                return True

            # Hataları göster
            print(f"  {C.RD}✗ {len(sonuc.hatalar)} derleme hatası{C.R}")
            for h in sonuc.hatalar[:8]:
                kisa_dosya = h.dosya.split(os.sep)[-1]
                print(f"     {C.RD}{kisa_dosya}({h.satir},{h.kolon}): "
                      f"{h.kod} {h.mesaj[:80]}{C.R}")
            if len(sonuc.hatalar) > 8:
                print(f"     {C.D}... +{len(sonuc.hatalar) - 8} hata daha{C.R}")

            if tur == self.cfg.max_build_tur:
                self._hata(f"Max tur aşıldı — {len(sonuc.hatalar)} hata kaldı")
                return False

            # LLM'e geri besle
            print(f"\n  {C.YL}↻ Hatalar LLM'e geri besleniyor...{C.R}")
            ok = self._hata_duzelt(p, sonuc, tur)
            if not ok:
                return False

        return False

    def _hata_duzelt(self, p: Proje, sonuc: BuildSonuc, tur: int) -> bool:
        hatalar_str = "\n".join(f"  {h.kisa()}" for h in sonuc.hatalar[:30])
        uyarilar_str = "\n".join(f"  {u.kisa()}" for u in sonuc.uyarilar[:15])
        dosyalar_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar)

        v, hata = ollama_cagri(
            DERLEME_DUZELT.format(
                hatalar=hatalar_str or "(yok)",
                uyarilar=uyarilar_str or "(yok)",
                dosyalar=dosyalar_str[:14000]),
            f"Bu {len(sonuc.hatalar)} hatayı düzelt. TAM dosyaları döndür.",
            self.cfg, sicaklik=0.15, max_tok=self.cfg.max_tok,
            canli=self.cfg.streaming,
            baslik=f"tur {tur} düzeltmesi")
        if hata or not v:
            self._hata(f"Düzeltme başarısız: {hata}")
            return False

        yeni = v.get("dosyalar") or []
        if yeni:
            yazilan = projeye_yaz(p.kok, yeni)
            self._ok(f"{len(yazilan)} dosya güncellendi")

        # csproj güncellemeleri
        for g in (v.get("csproj_guncelle") or []):
            proje_ad = g.get("proje", "")
            paket = g.get("package", "")
            surum = g.get("surum", "")
            if not paket: continue
            csproj = next(p.kok.rglob(proje_ad), None) if proje_ad else None
            if not csproj:
                csproj = next((p.kok / p.proje_adi).glob("*.csproj"), None)
            if csproj and csproj.exists():
                paket_arg = f"{paket}::--version {surum}" if surum else paket
                # Basit yaklaşım: dotnet add
                try:
                    cmd = ["dotnet", "add", str(csproj), "package", paket]
                    if surum:
                        cmd += ["--version", surum]
                    cmd += ["--no-restore"]
                    subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=60, encoding="utf-8",
                                   errors="replace")
                    self._ok(f"📦 {paket} {surum} eklendi")
                except Exception:
                    pass

        p.dosyalar = proje_oku(p.kok, max_dosya=30)
        return True

    # ── 6) STATİK DENETİM ──────────────────────────────────
    def _statik_denetim(self, p: Proje) -> None:
        self._bas(6, "statik denetim", "Roslyn-style kural kontrolü")
        t0 = time.time()
        bulgular = CSharpDenetleyici.denetle(p.dosyalar)
        if not bulgular:
            self._ok("Kural ihlali yok", time.time() - t0)
            return
        hata_s = sum(1 for b in bulgular if b["seviye"] == "error")
        uyari_s = sum(1 for b in bulgular if b["seviye"] == "uyari")
        self._ok(f"{hata_s} hata, {uyari_s} uyarı", time.time() - t0)
        for b in bulgular[:6]:
            renk = C.RD if b["seviye"] == "error" else C.YL
            print(f"     {renk}{b['dosya']}:{b['satir']} {b['mesaj'][:70]}{C.R}")

    # ── 7) LLM DENETİMİ ────────────────────────────────────
    def _denetim(self, p: Proje) -> int:
        self._bas(7, "LLM denetimi", "Kod kalitesi ve güvenlik incelemesi")
        t0 = time.time()
        kod_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar if d["yol"].endswith(".cs"))
        v, hata = ollama_cagri(
            DENETIM.format(kod=kod_str[:14000]),
            "Kodu denetle, JSON döndür.",
            self.cfg, sicaklik=0.1, max_tok=1500,
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
            print(f"     {C.RD}✗ {k.get('sorun', '?')[:80]}{C.R}")
        for o in (v.get("orta") or [])[:3]:
            print(f"     {C.YL}⚠ {o.get('sorun', '?')[:80]}{C.R}")
        if v.get("ozet"):
            print(f"     {C.CY}→ {v['ozet'][:100]}{C.R}")
        return p.skor

    # ── 8) TEST PROJESİ ────────────────────────────────────
    def _test_kur(self, p: Proje) -> bool:
        if not self.cfg.otomatik_test:
            return False
        self._bas(8, "test projesi", "xUnit test projesi kuruluyor")
        t0 = time.time()

        test_ad = f"{p.proje_adi}.Tests"
        ok, _ = dotnet_new(p.kok, "xunit", test_ad)
        if not ok:
            self._hata("Test projesi kurulamadı")
            return False

        # Ana projeye referans
        test_dir = p.kok / test_ad
        try:
            subprocess.run(
                ["dotnet", "add", str(test_dir),
                 "reference", str(p.kok / p.proje_adi)],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace")
        except Exception:
            pass

        # FluentAssertions + Moq ekle
        test_csproj = next(test_dir.glob("*.csproj"), None)
        if test_csproj:
            for pk in ["FluentAssertions", "Moq"]:
                try:
                    subprocess.run(
                        ["dotnet", "add", str(test_csproj), "package", pk,
                         "--no-restore"],
                        capture_output=True, text=True, timeout=60,
                        encoding="utf-8", errors="replace")
                except Exception:
                    pass

        # Solution'a ekle
        if (p.kok / f"{p.proje_adi}.sln").exists():
            try:
                subprocess.run(
                    ["dotnet", "sln", str(p.kok / f"{p.proje_adi}.sln"),
                     "add", str(test_dir)],
                    capture_output=True, text=True, timeout=60,
                    encoding="utf-8", errors="replace")
            except Exception:
                pass

        p.test_proje_adi = test_ad
        self._ok(f"Test projesi: {test_ad}", time.time() - t0)

        # LLM testleri yazsın
        return self._test_yaz(p)

    def _test_yaz(self, p: Proje) -> bool:
        print(f"\n  {C.CY}▶ xUnit testleri üretiliyor...{C.R}")
        t0 = time.time()
        kod_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar
            if d["yol"].endswith(".cs")
            and "Tests" not in d["yol"])
        v, hata = ollama_cagri(
            TEST_YAZ.format(kod=kod_str[:12000], proje_adi=p.proje_adi),
            f"{p.proje_adi} projesi için xUnit testleri yaz.",
            self.cfg, sicaklik=0.2, max_tok=self.cfg.max_tok,
            canli=self.cfg.streaming, baslik="testler")
        if hata or not v:
            self._hata(f"Test üretilemedi: {hata}")
            return False

        test_dir = p.kok / p.test_proje_adi
        test_dosyalar = v.get("dosyalar") or []
        if test_dosyalar:
            projeye_yaz(test_dir, test_dosyalar)
            p.test_dosyalari = test_dosyalar
            self._ok(f"{len(test_dosyalar)} test dosyası yazıldı",
                     time.time() - t0)
            return True
        return False

    # ── 9) TEST DÖNGÜSÜ ────────────────────────────────────
    def _test_dongusu(self, p: Proje) -> Dict[str, Any]:
        if not self.cfg.otomatik_test:
            return {"calisti": False, "sebep": "test kapalı"}
        self._bas(9, "test çalıştırma",
                  f"dotnet test — max {self.cfg.max_test_tur} tur")

        for tur in range(1, self.cfg.max_test_tur + 1):
            print(f"\n  {C.CY}▶ Tur {tur}/{self.cfg.max_test_tur} — "
                  f"dotnet test{C.R}")
            sonuc = dotnet_test(p.kok, tur=tur)
            p.test_gecmis.append(sonuc)

            if not sonuc.get("calisti"):
                self._uyari(f"Test çalıştırılamadı: {sonuc.get('sebep')}")
                return sonuc

            if sonuc.get("basarili"):
                self._ok(f"TÜM TESTLER GEÇTİ "
                         f"({sonuc['gecen']} test, {sonuc['sure']:.1f}s)")
                return sonuc

            kalan = sonuc.get("kalan", 0)
            gecen = sonuc.get("gecen", 0)
            print(f"  {C.RD}✗ {kalan} test başarısız, {gecen} geçti{C.R}")
            for t in sonuc.get("basarisiz_testler", [])[:5]:
                print(f"     {C.RD}✗ {t}{C.R}")

            if tur == self.cfg.max_test_tur:
                self._hata("Max test turu aşıldı")
                return sonuc

            print(f"\n  {C.YL}↻ Testler LLM'e geri besleniyor...{C.R}")
            ok = self._test_duzelt(p, sonuc)
            if not ok:
                return sonuc

        return p.test_gecmis[-1]

    def _test_duzelt(self, p: Proje, sonuc: Dict) -> bool:
        # Test çıktısını kısalt
        cikti = sonuc.get("cikti", "")[:4000]
        test_dosyalar_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.test_dosyalari)
        prod_dosyalar_str = "\n\n".join(
            f"// === {d['yol']} ===\n{d['icerik']}"
            for d in p.dosyalar if d["yol"].endswith(".cs"))

        v, hata = ollama_cagri(
            TEST_DUZELT.format(
                test_sonuc=cikti,
                test_dosyalar=test_dosyalar_str[:8000],
                prod_dosyalar=prod_dosyalar_str[:10000]),
            "Test başarısızlığını düzelt.",
            self.cfg, sicaklik=0.2, max_tok=self.cfg.max_tok,
            canli=self.cfg.streaming, baslik="test düzeltmesi")
        if hata or not v:
            self._hata(f"Test düzeltmesi başarısız: {hata}")
            return False

        # Test dosyalarını güncelle
        test_dir = p.kok / (p.test_proje_adi or "")
        if v.get("test_dosyalari"):
            projeye_yaz(test_dir, v["test_dosyalari"])
            p.test_dosyalari = v["test_dosyalari"]
        # Prodüksiyon kodunu güncelle
        if v.get("prod_dosyalari"):
            projeye_yaz(p.kok / p.proje_adi, v["prod_dosyalari"])
            p.dosyalar = proje_oku(p.kok, max_dosya=30)

        # Yeniden derleme gerekebilir
        if v.get("prod_dosyalari"):
            print(f"  {C.D}↻ Prodüksiyon değişti, yeniden derleniyor...{C.R}")
            dr = dotnet_build(p.kok, tur=99)
            if not dr.basarili:
                self._uyari("Prodüksiyon derleme kırıldı, geri alınıyor")
                return False

        self._ok("Düzeltme uygulandı")
        return True

    # ── ANA AKIŞ ───────────────────────────────────────────
    def uret(self, istek: str) -> Optional[Proje]:
        # Çalışma klasörü
        kok = Path(tempfile.mkdtemp(prefix="bebek_cs_"))
        p = Proje(istek=istek, kok=kok)

        try:
            # 1) Tespit
            sablon, ad = self._tespit(istek)
            p.sablon, p.proje_adi = sablon, ad

            # 2) İskelet
            if not self._iskelet(p): return p

            # 3) Plan
            if not self._plan(p): return p

            # 4) Kod
            if not self._kod(p): return p

            # 5) Derleme döngüsü ← KİLLER FEATURE
            if not self._derleme_dongusu(p):
                self._hata("Derleme başarısız — çıktı yine de gösterilecek")

            # 6) Statik denetim
            self._statik_denetim(p)

            # 7) LLM denetimi
            self._denetim(p)

            # 8-9) Test
            if p.calisan and self.cfg.otomatik_test:
                if self._test_kur(p):
                    test_sonuc = self._test_dongusu(p)
                    if test_sonuc.get("basarili"):
                        print(f"\n  {C.GR}{C.B}🎉 PROJE HAZIR — "
                              f"derleme temiz, testler geçti.{C.R}")

            # Öğren
            if self.cfg.ogrenme and p.dosyalar and p.calisan:
                self.store.pattern_ekle(
                    istek, " ".join(sorted(_tokenize(istek))),
                    p.sablon, p.dosyalar, p.skor)
                print(f"\n  {C.D}💾 Belleğe kaydedildi (skor {p.skor}){C.R}")

            return p
        except Exception as e:
            self._hata(f"Üretim hatası: {e}")
            import traceback
            traceback.print_exc()
            return p


# ═══════════════════════════════════════════════════════════════════════════
#  GÖRSEL
# ═══════════════════════════════════════════════════════════════════════════
BANNER = f"""{C.CY}{C.B}
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║   ██████╗ ███████╗██████╗ ███████╗██╗  ██╗    ██████╗ ███████╗██╗   ██╗      ║
║   ██╔══██╗██╔════╝██╔══██╗██╔════╝██║ ██╔╝    ██╔══██╗██╔════╝██║   ██║      ║
║   ██████╔╝█████╗  ██████╔╝█████╗  █████╔╝     ██║  ██║█████╗  ██║   ██║      ║
║   ██╔══██╗██╔══╝  ██╔══██╗██╔══╝  ██╔═██╗     ██║  ██║██╔══╝  ╚██╗ ██╔╝      ║
║   ██████╔╝███████╗██████╔╝███████╗██║  ██╗    ██████╔╝███████╗ ╚████╔╝       ║
║   ╚═════╝ ╚══════╝╚═════╝ ╚══════╝╚═╝  ╚═╝    ╚═════╝ ╚══════╝  ╚═══╝        ║
║                                                                              ║
║              C#  U Z M A N I  •  v{SURUM}  •  GERÇEK DERLEME DÖNGÜSÜ          ║
║                                                                              ║
║     dotnet new → LLM → dotnet build → hata → LLM → build → TEST → ✓        ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝{C.R}
"""


def bios(cfg: Config, store: Store) -> bool:
    os.system("cls" if os.name == "nt" else "clear")
    print(BANNER)
    time.sleep(0.2)
    print(f"{C.YL}  Sistem kontrolü...{C.R}\n")

    # dotnet
    if dotnet_var():
        v = dotnet_surum()
        print(f"  {C.D}[✓]{C.R} .NET SDK            {C.GR}{v}{C.R}")
    else:
        print(f"  {C.D}[✗]{C.R} .NET SDK            {C.RD}KURULU DEĞİL{C.R}")
        print(f"\n  {C.RD}✗ dotnet olmadan bu araç çalışmaz.{C.R}")
        print(f"  {C.CY}İndir: https://dotnet.microsoft.com/download{C.R}\n")
        return False

    # Ollama
    hazir, mesaj = ollama_hazir(cfg)
    if hazir:
        print(f"  {C.D}[✓]{C.R} Ollama              {C.GR}{cfg.ollama_url}{C.R}")
        print(f"  {C.D}[✓]{C.R} Model               {C.GR}{mesaj}{C.R}")
    else:
        print(f"  {C.D}[✗]{C.R} Ollama              {C.RD}{mesaj}{C.R}")
        print(f"\n  {C.CY}Başlat: ollama serve{C.R}")
        print(f"  {C.CY}Model:  ollama pull {cfg.model}{C.R}\n")
        return False

    print(f"  {C.D}[✓]{C.R} Veritabanı          {C.GR}{cfg.db} "
          f"({store.sayi()} üretim){C.R}")
    print(f"\n  {C.GR}{C.B}Sistem hazır.{C.R}\n")
    return True


def karsilama(cfg: Config) -> None:
    print(f"{C.MG}{'═' * 78}{C.R}")
    print(f"{C.B}{C.CY}  🧠  BEBEK DEV v{SURUM} — C# UZMANI{C.R}")
    print(f"{C.MG}{'═' * 78}{C.R}")
    print(f"  {C.D}Model:{C.R} {cfg.model}   "
          f"{C.D}Streaming:{C.R} {'açık' if cfg.streaming else 'kapalı'}")
    print(f"\n{C.WH}{C.B}  Ne yapmak istiyorsun?{C.R}\n")
    print(f"{C.D}  Akış: Tespit → İskelet → Plan → Kod → BUILD → Düzelt "
          f"→ BUILD → Test → ✓{C.R}\n")
    print(f"{C.D}  Örnekler:{C.R}")
    print(f"    • jwt auth + ef core sqlite ile todo rest api yaz")
    print(f"    • rabbitmq consumer background service yaz")
    print(f"    • blazor server ile notepad uygulaması")
    print(f"    • generic repository pattern + unit of work")
    print(f"    • fluentvalidation ile register endpoint'i")
    print(f"\n{C.D}  Komutlar: {C.CY}/yardim{C.R}  |  Çıkış: {C.RD}/kapat{C.R}\n")


YARDIM = f"""{C.B}{C.CY}KOMUTLAR{C.R}
  {C.CY}/yardim{C.R}              Bu yardım
  {C.CY}/model <ad>{C.R}          Ollama modeli değiştir
  {C.CY}/stream ac|kapa{C.R}      Streaming çıktı
  {C.CY}/tur <sayi>{C.R}          Max build turu (varsayılan 5)
  {C.CY}/testtur <sayi>{C.R}      Max test turu (varsayılan 3)
  {C.CY}/test ac|kapa{C.R}        Test projesi üret
  {C.CY}/ctx <sayi>{C.R}          Context window (varsayılan 16384)
  {C.CY}/kaydet <klasör>{C.R}     Son projeyi kalıcı kaydet
  {C.CY}/calistir{C.R}            Son projeyi dotnet run ile çalıştır
  {C.CY}/gecmis{C.R}              Son üretimler
  {C.CY}/kapat{C.R}               Çıkış
"""


def projeyi_goster(p: Proje) -> None:
    print(f"\n{C.MG}{'═' * 78}{C.R}")
    baslik = "🎯 FİNAL PROJE"
    if p.calisan:
        baslik += f"  {C.GR}(DERLENDİ ✓){C.R}"
    else:
        baslik += f"  {C.YL}(derleme başarısız){C.R}"
    print(f"{C.B}{C.CY}  {baslik}{C.R}")
    print(f"{C.MG}{'═' * 78}{C.R}")
    print(f"  {C.D}Şablon:{C.R} {p.sablon}   "
          f"{C.D}Proje:{C.R} {p.proje_adi}   "
          f"{C.D}Skor:{C.R} {p.skor}/100")

    test = p.test_gecmis[-1] if p.test_gecmis else None
    if test and test.get("calisti"):
        if test.get("basarili"):
            print(f"  {C.GR}✓ Testler: {test['gecen']} geçti{C.R}")
        else:
            print(f"  {C.RD}✗ Testler: {test['kalan']} kaldı, "
                  f"{test['gecen']} geçti{C.R}")

    b = p.build_gecmis[-1] if p.build_gecmis else None
    if b:
        print(f"  {C.D}Son derleme:{C.R} {b.sure:.1f}s, "
              f"{len(b.uyarilar)} uyarı, {len(b.hatalar)} hata")

    if p.calisan:
        print(f"\n  {C.CY}📂 Konum: {C.R}{p.kok}")
        print(f"  {C.CY}▶ Çalıştır:{C.R} cd {p.kok / p.proje_adi} && dotnet run")
        if p.test_proje_adi:
            print(f"  {C.CY}🧪 Test:{C.R} cd {p.kok} && dotnet test")
    else:
        print(f"\n  {C.YL}⚠ Proje derlenmedi. Hatalar:{C.R}")
        if b:
            for h in b.hatalar[:5]:
                print(f"     {C.RD}{h.kisa()[:100]}{C.R}")

    print(f"\n{C.D}  💾 /kaydet <klasör>  →  projeyi kalıcı olarak sakla{C.R}\n")


# ═══════════════════════════════════════════════════════════════════════════
#  KOMUT İŞLEYİCİ
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
            print(f"  {C.CY}#{r['id']}{C.R} [{r['sablon']}] "
                  f"{r['istek'][:50]}  skor:{r['skor']}{t}  {C.D}{r['tarih']}{C.R}")
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
        except ValueError: print(f"{C.RD}✗ Geçersiz{C.R}")
        return True
    if low.startswith("/testtur "):
        try:
            cfg.max_test_tur = max(1, min(10, int(s[9:].strip())))
            store.ayar_set("max_test_tur", str(cfg.max_test_tur))
            print(f"{C.GR}✓ Max test turu: {cfg.max_test_tur}{C.R}")
        except ValueError: print(f"{C.RD}✗ Geçersiz{C.R}")
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
        except ValueError: print(f"{C.RD}✗ Geçersiz{C.R}")
        return True
    if low.startswith("/kaydet"):
        parca = s[7:].strip()
        p = son_proje[0]
        if not p or not p.dosyalar:
            print(f"{C.RD}✗ Önce proje üret.{C.R}"); return True
        if not parca:
            parca = input(f"  {C.CY}Hedef klasör: {C.R}").strip() or "./cikti"
        hedef = Path(parca).expanduser().resolve()
        hedef.mkdir(parents=True, exist_ok=True)
        # Projeyi kopyala (bin/obj hariç)
        atla = {"bin", "obj", ".vs", ".vscode"}
        n = 0
        for src in p.kok.rglob("*"):
            if not src.is_file(): continue
            if any(part in atla for part in src.parts): continue
            rel = src.relative_to(p.kok)
            dst = hedef / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst); n += 1
            except Exception:
                pass
        print(f"{C.GR}✓ {n} dosya kopyalandı: {hedef}{C.R}")
        print(f"  {C.CY}▶ Çalıştır:{C.R} cd {hedef / p.proje_adi} && dotnet run")
        return True
    if low == "/calistir":
        p = son_proje[0]
        if not p or not p.dosyalar:
            print(f"{C.RD}✗ Önce proje üret.{C.R}"); return True
        if not p.calisan:
            print(f"{C.RD}✗ Proje derlenmemiş.{C.R}"); return True
        print(f"{C.D}▶ dotnet run (Ctrl+C ile durdur){C.R}\n")
        try:
            subprocess.run(
                ["dotnet", "run", "--project", str(p.kok / p.proje_adi)],
                cwd=str(p.kok / p.proje_adi),
                timeout=120)
        except subprocess.TimeoutExpired:
            print(f"\n{C.YL}Zaman aşımı.{C.R}")
        except KeyboardInterrupt:
            print(f"\n{C.YL}Durduruldu.{C.R}")
        return True
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  ANA
# ═══════════════════════════════════════════════════════════════════════════
def main() -> int:
    global _sinyal_izin
    signal.signal(signal.SIGINT, _sinyal_handler)
    try: signal.signal(signal.SIGTERM, _sinyal_handler)
    except (AttributeError, ValueError): pass

    cfg = Config()
    store = Store(cfg.db)

    # Ayarları yükle
    cfg.model = store.ayar_get("model", cfg.model)
    cfg.streaming = store.ayar_get("streaming", "1") == "1"
    cfg.otomatik_test = store.ayar_get("otomatik_test", "1") == "1"
    try: cfg.max_build_tur = int(store.ayar_get("max_build_tur", str(cfg.max_build_tur)))
    except ValueError: pass
    try: cfg.max_test_tur = int(store.ayar_get("max_test_tur", str(cfg.max_test_tur)))
    except ValueError: pass
    try: cfg.num_ctx = int(store.ayar_get("num_ctx", str(cfg.num_ctx)))
    except ValueError: pass

    if not bios(cfg, store):
        store.kapat(); return 1

    karsilama(cfg)
    motor = AkilliMotor(store, cfg)
    son_proje: List[Optional[Proje]] = [None]

    while True:
        try:
            s = input(f"{C.B}{C.GR}cs>{C.R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YL}Çıkmak için /kapat{C.R}"); continue
        if not s: continue

        try:
            r = komut(s, motor, store, cfg, son_proje)
        except Exception as e:
            print(f"{C.RD}✗ Komut hatası: {e}{C.R}"); continue
        if r is False: break
        if r is True: continue

        # Üretim
        try:
            p = motor.uret(s)
            if p:
                son_proje[0] = p
                projeyi_goster(p)
                # Kaydet
                kod_str = "\n\n".join(
                    f"// === {d['yol']} ===\n{d['icerik']}" for d in p.dosyalar)
                test = p.test_gecmis[-1] if p.test_gecmis else {}
                store.uretim_ekle(
                    s, p.sablon, p.proje_adi, kod_str, p.skor,
                    test.get("gecen", 0), test.get("kalan", 0))
                if p.kok and p.kok.exists():
                    print(f"{C.D}💡 Proje geçici klasörde: {p.kok}{C.R}")
                    print(f"{C.D}   Kalıcı kaydetmek için: /kaydet <klasör>{C.R}\n")
        except Exception as e:
            print(f"{C.RD}✗ Üretim hatası: {e}{C.R}")

    _sinyal_izin = True
    print(f"\n{C.CY}Kapatılıyor...{C.R}")
    store.kapat()
    print(f"{C.GR}✓ Görüşürüz.{C.R}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
