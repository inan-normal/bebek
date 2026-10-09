#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bebek Ultra v11.4 — Web aramalı, kendi kendine öğrenen

v11.3 → v11.4:
- Otomatik web araması: kullanıcı bilgi sorusu sorunca bot kendi araştırır
- WebArama: DuckDuckGo (paket → HTML → Instant) + Wikipedia (TR→EN)
- web_cache tablosu, TTL 24 saat
- LLM ile arama sonuçlarını doğal Türkçe özete çevirir
- /web, /webon, /weboff, /webdurum, /webtemizle komutları
- Kalıcı kalıp havuzu (v11.3'ten)

⚠️ API anahtarı gömülüdür. Üretimde rotate edin.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import datetime as dt
import html as html_mod
import json
import logging
import math
import operator
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

try:
    from rapidfuzz import fuzz as rf_fuzz, process as rf_process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

try:
    from ddgs import DDGS  # type: ignore
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS  # type: ignore
        HAS_DDGS = True
    except ImportError:
        HAS_DDGS = False


SURUM = "11.4"

# ============================================================
# GÖMÜLÜ API ANAHTARI (⚠️ rotate edilmeli)
# ============================================================
_GOMULU_API_KEY = "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"


# ============================================================
# LOGGING
# ============================================================
log = logging.getLogger("bebek")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ============================================================
# HTTP OTURUMLARI
# ============================================================
_session = requests.Session()
_session.headers.update({
    "User-Agent": f"bebek-ultra/{SURUM}",
    "Accept-Encoding": "gzip, deflate",
})

_web_session = requests.Session()
_web_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


# ============================================================
# YAPILANDIRMA
# ============================================================
@dataclass
class Config:
    db_path: str = "ultra_hafiza.db"
    model: str = "openai/gpt-oss-120b"
    api_url: str = "https://api.groq.com/openai/v1/chat/completions"
    api_key: str = ""

    esik_uzun: float = 0.72
    esik_kisa: float = 0.86
    max_cevap: int = 5
    gecmis_llm: int = 6
    gecmis_db: int = 200
    gecmis_temizle_araligi: int = 25

    llm_temp: float = 0.75
    llm_max_tok: int = 800
    llm_timeout: int = 90
    llm_retry: int = 3

    cache_min_len: int = 15
    kategori_esik: int = 3

    kalip_hedef: int = 20
    kalip_min_havuz: int = 5

    # Web
    web_aktif: bool = True
    web_limit: int = 4
    web_ttl_saat: int = 24
    web_timeout: int = 15
    web_snippet_max: int = 300

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        c.db_path = os.environ.get("BEBEK_DB", c.db_path)
        c.model = os.environ.get("BEBEK_MODEL", c.model)
        c.api_key = _load_api_key()
        return c


def _load_api_key() -> str:
    k = os.environ.get("GROQ_API_KEY", "").strip()
    if k:
        return k
    for fname in (".groq_key", "groq_key.txt"):
        if os.path.exists(fname):
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    icerik = f.read().strip()
                if icerik:
                    return icerik
            except OSError as e:
                log.warning("API anahtarı okunamadı (%s): %s", fname, e)
    if _GOMULU_API_KEY:
        return _GOMULU_API_KEY
    return ""


# ============================================================
# METİN
# ============================================================
def tr_lower(s: str) -> str:
    return (s.replace("İ", "i").replace("I", "ı")
             .replace("Ş", "ş").replace("Ğ", "ğ")
             .replace("Ü", "ü").replace("Ö", "ö").replace("Ç", "ç")
             .lower())


_TOKEN_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(s: str) -> str:
    s = tr_lower(s)
    s = _TOKEN_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def benzerlik(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if HAS_RAPIDFUZZ:
        return rf_fuzz.token_set_ratio(a, b) / 100.0
    wa, wb = set(a.split()), set(b.split())
    j = len(wa & wb) / len(wa | wb) if (wa | wb) else 0.0
    c = SequenceMatcher(None, a, b).ratio()
    return max(j, c * 0.9)


def benzerlik_esik(soru: str, cfg: Config) -> float:
    return cfg.esik_kisa if len(normalize(soru)) < 12 else cfg.esik_uzun


# ============================================================
# MATEMATİK
# ============================================================
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval_dugum(d: ast.AST) -> float:
    if isinstance(d, ast.Constant) and isinstance(d.value, (int, float)):
        return d.value
    if isinstance(d, ast.BinOp) and type(d.op) in _OPS:
        return _OPS[type(d.op)](_eval_dugum(d.left), _eval_dugum(d.right))
    if isinstance(d, ast.UnaryOp) and type(d.op) in _OPS:
        return _OPS[type(d.op)](_eval_dugum(d.operand))
    raise ValueError("yasak ifade")


def guvenli_eval(ifade: str) -> float:
    return _eval_dugum(ast.parse(ifade, mode="eval").body)


def matematik_coz(soru: str) -> Optional[str]:
    s = tr_lower(soru)
    if "karekök" in s or "karekok" in s:
        m = re.search(r"(-?\d+\.?\d*)", s)
        if m:
            n = float(m.group(1))
            if n >= 0:
                return f"√{n:g} = {math.sqrt(n):.6g}"
            return "Negatif sayının gerçek karekökü yok."
    m = re.search(r"(-?\d+\.?\d*)\D{0,6}yüzde\D{0,6}(-?\d+\.?\d*)", s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return f"{a:g} sayısının %{b:g}'i = {a * b / 100:.6g}"
    s2 = soru.replace("×", "*").replace("÷", "/").replace("^", "**")
    s2 = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", s2)
    ifade = re.sub(r"[^\d+\-*/(). ]", " ", s2)
    ifade = re.sub(r"\s+", " ", ifade).strip()
    if re.search(r"\d\s*[+\-*/]\s*\d", ifade) or re.search(r"\(\s*-?\d", ifade):
        try:
            sonuc = guvenli_eval(ifade)
            return f"{ifade} = {sonuc:g}"
        except Exception:
            return None
    return None


# ============================================================
# SQLITE
# ============================================================
SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS cache (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    anahtar   TEXT UNIQUE NOT NULL,
    orijinal  TEXT NOT NULL,
    kategori  TEXT NOT NULL DEFAULT 'genel',
    ilk       TEXT NOT NULL,
    son       TEXT NOT NULL,
    sayi      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cevaplar (
    cache_id INTEGER NOT NULL,
    cevap    TEXT NOT NULL,
    sira     INTEGER NOT NULL,
    PRIMARY KEY (cache_id, sira),
    FOREIGN KEY (cache_id) REFERENCES cache(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ogretilen (
    soru  TEXT NOT NULL,
    cevap TEXT NOT NULL,
    PRIMARY KEY (soru, cevap)
);

CREATE TABLE IF NOT EXISTS kisi (
    alan  TEXT PRIMARY KEY,
    deger TEXT NOT NULL,
    tarih TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notlar (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    metin TEXT NOT NULL,
    tarih TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gecmis (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    soru  TEXT NOT NULL,
    cevap TEXT NOT NULL,
    niyet TEXT,
    tarih TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ayar (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kaliplar (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kategori TEXT NOT NULL,
    cevap    TEXT NOT NULL,
    kaynak   TEXT NOT NULL DEFAULT 'llm',
    tarih    TEXT NOT NULL,
    UNIQUE(kategori, cevap)
);

CREATE TABLE IF NOT EXISTS web_cache (
    sorgu  TEXT PRIMARY KEY,
    sonuc  TEXT NOT NULL,
    tarih  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gecmis_id ON gecmis(id);
CREATE INDEX IF NOT EXISTS idx_notlar_id ON notlar(id);
CREATE INDEX IF NOT EXISTS idx_kaliplar_kat ON kaliplar(kategori);
CREATE INDEX IF NOT EXISTS idx_web_tarih ON web_cache(tarih);
"""

SCHEMA_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS cache_fts USING fts5(
    anahtar,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS cache_ai AFTER INSERT ON cache BEGIN
    INSERT INTO cache_fts(rowid, anahtar) VALUES (new.id, new.anahtar);
END;

CREATE TRIGGER IF NOT EXISTS cache_ad AFTER DELETE ON cache BEGIN
    DELETE FROM cache_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS cache_au AFTER UPDATE OF anahtar ON cache BEGIN
    DELETE FROM cache_fts WHERE rowid = old.id;
    INSERT INTO cache_fts(rowid, anahtar) VALUES (new.id, new.anahtar);
END;
"""


def _has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _fts_quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


class Store:
    def __init__(self, path: str, cfg: Config):
        self.cfg = cfg
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_BASE)
        self.has_fts5 = _has_fts5(self.conn)
        if self.has_fts5:
            self.conn.executescript(SCHEMA_FTS5)
        self.conn.commit()
        self._gecmis_ins = 0
        log.info("Depo açıldı: %s (FTS5=%s)", path, self.has_fts5)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.kapat()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- CACHE ----------
    def _fts_adaylar(self, hedef: str, limit: int = 30) -> List[Tuple[str, int]]:
        tokens = [t for t in hedef.split() if len(t) >= 2]
        if not tokens:
            return []
        q = " OR ".join(_fts_quote(t) for t in tokens)
        try:
            rows = self.conn.execute(
                "SELECT c.anahtar, c.id FROM cache_fts "
                "JOIN cache c ON c.id = cache_fts.rowid "
                "WHERE cache_fts MATCH ? ORDER BY bm25(cache_fts) LIMIT ?",
                (q, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r["anahtar"], r["id"]) for r in rows]

    def _like_adaylar(self, hedef: str, limit: int = 30) -> List[Tuple[str, int]]:
        prefix = hedef[:4]
        if len(prefix) < 3:
            return []
        rows = self.conn.execute(
            "SELECT anahtar, id FROM cache WHERE anahtar LIKE ? LIMIT ?",
            (prefix + "%", limit)).fetchall()
        return [(r["anahtar"], r["id"]) for r in rows]

    def _rescore(self, hedef: str, adaylar: List[Tuple[str, int]]
                 ) -> Tuple[Optional[str], int, float]:
        if not adaylar:
            return None, -1, 0.0
        if HAS_RAPIDFUZZ:
            anahtarlar = [a for a, _ in adaylar]
            m = rf_process.extractOne(hedef, anahtarlar,
                                     scorer=rf_fuzz.token_set_ratio)
            if not m:
                return None, -1, 0.0
            _, skor, idx = m
            a, cid = adaylar[idx]
            return a, cid, skor / 100.0
        en_iyi, en_iyi_id, skor = None, -1, 0.0
        for a, cid in adaylar:
            s = benzerlik(hedef, a)
            if s > skor:
                skor, en_iyi, en_iyi_id = s, a, cid
        return en_iyi, en_iyi_id, skor

    def cache_bul(self, soru: str) -> Tuple[Optional[str], float]:
        hedef = normalize(soru)
        if not hedef:
            return None, 0.0
        row = self.conn.execute(
            "SELECT id FROM cache WHERE anahtar=?", (hedef,)).fetchone()
        if row:
            return hedef, 1.0
        adaylar = self._fts_adaylar(hedef) if self.has_fts5 else []
        anahtar, cid, skor = self._rescore(hedef, adaylar)
        if cid < 0 or skor < benzerlik_esik(soru, self.cfg):
            like_aday = self._like_adaylar(hedef)
            a2, c2, s2 = self._rescore(hedef, like_aday)
            if s2 > skor:
                anahtar, cid, skor = a2, c2, s2
        if cid < 0 or skor < benzerlik_esik(soru, self.cfg):
            return None, skor
        return anahtar, skor

    def cache_cevap(self, soru: str) -> Tuple[Optional[str], float]:
        anahtar, skor = self.cache_bul(soru)
        if not anahtar:
            return None, skor
        rows = self.conn.execute(
            "SELECT cevap FROM cevaplar WHERE cache_id="
            "(SELECT id FROM cache WHERE anahtar=?) ORDER BY sira",
            (anahtar,)).fetchall()
        if not rows:
            return None, skor
        return random.choice([x["cevap"] for x in rows]), skor

    def cache_kaydet(self, soru: str, cevap: str, kategori: str = "genel") -> None:
        cevap = cevap.strip()
        if len(cevap) < self.cfg.cache_min_len:
            return
        anahtar = normalize(soru)
        if not anahtar:
            return
        simdi = dt.datetime.now().isoformat(timespec="seconds")
        with self._tx():
            row = self.conn.execute(
                "SELECT id FROM cache WHERE anahtar=?", (anahtar,)).fetchone()
            if row:
                cid = row["id"]
                self.conn.execute(
                    "UPDATE cache SET son=?, sayi=sayi+1 WHERE id=?",
                    (simdi, cid))
                var = self.conn.execute(
                    "SELECT 1 FROM cevaplar WHERE cache_id=? AND cevap=?",
                    (cid, cevap)).fetchone()
                if not var:
                    max_sira = self.conn.execute(
                        "SELECT COALESCE(MAX(sira), -1) FROM cevaplar "
                        "WHERE cache_id=?", (cid,)).fetchone()[0]
                    self.conn.execute(
                        "INSERT INTO cevaplar(cache_id, cevap, sira) "
                        "VALUES(?, ?, ?)", (cid, cevap, max_sira + 1))
                    keep = [r[0] for r in self.conn.execute(
                        "SELECT sira FROM cevaplar WHERE cache_id=? "
                        "ORDER BY sira DESC LIMIT ?",
                        (cid, self.cfg.max_cevap)).fetchall()]
                    if keep:
                        ph = ",".join("?" * len(keep))
                        self.conn.execute(
                            f"DELETE FROM cevaplar "
                            f"WHERE cache_id=? AND sira NOT IN ({ph})",
                            (cid, *keep))
            else:
                cur = self.conn.execute(
                    "INSERT INTO cache(anahtar, orijinal, kategori, ilk, son, sayi)"
                    " VALUES(?, ?, ?, ?, ?, 1)",
                    (anahtar, soru, kategori, simdi, simdi))
                self.conn.execute(
                    "INSERT INTO cevaplar(cache_id, cevap, sira) VALUES(?, ?, 0)",
                    (cur.lastrowid, cevap))

    def cache_sil(self, soru: str) -> Optional[str]:
        anahtar, _ = self.cache_bul(soru)
        if not anahtar:
            return None
        with self._tx():
            self.conn.execute("DELETE FROM cache WHERE anahtar=?", (anahtar,))
        return anahtar

    def cache_sayisi(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]

    def kategori_dagilimi(self) -> List[Tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT kategori, COUNT(*) AS c FROM cache GROUP BY kategori "
            "ORDER BY c DESC").fetchall()
        return [(r["kategori"], r["c"]) for r in rows]

    # ---------- WEB CACHE ----------
    def web_cache_get(self, sorgu: str) -> Optional[List[Dict[str, str]]]:
        key = normalize(sorgu)
        row = self.conn.execute(
            "SELECT sonuc, tarih FROM web_cache WHERE sorgu=?", (key,)
        ).fetchone()
        if not row:
            return None
        try:
            tarih = dt.datetime.fromisoformat(row["tarih"])
        except ValueError:
            return None
        yas = (dt.datetime.now() - tarih).total_seconds() / 3600.0
        if yas > self.cfg.web_ttl_saat:
            with self._tx():
                self.conn.execute(
                    "DELETE FROM web_cache WHERE sorgu=?", (key,))
            return None
        try:
            d = json.loads(row["sonuc"])
            if isinstance(d, list):
                return d
        except json.JSONDecodeError:
            return None
        return None

    def web_cache_set(self, sorgu: str, sonuc: List[Dict[str, str]]) -> None:
        key = normalize(sorgu)
        if not key:
            return
        simdi = dt.datetime.now().isoformat(timespec="seconds")
        try:
            with self._tx():
                self.conn.execute(
                    "INSERT INTO web_cache(sorgu, sonuc, tarih) "
                    "VALUES(?, ?, ?) "
                    "ON CONFLICT(sorgu) DO UPDATE SET "
                    "sonuc=excluded.sonuc, tarih=excluded.tarih",
                    (key, json.dumps(sonuc, ensure_ascii=False), simdi))
        except sqlite3.Error as e:
            log.warning("web_cache_set: %s", e)

    def web_cache_temizle(self) -> int:
        with self._tx():
            cur = self.conn.execute("DELETE FROM web_cache")
        return cur.rowcount

    def web_cache_sayisi(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM web_cache").fetchone()[0]

    # ---------- ÖĞRETİLEN ----------
    def ogret(self, soru: str, cevap: str) -> None:
        k = normalize(soru)
        if not k:
            return
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO ogretilen(soru, cevap) VALUES(?, ?)",
                (k, cevap.strip()))

    def ogretilen_ara(self, soru: str) -> Optional[str]:
        hedef = normalize(soru)
        rows = self.conn.execute(
            "SELECT soru, cevap FROM ogretilen").fetchall()
        if not rows:
            return None
        if HAS_RAPIDFUZZ:
            anahtarlar = [r["soru"] for r in rows]
            m = rf_process.extractOne(hedef, anahtarlar,
                                     scorer=rf_fuzz.token_set_ratio)
            if not m:
                return None
            _, skor, idx = m
            if skor / 100.0 >= benzerlik_esik(soru, self.cfg):
                return rows[idx]["cevap"]
            return None
        en_iyi, skor = None, 0.0
        for r in rows:
            s = benzerlik(hedef, r["soru"])
            if s > skor:
                skor, en_iyi = s, r["cevap"]
        return en_iyi if skor >= benzerlik_esik(soru, self.cfg) else None

    def ogretilen_sayisi(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT soru) FROM ogretilen").fetchone()[0]

    # ---------- KALIPLAR ----------
    def kalip_ekle(self, kategori: str, cevap: str,
                   kaynak: str = "llm") -> bool:
        cevap = cevap.strip()
        if not cevap or len(cevap) < 3 or len(cevap) > 500:
            return False
        try:
            with self._tx():
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO kaliplar"
                    "(kategori, cevap, kaynak, tarih) VALUES(?, ?, ?, ?)",
                    (kategori, cevap, kaynak,
                     dt.datetime.now().isoformat(timespec="seconds")))
            return cur.rowcount > 0
        except sqlite3.Error as e:
            log.warning("kalip_ekle hata: %s", e)
            return False

    def kalip_rastgele(self, kategori: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT cevap FROM kaliplar WHERE kategori=? "
            "ORDER BY RANDOM() LIMIT 1",
            (kategori,)).fetchone()
        return row["cevap"] if row else None

    def kalip_sayisi(self, kategori: Optional[str] = None) -> int:
        if kategori:
            return self.conn.execute(
                "SELECT COUNT(*) FROM kaliplar WHERE kategori=?",
                (kategori,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM kaliplar").fetchone()[0]

    def kalip_dagilimi(self) -> List[Tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT kategori, COUNT(*) AS c FROM kaliplar "
            "GROUP BY kategori ORDER BY c DESC").fetchall()
        return [(r["kategori"], r["c"]) for r in rows]

    def kalip_temizle(self, kategori: Optional[str] = None) -> int:
        with self._tx():
            if kategori:
                cur = self.conn.execute(
                    "DELETE FROM kaliplar WHERE kategori=?", (kategori,))
            else:
                cur = self.conn.execute("DELETE FROM kaliplar")
        return cur.rowcount

    # ---------- KİŞİ ----------
    def kisi_set(self, alan: str, deger: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO kisi(alan, deger, tarih) VALUES(?, ?, ?) "
                "ON CONFLICT(alan) DO UPDATE SET deger=excluded.deger, "
                "tarih=excluded.tarih",
                (alan, deger,
                 dt.datetime.now().isoformat(timespec="seconds")))

    def kisi_get(self, alan: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT deger FROM kisi WHERE alan=?", (alan,)).fetchone()
        return row["deger"] if row else None

    def kisi_tumu(self) -> Dict[str, str]:
        rows = self.conn.execute("SELECT alan, deger FROM kisi").fetchall()
        return {r["alan"]: r["deger"] for r in rows}

    # ---------- NOTLAR ----------
    def not_ekle(self, metin: str) -> int:
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO notlar(metin, tarih) VALUES(?, ?)",
                (metin, dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
        return cur.lastrowid or 0

    def not_listele(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT metin, tarih FROM notlar ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"metin": r["metin"], "tarih": r["tarih"]} for r in rows]

    def not_sayisi(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM notlar").fetchone()[0]

    # ---------- GEÇMİŞ ----------
    def gecmis_ekle(self, soru: str, cevap: str, niyet: Optional[str]) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO gecmis(soru, cevap, niyet, tarih) VALUES(?, ?, ?, ?)",
                (soru, cevap, niyet,
                 dt.datetime.now().isoformat(timespec="seconds")))
        self._gecmis_ins += 1
        if self._gecmis_ins >= self.cfg.gecmis_temizle_araligi:
            self._gecmis_ins = 0
            n = self.gecmis_sayisi()
            if n > self.cfg.gecmis_db * 2:
                with self._tx():
                    self.conn.execute(
                        "DELETE FROM gecmis WHERE id IN ("
                        "  SELECT id FROM gecmis ORDER BY id ASC LIMIT ?)",
                        (n - self.cfg.gecmis_db,))

    def gecmis_son(self, n: int) -> List[Tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT soru, cevap FROM gecmis ORDER BY id DESC LIMIT ?",
            (n,)).fetchall()
        return [(r["soru"], r["cevap"]) for r in reversed(rows)]

    def gecmis_sayisi(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM gecmis").fetchone()[0]

    def gecmis_temizle(self) -> None:
        with self._tx():
            self.conn.execute("DELETE FROM gecmis")

    # ---------- NİYET STACK ----------
    def niyet_push(self, niyet: Optional[str], boyut: int = 3) -> None:
        try:
            stack = json.loads(self.ayar_get("niyet_stack", "[]"))
            if not isinstance(stack, list):
                stack = []
        except json.JSONDecodeError:
            stack = []
        stack.append(niyet or "")
        stack = stack[-boyut:]
        self.ayar_set("niyet_stack", json.dumps(stack))

    def niyet_son(self) -> Optional[str]:
        try:
            stack = json.loads(self.ayar_get("niyet_stack", "[]"))
            if not isinstance(stack, list):
                return None
        except json.JSONDecodeError:
            return None
        for n in reversed(stack):
            if n:
                return n
        return None

    # ---------- AYAR ----------
    def ayar_set(self, k: str, v: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO ayar(k, v) VALUES(?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def ayar_get(self, k: str, default: str = "") -> str:
        row = self.conn.execute("SELECT v FROM ayar WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def kapat(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


# ============================================================
# WEB ARAMA
# ============================================================
@dataclass
class WebSonuc:
    baslik: str
    url: str
    snippet: str
    kaynak: str = "ddg"


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _kisalt(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


class WebArama:
    """
    Sırayla dener:
      1) ddgs / duckduckgo_search paketi (varsa)
      2) DuckDuckGo HTML endpoint
      3) DuckDuckGo Instant Answer API
      4) Wikipedia (TR → EN)
    """

    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    def ara(self, sorgu: str, limit: Optional[int] = None) -> List[WebSonuc]:
        sorgu = sorgu.strip()
        if not sorgu:
            return []
        limit = limit or self.cfg.web_limit

        onb = self.store.web_cache_get(sorgu)
        if onb:
            log.debug("web cache hit: %s", sorgu)
            return [WebSonuc(**d) for d in onb[:limit]]

        sonuc: List[WebSonuc] = []
        for fn in (self._ddgs, self._ddg_html, self._ddg_instant,
                   self._wikipedia):
            try:
                r = fn(sorgu, limit)
            except Exception as e:
                log.debug("web %s hata: %s", fn.__name__, e)
                r = []
            if r:
                log.debug("web kaynak: %s (%d sonuç)",
                          fn.__name__, len(r))
                sonuc = r
                break

        if sonuc:
            self.store.web_cache_set(
                sorgu,
                [{"baslik": s.baslik, "url": s.url,
                  "snippet": s.snippet, "kaynak": s.kaynak}
                 for s in sonuc[:limit]])
        return sonuc[:limit]

    def _ddgs(self, sorgu: str, limit: int) -> List[WebSonuc]:
        if not HAS_DDGS:
            return []
        out: List[WebSonuc] = []
        with DDGS() as ddgs:
            for r in ddgs.text(sorgu, region="tr-tr", max_results=limit):
                baslik = (r.get("title") or "").strip()
                url = (r.get("href") or r.get("url") or "").strip()
                sn = (r.get("body") or "").strip()
                if not baslik or not url:
                    continue
                out.append(WebSonuc(
                    baslik=_kisalt(baslik, 150),
                    url=url,
                    snippet=_kisalt(_strip_html(sn),
                                    self.cfg.web_snippet_max),
                    kaynak="ddgs"))
        return out

    def _ddg_html(self, sorgu: str, limit: int) -> List[WebSonuc]:
        try:
            r = _web_session.post(
                "https://html.duckduckgo.com/html/",
                data={"q": sorgu, "kl": "tr-tr"},
                timeout=self.cfg.web_timeout)
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []
        kalip = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE)
        out: List[WebSonuc] = []
        for m in kalip.finditer(r.text):
            href, baslik, sn = m.group(1), m.group(2), m.group(3)
            href = html_mod.unescape(href)
            if href.startswith("//duckduckgo.com/l/?uddg="):
                q = urllib.parse.urlparse("https:" + href).query
                gercek = urllib.parse.parse_qs(q).get("uddg", [""])[0]
                href = urllib.parse.unquote(gercek) or href
            baslik = _strip_html(baslik)
            sn = _strip_html(sn)
            if not baslik or not href.startswith("http"):
                continue
            out.append(WebSonuc(
                baslik=_kisalt(baslik, 150),
                url=href,
                snippet=_kisalt(sn, self.cfg.web_snippet_max),
                kaynak="ddg_html"))
            if len(out) >= limit:
                break
        return out

    def _ddg_instant(self, sorgu: str, limit: int) -> List[WebSonuc]:
        try:
            r = _web_session.get(
                "https://api.duckduckgo.com/",
                params={"q": sorgu, "format": "json", "no_html": 1,
                        "no_redirect": 1, "kl": "tr-tr"},
                timeout=self.cfg.web_timeout)
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []
        try:
            d = r.json()
        except ValueError:
            return []
        out: List[WebSonuc] = []
        if d.get("AbstractText"):
            out.append(WebSonuc(
                baslik=d.get("Heading") or sorgu,
                url=d.get("AbstractURL") or "https://duckduckgo.com",
                snippet=_kisalt(_strip_html(d["AbstractText"]),
                                self.cfg.web_snippet_max),
                kaynak="ddg_instant"))
        for t in (d.get("RelatedTopics") or [])[: max(0, limit - len(out))]:
            if isinstance(t, dict) and t.get("Text"):
                out.append(WebSonuc(
                    baslik=_kisalt(t["Text"], 150),
                    url=t.get("FirstURL") or "",
                    snippet=_kisalt(_strip_html(t["Text"]),
                                    self.cfg.web_snippet_max),
                    kaynak="ddg_instant"))
        return out

    def _wikipedia(self, sorgu: str, limit: int) -> List[WebSonuc]:
        for lang in ("tr", "en"):
            try:
                r = _web_session.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": sorgu,
                        "format": "json",
                        "srlimit": limit,
                    },
                    timeout=self.cfg.web_timeout)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            try:
                d = r.json()
            except ValueError:
                continue
            out: List[WebSonuc] = []
            for it in d.get("query", {}).get("search", []):
                baslik = it.get("title") or ""
                if not baslik:
                    continue
                url = (f"https://{lang}.wikipedia.org/wiki/"
                       + urllib.parse.quote(baslik.replace(" ", "_")))
                sn = _strip_html(it.get("snippet") or "")
                out.append(WebSonuc(
                    baslik=_kisalt(baslik, 150),
                    url=url,
                    snippet=_kisalt(sn, self.cfg.web_snippet_max),
                    kaynak=f"wikipedia_{lang}"))
            if out:
                return out
        return []


# ============================================================
# KATEGORİ
# ============================================================
KATEGORILER: Dict[str, Dict[str, int]] = {
    "selamlasma": {"selam": 3, "merhaba": 3, "hey": 2, "naber": 3,
                   "nasılsın": 3, "günaydın": 3, "iyi akşamlar": 3,
                   "alo": 2, "slm": 2, "nasıl gidiyor": 3,
                   "napıyorsun": 3, "ne yapıyorsun": 3, "iyi misin": 3},
    "veda":       {"hoşçakal": 3, "görüşürüz": 3, "güle güle": 3,
                   "çıkıyorum": 3, "bay bay": 3, "kendine iyi bak": 3},
    "tesekkur":   {"teşekkür": 3, "sağol": 3, "eyvallah": 3, "tşk": 3,
                   "sağ ol": 3},
    "ozur":       {"özür": 3, "pardon": 3, "kusura": 3, "affet": 3,
                   "affedersin": 3},
    "duygu":      {"üzgün": 3, "mutlu": 3, "kızgın": 3, "sinirli": 3,
                   "yalnız": 3, "moral": 3, "stres": 3, "kaygı": 3,
                   "depres": 3, "kötüyüm": 3, "iyiyim": 2},
    "matematik":  {"topla": 3, "çarp": 3, "böl": 3, "çıkar": 3, "hesap": 3,
                   "yüzde": 3, "karekök": 4},
    "kod":        {"python": 4, "javascript": 4, "kod": 3, "program": 3,
                   "yazılım": 3, "bug": 3, "fonksiyon": 3, "değişken": 3,
                   "döngü": 3, "html": 3, "css": 3, "sql": 3, "java": 3,
                   "c++": 3, "kodla": 3},
    "oyun":       {"minecraft": 4, "roblox": 4, "valorant": 4, "csgo": 4,
                   "fifa": 4, "pubg": 4},
    "yemek":      {"tarif": 4, "yemek": 3, "pizza": 3, "makarna": 3,
                   "çorba": 3, "tatlı": 3, "kahvaltı": 3, "pasta": 3,
                   "börek": 3},
    "spor":       {"futbol": 3, "basketbol": 3, "maç": 3, "antrenman": 3,
                   "koşu": 3, "voleybol": 3, "yüzme": 3},
    "tarih":      {"osmanlı": 4, "cumhuriyet": 4, "atatürk": 4, "savaş": 3,
                   "fatih": 3, "kanuni": 3},
    "saglik":     {"doktor": 3, "ilaç": 3, "ağrı": 3, "mide": 3,
                   "hastane": 3, "vitamin": 3},
    "teknoloji":  {"telefon": 3, "bilgisayar": 3, "internet": 3,
                   "yapay zeka": 4, "robot": 3, "işlemci": 3, "ram": 3},
    "sanat":      {"müzik": 3, "resim": 3, "film": 3, "dizi": 3, "kitap": 3,
                   "şiir": 3, "roman": 3, "gitar": 3},
    "ask":        {"aşk": 3, "sevgili": 3, "ilişki": 3, "flört": 3,
                   "evlilik": 3, "aşık": 3},
    "para":       {"maaş": 3, "kariyer": 3, "yatırım": 3, "borsa": 3,
                   "dolar": 3, "bitcoin": 3, "kripto": 3},
    "egitim":     {"üniversite": 3, "sınav": 3, "ders": 3, "ödev": 3,
                   "öğretmen": 3, "lgs": 3, "yks": 3, "tyt": 3, "ayt": 3},
}


def kategori_bul(s: str, cfg: Optional[Config] = None) -> str:
    esik = cfg.kategori_esik if cfg else 3
    s = normalize(s)
    skorlar: Dict[str, int] = {}
    for kat, kelimeler in KATEGORILER.items():
        sk = 0
        for kel, w in kelimeler.items():
            if " " in kel:
                if kel in s:
                    sk += w
            else:
                if len(kel) <= 3:
                    pat = rf"\b{re.escape(kel)}(?!\w)"
                else:
                    pat = rf"\b{re.escape(kel)}\w*"
                if re.search(pat, s):
                    sk += w
        if sk:
            skorlar[kat] = sk
    if not skorlar:
        return "genel"
    en_iyi = max(skorlar.values())
    return max(skorlar, key=skorlar.get) if en_iyi >= esik else "genel"


# ============================================================
# WEB TETİKLEYİCİLER
# ============================================================
WEB_TETIK_KALIPLARI = re.compile(
    r"\b("
    r"kim|kimdir|kimdi|nerede|neresi|nerde|"
    r"ne zaman|hangi yıl|hangi tarih|kaç yılında|"
    r"nedir|ne demek|ne anlama|nasıl yapılır|nasıl çalışır|"
    r"en son|güncel|son haber|haberler|"
    r"fiyatı|fiyat|kaç para|ne kadar|"
    r"skor|maç sonucu|kim kazandı|"
    r"hava durumu|hava nasıl|"
    r"bugün|dün|yarın|bu yıl|bu ay|bu hafta|"
    r"2024|2025|2026|2027|"
    r"araştır|bak|öğren|kontrol et"
    r")\b",
    re.IGNORECASE,
)

WEB_SKIP_KATEGORI = {
    "selamlasma", "veda", "tesekkur", "ozur", "duygu",
    "matematik",
}

WEB_ZORLA = re.compile(
    r"\b(araştır|internetten|güncel|son dakika|haber)\b",
    re.IGNORECASE)


def web_gerekli(soru: str, kategori: str) -> bool:
    s = tr_lower(soru).strip()
    if len(s) < 6:
        return False
    if kategori in WEB_SKIP_KATEGORI:
        return False
    if WEB_ZORLA.search(s):
        return True
    if "?" in soru and WEB_TETIK_KALIPLARI.search(s):
        return True
    if len(s.split()) >= 4 and WEB_TETIK_KALIPLARI.search(s):
        return True
    return False


# ============================================================
# KALIP ÖĞRENME
# ============================================================
SOSYAL_KATEGORILER = {"selamlasma", "veda", "tesekkur", "ozur", "duygu"}

OGRENME_TALIMATI: Dict[str, str] = {
    "selamlasma": (
        "Kullanıcı sana selamlaşma mesajı yazıyor: 'selam', 'merhaba', "
        "'naber', 'napıyorsun', 'nasılsın', 'iyi misin', 'hey', 'günaydın', "
        "'iyi akşamlar', 'iyi geceler'. Sen Türkçe konuşan, samimi, kısa "
        "(1-2 cümle) bir asistansın. Çok ÇEŞİTLİ cevaplar veriyorsun."
    ),
    "veda": (
        "Kullanıcı sana veda ediyor: 'hoşçakal', 'görüşürüz', 'güle güle', "
        "'bay bay', 'kendine iyi bak'. Sen Türkçe, samimi, kısa vedalaşma "
        "cevapları veriyorsun."
    ),
    "tesekkur": (
        "Kullanıcı sana teşekkür ediyor: 'teşekkür ederim', 'sağol', "
        "'eyvallah'. Sen Türkçe, samimi, kısa teşekkür karşılığı veriyorsun."
    ),
    "ozur": (
        "Kullanıcı senden özür diliyor: 'özür dilerim', 'pardon', "
        "'kusura bakma'. Sen Türkçe, samimi, kısa affetme cevabı veriyorsun."
    ),
    "duygu": (
        "Kullanıcı sana duygusunu anlatıyor: 'üzgünüm', 'moralim bozuk', "
        "'kötüyüm', 'yalnızım', 'stresliyim', 'mutluyum', 'harikayım'. "
        "Sen Türkçe, samimi, kısa empatik cevaplar veriyorsun."
    ),
}


def ogren_kategori(store: Store, cfg: Config, kategori: str,
                   adet: int = 20) -> int:
    if kategori not in OGRENME_TALIMATI:
        return -1
    if not cfg.api_key:
        return -2

    mevcut = [r["cevap"] for r in store.conn.execute(
        "SELECT cevap FROM kaliplar WHERE kategori=? LIMIT 40",
        (kategori,)).fetchall()]
    mevcut_ozet = "; ".join(mevcut) if mevcut else "(henüz yok)"

    prompt = (
        f"{OGRENME_TALIMATI[kategori]}\n\n"
        f"Şu ana kadar üretilmiş cevaplar (bunları TEKRAR ETME):\n"
        f"{mevcut_ozet}\n\n"
        f"Şimdi {adet} tane YENİ ve FARKLI cevap üret. "
        f"SADECE şu JSON'u döndür:\n"
        f'{{"cevaplar": ["c1", "c2", ...]}}\n'
        f"Kurallar:\n"
        f"- Her cevap 5-150 karakter arası\n"
        f"- Tek satır, doğal Türkçe, samimi\n"
        f"- Cevaplar birbirinden farklı olsun\n"
        f"- Numara/madde işareti kullanma"
    )

    mesajlar = [
        {"role": "system",
         "content": "Sadece geçerli JSON döndür. Başka hiçbir şey yazma."},
        {"role": "user", "content": prompt},
    ]

    for _ in range(2):
        try:
            rq = _session.post(
                cfg.api_url,
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json={
                    "model": cfg.model,
                    "messages": mesajlar,
                    "temperature": 0.95,
                    "max_tokens": 2500,
                    "response_format": {"type": "json_object"},
                },
                timeout=cfg.llm_timeout,
            )
        except requests.RequestException as e:
            log.warning("öğrenme isteği hatası: %s", e)
            time.sleep(2)
            continue

        if rq.status_code == 401:
            return -2
        if rq.status_code != 200:
            log.warning("öğrenme HTTP %d: %s", rq.status_code, rq.text[:200])
            time.sleep(2)
            continue

        try:
            ham = rq.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError):
            continue

        d = _json_ayikla(ham)
        if not d or not isinstance(d.get("cevaplar"), list):
            continue

        eklenen = 0
        for c in d["cevaplar"]:
            if not isinstance(c, str):
                continue
            c = c.strip().strip('"').strip("'").strip()
            if 5 <= len(c) <= 500 and store.kalip_ekle(kategori, c):
                eklenen += 1
        return eklenen

    return -3


# ============================================================
# KİŞİ BİLGİSİ
# ============================================================
_HARF = "a-zçğıöşü"
_ISIM_PAT = rf"[{_HARF}]+(?:\s+[{_HARF}]+){{0,3}}"

KISI_KALIPLARI: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(rf"^(?:benim\s+)?adım\s+({_ISIM_PAT})$"), "ad"),
    (re.compile(rf"^(?:benim\s+)?ismim\s+({_ISIM_PAT})$"), "ad"),
    (re.compile(r"^(?:benim\s+)?yaşım\s+(\d{1,3})$"), "yas"),
    (re.compile(rf"^(?:benim\s+)?şehrim\s+({_ISIM_PAT})$"), "sehir"),
    (re.compile(r"^(?:benim\s+)?mesleğim\s+(.{2,60})$"), "meslek"),
    (re.compile(r"^(?:benim\s+)?okulum\s+(.{2,60})$"), "okul"),
    (re.compile(r"^(?:benim\s+)?doğum günüm\s+(.{2,40})$"), "dogum"),
]

KISI_SORU_KALIPLARI: Dict[str, List[str]] = {
    "ad":     ["adım ne", "adım nedir", "ismim ne", "ismim nedir",
               "adımı biliyor musun", "benim adım ne"],
    "yas":    ["yaşım kaç", "yaşım ne", "kaç yaşındayım"],
    "sehir":  ["şehrim ne", "nerede yaşıyorum", "hangi şehirdeyim"],
    "meslek": ["mesleğim ne", "işim ne"],
    "okul":   ["okulum ne"],
    "dogum":  ["doğum günüm ne zaman"],
}

SORU_ISARETLERI = ("biliyor musun", "nedir", "neydi", "hatırlıyor musun",
                   "söyler misin", "kaç", "ne zaman", "nerede",
                   "mi", "mı", "mu", "mü")

KISI_ALANLARI = {"ad", "yas", "sehir", "meslek", "okul", "dogum"}

KISI_YASAK_DEGERLER = {
    "ne", "nedir", "neydi", "kim", "kaç", "nerede", "ne zaman",
    "biliyor", "hatırlıyor", "söyler", "musun", "misin",
}


def kisi_regex_kaydet(store: Store, cumle: str) -> Optional[str]:
    cumle_stripped = cumle.strip()
    c_low = tr_lower(cumle_stripped)
    if c_low.endswith("?"):
        return None
    if any(x in c_low for x in SORU_ISARETLERI) and \
       "adım" not in c_low and "ismim" not in c_low:
        return None
    for kalip, alan in KISI_KALIPLARI:
        m = kalip.match(c_low)
        if not m:
            continue
        start, end = m.start(1), m.end(1)
        deger = cumle_stripped[start:end].strip(".!? \t").strip()
        if not deger or len(deger) > 60:
            continue
        if tr_lower(deger) in KISI_YASAK_DEGERLER:
            continue
        if alan == "yas" and not deger.isdigit():
            continue
        store.kisi_set(alan, deger)
        return f"Tamam, {alan} = {deger} olarak kaydettim."
    return None


def kisi_sor(store: Store, cumle: str) -> Optional[str]:
    c = tr_lower(cumle)
    for alan, kaliplar in KISI_SORU_KALIPLARI.items():
        if any(k in c for k in kaliplar):
            v = store.kisi_get(alan)
            return (f"{alan}: {v}" if v
                    else f"{alan}: bilmiyorum, söylersen kaydederim.")
    return None


def kisi_llm_uygula(store: Store, kisi: Dict[str, Any]) -> None:
    for alan, deger in (kisi or {}).items():
        if alan not in KISI_ALANLARI or deger is None:
            continue
        metin = str(deger).strip()
        if not metin or len(metin) > 80:
            continue
        store.kisi_set(alan, metin)


# ============================================================
# LLM
# ============================================================
SISTEM_TEMEL = (
    "Sen 'Bebek' adlı Türkçe konuşan samimi bir asistansın. "
    "Kısa ve net cevap ver (genelde 1-3 cümle). "
    "Teknik/kod sorularında gerektiği kadar uzun olabilirsin, kod bloğu kullan. "
    "Bilmediğin şeyi uydurma; emin değilsen 'bilmiyorum' de. "
    "SADECE Türkçe cevap ver. "
    "Sana verilen konuşma geçmişindeki kişisel bilgileri (ad, yaş, şehir) hatırla. "
    "Yanıtını MUTLAKA şu JSON şemasında ver, başka hiçbir şey yazma:\n"
    '{"cevap": "<Türkçe cevabın>", '
    '"niyet": "<hal_hatir|yardim_teklif|bekleme|bitis|yok>", '
    '"kisi": {"ad": "...", "yas": 25, "sehir": "...", "meslek": "...", '
    '"okul": "...", "dogum": "..."}}\n'
    "Kurallar:\n"
    "- niyet: cevabında kullanıcıya nasıl olduğunu soruyorsan 'hal_hatir'; "
    "yardım teklif ediyorsan 'yardim_teklif'; aksi halde 'yok'.\n"
    "- kisi: kullanıcının mesajında geçen kişisel bilgi varsa doldur, "
    "yoksa boş obje {} koy. SADECE kullanıcının kendisiyle ilgili bilgi.\n"
)

MOD_EKLERI = {
    "komik":  " Üslubun esprili ve eğlenceli olsun.",
    "samimi": " Üslubun çok samimi ve içten olsun.",
    "ciddi":  " Üslubun ciddi ve resmi olsun.",
    "normal": "",
}


def sistem_prompt(mod: str = "normal") -> str:
    return SISTEM_TEMEL + MOD_EKLERI.get(mod, "")


@dataclass
class LLMYanit:
    cevap: Optional[str] = None
    niyet: Optional[str] = None
    kisi: Dict[str, Any] = field(default_factory=dict)
    hata: Optional[str] = None


def _json_ayikla(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start >= 0:
        derinlik = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                derinlik += 1
            elif ch == "}":
                derinlik -= 1
                if derinlik == 0:
                    aday = text[start:i + 1]
                    try:
                        v = json.loads(aday)
                        if isinstance(v, dict):
                            return v
                    except json.JSONDecodeError:
                        break
    return None


def _retry_after(headers: Dict[str, str], default: float) -> float:
    v = headers.get("Retry-After") or headers.get("retry-after")
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def llm(soru: str, cfg: Config,
        gecmis: Optional[List[Tuple[str, str]]] = None,
        mod: str = "normal") -> LLMYanit:
    if not cfg.api_key:
        return LLMYanit(hata="api_key_yok")

    mesajlar: List[Dict[str, str]] = [
        {"role": "system", "content": sistem_prompt(mod)}
    ]
    if gecmis:
        for s, c in gecmis[-cfg.gecmis_llm:]:
            mesajlar.append({"role": "user", "content": s})
            mesajlar.append({"role": "assistant", "content": c})
    mesajlar.append({"role": "user", "content": soru})

    son_hata: Optional[str] = None
    for deneme in range(cfg.llm_retry):
        try:
            rq = _session.post(
                cfg.api_url,
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json={
                    "model": cfg.model,
                    "messages": mesajlar,
                    "temperature": cfg.llm_temp,
                    "max_tokens": cfg.llm_max_tok,
                    "response_format": {"type": "json_object"},
                },
                timeout=cfg.llm_timeout,
            )
        except requests.RequestException as e:
            son_hata = f"ağ: {e}"
            log.warning("LLM isteği başarısız (deneme %d): %s", deneme + 1, e)
            time.sleep(min(2 ** deneme, 8))
            continue

        if rq.status_code == 401:
            return LLMYanit(hata="auth")
        if rq.status_code == 429:
            bekle = _retry_after(rq.headers, default=min(5 * (deneme + 1), 30))
            time.sleep(bekle)
            continue
        if rq.status_code >= 500:
            son_hata = f"http {rq.status_code}"
            time.sleep(min(2 ** deneme, 8))
            continue
        if rq.status_code != 200:
            son_hata = f"http {rq.status_code}"
            log.warning("LLM HTTP %d: %s", rq.status_code, rq.text[:200])
            time.sleep(1.5)
            continue

        try:
            ham = rq.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            son_hata = f"malformed: {e}"
            continue

        d = _json_ayikla(ham)
        if not d:
            return LLMYanit(cevap=ham.strip() or None)

        cevap = (d.get("cevap") or "").strip() or None
        niyet = (d.get("niyet") or "").strip()
        if niyet in ("yok", "", "none", "None"):
            niyet = None
        kisi = d.get("kisi") if isinstance(d.get("kisi"), dict) else {}
        return LLMYanit(cevap=cevap, niyet=niyet, kisi=kisi)

    return LLMYanit(hata=son_hata or "bilinmeyen")


# ============================================================
# BAĞLAM
# ============================================================
IYI_KELIMELER = ["iyiyim", "iyi", "fena değil", "süper", "harika",
                 "mükemmel", "iyilik", "şükür", "idare eder"]
KOTU_KELIMELER = ["kötü", "berbat", "fena", "yorgun", "üzgün"]
STRES_KELIMELER = ["sıkıldım", "stresli", "moralim bozuk", "yalnızım"]


def baglam_cevap(soru: str, son_niyet: Optional[str]
                 ) -> Tuple[Optional[str], Optional[str]]:
    if not son_niyet:
        return None, None
    s = tr_lower(soru)

    if son_niyet == "hal_hatir":
        if any(k in s for k in IYI_KELIMELER):
            return random.choice(["Sevindim!", "Güzel, ben de iyiyim.",
                                  "Harika!", "Ne güzel!", "Süper!"]), "bitis"
        if any(k in s for k in KOTU_KELIMELER):
            return random.choice(["Üzüldüm, ne oldu?",
                                  "Hayırdır, bir sorun mu var?",
                                  "Anlatmak ister misin?",
                                  "Geçmiş olsun."]), "moral_sor"
        if any(k in s for k in STRES_KELIMELER):
            return random.choice(["Ne oldu?", "Anlat bakalım.", "Dinliyorum...",
                                  "İstersen konuşabiliriz."]), "moral_sor"

    if son_niyet == "yardim_teklif":
        if any(k in s for k in ["yok", "hayır", "hayir", "gerek yok"]):
            return random.choice(["Tamam!", "Peki, ne zaman istersen.",
                                  "Anytime 👍"]), "bitis"
        if any(k in s for k in ["evet", "var", "tabi", "olur"]):
            return "Buyur, dinliyorum.", "bekleme"
    return None, None


# ============================================================
# NOT
# ============================================================
NOT_EKLE_RE_LOW = re.compile(
    r"^not\s*(?:(?:ekle|al)\b\s*[:\-]?\s*|[:\-]\s*)(.+)$"
)


def not_ekle_komut(store: Store, soru_orig: str) -> Optional[str]:
    cumle_stripped = soru_orig.strip()
    c_low = tr_lower(cumle_stripped)
    m = NOT_EKLE_RE_LOW.match(c_low)
    if not m:
        return None
    start, end = m.start(1), m.end(1)
    metin = cumle_stripped[start:end].strip()
    if not metin:
        return "Boş not eklenmez."
    store.not_ekle(metin)
    return f"Not eklendi: {metin}"


def notlari_goster(store: Store) -> str:
    n = store.not_sayisi()
    if n == 0:
        return "Henüz not yok."
    liste = store.not_listele(20)
    out = [f"{n} not:"]
    for i, x in enumerate(reversed(liste), 1):
        out.append(f"  {i}. {x['metin']}  ({x['tarih']})")
    return "\n".join(out)


# ============================================================
# BOT
# ============================================================
ONEMSIZ_CEVAPLAR_HAM = {"evet", "hayır", "tamam", "ok", "peki", "anladım",
                        "bilmiyorum"}
ONEMSIZ_NORMALIZE = {normalize(x) for x in ONEMSIZ_CEVAPLAR_HAM}


def _onemsiz(cevap: str) -> bool:
    return normalize(cevap) in ONEMSIZ_NORMALIZE


class Bot:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg
        self.cfg.web_aktif = (store.ayar_get("web_aktif", "1") == "1")

    def cevapla(self, soru: str, son_niyet: Optional[str]
                ) -> Tuple[str, Optional[str]]:
        s = tr_lower(soru).strip()
        if not s:
            return "Bir şey söylemedin.", None

        # 1) kişi regex kaydet
        k = kisi_regex_kaydet(self.store, soru)
        if k:
            return k, None

        # 2) kişi sor
        k = kisi_sor(self.store, soru)
        if k:
            return k, None

        # 3) bağlam
        b, niyet = baglam_cevap(soru, son_niyet)
        if b:
            return b, niyet

        # 4) sosyal kategori — kalıptan cevap
        kat = kategori_bul(soru, self.cfg)
        if kat in SOSYAL_KATEGORILER:
            havuz = self.store.kalip_sayisi(kat)
            if havuz < self.cfg.kalip_min_havuz:
                print(f"[{kat} öğreniyorum, birkaç saniye...]", flush=True)
                eklenen = ogren_kategori(self.store, self.cfg, kat,
                                         adet=self.cfg.kalip_hedef)
                if eklenen > 0:
                    print(f"[+{eklenen} yeni cevap öğrendim]", flush=True)
                elif eklenen == -2:
                    print("[API anahtarı yok — öğrenemiyorum]", flush=True)
                elif eklenen == -3:
                    print("[öğrenme başarısız — normal akışa dönüyorum]",
                          flush=True)
            kaliptan = self.store.kalip_rastgele(kat)
            if kaliptan:
                return kaliptan, None

        # 5) saat / tarih
        if "saat" in s and ("kaç" in s or "ne" in s):
            return "Saat: " + dt.datetime.now().strftime("%H:%M:%S"), None
        if ("tarih" in s and ("ne" in s or "bugün" in s)) or "bugün ayın" in s:
            return "Tarih: " + dt.datetime.now().strftime("%d %B %Y, %A"), None

        # 6) notlar
        n = not_ekle_komut(self.store, soru)
        if n is not None:
            return n, None
        if s in ("notlar", "notları göster", "notlarım", "notlarımı göster"):
            return notlari_goster(self.store), None

        # 7) matematik
        if (re.search(r"\d\s*[+\-*/×÷xX^]\s*\d", soru)
                or "karekök" in s or "karekok" in s or "yüzde" in s
                or re.search(r"\(\s*-?\d", soru)):
            mat = matematik_coz(soru)
            if mat:
                return mat, None

        # 8) öğretilen
        ogr = self.store.ogretilen_ara(soru)
        if ogr:
            return ogr, None

        # 9) önbellek
        cache, _ = self.store.cache_cevap(soru)
        if cache:
            return cache, None

        # 9.5) OTOMATİK WEB ARAMA
        if self.cfg.web_aktif and web_gerekli(soru, kat):
            web_cevap = self._web_cevapla(soru, kat)
            if web_cevap:
                return web_cevap, None

        # 10) LLM
        if not self.cfg.api_key:
            return "API anahtarı yok, sadece yerel bilgilerle çalışıyorum.", None

        mod = self.store.ayar_get("mod", "normal")
        yanit = llm(soru, self.cfg,
                    gecmis=self.store.gecmis_son(self.cfg.gecmis_llm),
                    mod=mod)

        if yanit.hata == "auth":
            return "API anahtarı geçersiz görünüyor.", None
        if not yanit.cevap:
            log.warning("LLM başarısız: %s", yanit.hata)
            return "Şu an cevap veremiyorum, tekrar dener misin?", None

        if yanit.kisi:
            kisi_llm_uygula(self.store, yanit.kisi)

        if (len(yanit.cevap) >= self.cfg.cache_min_len
                and not _onemsiz(yanit.cevap)):
            self.store.cache_kaydet(
                soru, yanit.cevap, kategori_bul(soru, self.cfg))

        if mod == "komik" and random.random() < 0.15 \
                and not yanit.cevap.endswith("😄"):
            yanit.cevap += " 😄"

        return yanit.cevap, yanit.niyet

    # ---------- WEB CEVAP ----------
    def _web_cevapla(self, soru: str, kategori: str) -> Optional[str]:
        print("[internetten araştırıyorum...]", flush=True)
        web = WebArama(self.cfg, self.store)
        sonuclar = web.ara(soru, limit=self.cfg.web_limit)
        if not sonuclar:
            print("[web'de net sonuç bulamadım]", flush=True)
            return None

        if self.cfg.api_key:
            ozet = self._web_ozetle(soru, sonuclar)
            if ozet:
                self.store.cache_kaydet(soru, ozet, kategori="web")
                return ozet

        return self._web_ham(soru, sonuclar)

    def _web_ozetle(self, soru: str,
                    sonuclar: List[WebSonuc]) -> Optional[str]:
        kaynaklar = "\n\n".join(
            f"[{i+1}] {s.baslik}\n{s.snippet}\n({s.url})"
            for i, s in enumerate(sonuclar)
        )
        prompt = (
            f"Kullanıcının sorusu: {soru}\n\n"
            f"Web aramasından gelen sonuçlar:\n{kaynaklar}\n\n"
            f"Yukarıdaki bilgilerle kullanıcıya TÜRKÇE, doğal, samimi bir "
            f"cevap yaz. 2-5 cümle yeterli. Başında 'internetten baktım' "
            f"gibi doğal bir giriş kullanabilirsin. Uydurma — sadece "
            f"verilen bilgiyi kullan. Emin değilsen 'net bilgi bulamadım' de. "
            f"SADECE şu JSON'u döndür: "
            f'{{"cevap": "<Türkçe cevap>"}}'
        )
        try:
            rq = _session.post(
                self.cfg.api_url,
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                json={
                    "model": self.cfg.model,
                    "messages": [
                        {"role": "system",
                         "content": "Sadece geçerli JSON döndür."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.4,
                    "max_tokens": self.cfg.llm_max_tok,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.cfg.llm_timeout,
            )
        except requests.RequestException as e:
            log.warning("web özet hatası: %s", e)
            return None
        if rq.status_code != 200:
            return None
        try:
            ham = rq.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError):
            return None
        d = _json_ayikla(ham)
        if d and isinstance(d.get("cevap"), str):
            return d["cevap"].strip() or None
        return None

    def _web_ham(self, soru: str,
                 sonuclar: List[WebSonuc]) -> str:
        satirlar = [f"İnternetten bulduklarım ({soru}):"]
        for i, s in enumerate(sonuclar[:3], 1):
            satirlar.append(f"{i}. {s.baslik} — {s.snippet}")
            satirlar.append(f"   {s.url}")
        return "\n".join(satirlar)


# ============================================================
# KOMUTLAR
# ============================================================
KOMUT_YARDIM = """Komutlar:
  /yardim                              bu yardım
  /liste                               kategoriler + kalıplar
  /stat                                genel istatistikler
  /bilgi veya /kisi                    kayıtlı kişisel bilgiler
  /notlar                              notları göster
  /temizle                             sohbet geçmişini sil
  /mod komik|samimi|ciddi|normal       üslup modu
  /ogret soru => cevap                 manuel öğret
  /sil <soru>                          önbellekten sil
  /web <sorgu>                         elle web ara
  /webon | /weboff                     otomatik web aramayı aç/kapat
  /webdurum                            web arama durumu
  /webtemizle                          web önbelleğini sil
  /cik veya /exit                      çıkış"""


def komut_calistir(s: str, store: Store) -> Optional[bool]:
    low = tr_lower(s).strip()

    if low in ("/cik", "/exit", "exit", "quit"):
        return False

    if low in ("/yardim", "/help", "help", "?"):
        print(KOMUT_YARDIM)
        return True

    if low == "/liste":
        d = store.kategori_dagilimi()
        print(f"[{store.cache_sayisi()} soru, {len(d)} kategori]")
        for kat, sayi in d:
            print(f"  • {kat}: {sayi} soru")
        kd = store.kalip_dagilimi()
        if kd:
            print(f"\n[kalıplar: {store.kalip_sayisi()} cevap]")
            for kat, sayi in kd:
                print(f"  • {kat}: {sayi} cevap")
        return True

    if low == "/stat":
        aktif_web = store.ayar_get("web_aktif", "1") == "1"
        print(f"[önbellek]  {store.cache_sayisi()} soru")
        print(f"[kategori]  {len(store.kategori_dagilimi())}")
        print(f"[sohbet]    {store.gecmis_sayisi()}")
        print(f"[not]       {store.not_sayisi()}")
        print(f"[öğretilen] {store.ogretilen_sayisi()}")
        print(f"[kalıp]     {store.kalip_sayisi()} cevap "
              f"({len(store.kalip_dagilimi())} kategori)")
        print(f"[web]       {store.web_cache_sayisi()} sorgu "
              f"({'açık' if aktif_web else 'kapalı'})")
        print(f"[kişi]      {store.kisi_tumu()}")
        print(f"[mod]       {store.ayar_get('mod', 'normal')}")
        print(f"[fts5]      {store.has_fts5}")
        print(f"[ddgs]      {HAS_DDGS}")
        return True

    if low in ("/bilgi", "/kisi"):
        k = store.kisi_tumu()
        if not k:
            print("(kişisel bilgi yok)")
        else:
            for a, v in k.items():
                print(f"  • {a}: {v}")
        return True

    if low == "/notlar":
        print(notlari_goster(store))
        return True

    if low == "/temizle":
        store.gecmis_temizle()
        print("[sohbet geçmişi silindi]")
        return True

    if low.startswith("/mod "):
        yeni = s[5:].strip() or "normal"
        store.ayar_set("mod", yeni)
        print(f"[mod: {yeni}]")
        return True

    if low.startswith("/ogret "):
        try:
            q, a = s[7:].split("=>", 1)
            q, a = q.strip(), a.strip()
            if not q or not a:
                raise ValueError
            store.ogret(q, a)
            print(f"[öğretildi] {q}")
        except ValueError:
            print("kullanım: /ogret soru => cevap")
        return True

    if low.startswith("/web "):
        sorgu = s[5:].strip()
        if not sorgu:
            print("kullanım: /web <sorgu>")
            return True
        cfg = Config.from_env()
        cfg.db_path = store.cfg.db_path
        web = WebArama(cfg, store)
        sonuc = web.ara(sorgu)
        if not sonuc:
            print("[sonuç yok]")
        else:
            for i, x in enumerate(sonuc, 1):
                print(f"{i}. {x.baslik}")
                print(f"   {x.snippet}")
                print(f"   {x.url}")
        return True

    if low == "/webon":
        store.ayar_set("web_aktif", "1")
        print("[otomatik web arama AÇIK]")
        return True

    if low == "/weboff":
        store.ayar_set("web_aktif", "0")
        print("[otomatik web arama KAPALI]")
        return True

    if low == "/webdurum":
        aktif = store.ayar_get("web_aktif", "1") == "1"
        print(f"[web] {'AÇIK' if aktif else 'KAPALI'} "
              f"| önbellek: {store.web_cache_sayisi()} sorgu "
              f"| ddgs paketi: {HAS_DDGS}")
        return True

    if low == "/webtemizle":
        n = store.web_cache_temizle()
        print(f"[web önbelleği silindi: {n}]")
        return True

    if low.startswith("/sil "):
        silinen = store.cache_sil(s[5:])
        if silinen:
            print(f"[silindi] {silinen}")
        else:
            print("[bulunamadı]")
        return True

    return None


# ============================================================
# ANA DÖNGÜ
# ============================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Bebek Ultra v{SURUM} — web aramalı akıllı bot")
    parser.add_argument("--db", help="SQLite dosyası")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--test", action="store_true",
                        help="Self-test çalıştır ve çık")
    parser.add_argument("--version", action="version",
                        version=f"bebek {SURUM}")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if args.test:
        logging.disable(logging.CRITICAL)
        return run_tests()

    cfg = Config.from_env()
    if args.db:
        cfg.db_path = args.db

    store = Store(cfg.db_path, cfg)
    atexit.register(store.kapat)

    if not cfg.api_key:
        log.warning("GROQ_API_KEY tanımlı değil — yerel özellikler çalışır")

    print("=" * 58)
    print(f"  BEBEK ULTRA v{SURUM}  (web aramalı)")
    print("=" * 58)
    print(f"DB:       {cfg.db_path}")
    print(f"Önbellek: {store.cache_sayisi()} soru")
    print(f"Kalıp:    {store.kalip_sayisi()} cevap")
    print(f"Web:      {'açık' if store.ayar_get('web_aktif','1')=='1' else 'kapalı'}"
          f"  ({store.web_cache_sayisi()} sorgu önbellekte, "
          f"ddgs={'var' if HAS_DDGS else 'yok'})")
    print(f"FTS5:     {'aktif' if store.has_fts5 else 'kapalı'}")
    print(f"Fuzzy:    {'rapidfuzz' if HAS_RAPIDFUZZ else 'difflib (yavaş)'}")
    print(f"LLM:      {'hazır' if cfg.api_key else 'kapalı'}")
    print(f"Mod:      {store.ayar_get('mod', 'normal')}")
    print()
    print(KOMUT_YARDIM)

    bot = Bot(store, cfg)

    while True:
        try:
            s = input("\nSen : ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not s:
            continue

        sonuc = komut_calistir(s, store)
        if sonuc is False:
            break
        if sonuc is True:
            # ayar değişmiş olabilir, botu yenile
            bot.cfg.web_aktif = (store.ayar_get("web_aktif", "1") == "1")
            continue

        try:
            son_niyet = store.niyet_son()
            cevap, niyet = bot.cevapla(s, son_niyet)
            print(f"Bot : {cevap}")
            store.gecmis_ekle(s, cevap, niyet)
            store.niyet_push(niyet)
        except Exception as e:
            log.exception("cevapla hatası: %s", e)
            print("[beklenmeyen hata — log'a bak]")

    return 0


# ============================================================
# SELF-TEST
# ============================================================
def run_tests() -> int:
    import tempfile

    print("Self-test başlıyor...")
    hata = 0

    def check(ad: str, kosul: bool, mesaj: str = "") -> None:
        nonlocal hata
        if kosul:
            print(f"  ✓ {ad}")
        else:
            hata += 1
            print(f"  ✗ {ad} {mesaj}")

    check("tr_lower İ", tr_lower("İSTANBUL") == "istanbul")
    check("tr_lower I", tr_lower("IĞDIR") == "ığdır")
    check("normalize noktalama",
          normalize("Merhaba, dünya!") == "merhaba dunya"
          or normalize("Merhaba, dünya!") == "merhaba dünya")

    check("toplama", matematik_coz("5 + 5") == "5 + 5 = 10")
    check("çarpma", "20" in (matematik_coz("4 * 5") or ""))
    check("karekök", matematik_coz("16 karekök") is not None)
    check("yüzde", "10" in (matematik_coz("100'ün yüzde 10'u") or ""))

    try:
        guvenli_eval("__import__('os').system('echo hi')")
        check("eval güvenliği", False)
    except Exception:
        check("eval güvenliği", True)

    d = _json_ayikla('```json\n{"cevap":"merhaba"}\n```')
    check("json fence", d == {"cevap": "merhaba"})
    d = _json_ayikla('{"cevap":"a} b"}')
    check("json iç brace", d == {"cevap": "a} b"})

    check("fts quote", _fts_quote('a"b') == '"a""b"')

    # web tetikleyici testleri
    check("web tetik: selam", not web_gerekli("selam", "selamlasma"))
    check("web tetik: matematik", not web_gerekli("5+5", "matematik"))
    check("web tetik: atatürk",
          web_gerekli("Atatürk ne zaman doğdu?", "tarih"))
    check("web tetik: araştır",
          web_gerekli("araştır: kuantum bilgisayar", "genel"))

    # _strip_html / _kisalt
    check("strip_html", _strip_html("<b>merhaba</b> <i>dünya</i>")
          == "merhaba dünya")
    check("kisalt", _kisalt("abcdef", 4) == "abc…")
    check("kisalt kısa", _kisalt("ab", 5) == "ab")

    # --- depo ---
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        cfg = Config(db_path=tmp.name)
        st = Store(tmp.name, cfg)
        st.cache_kaydet("Python nedir?",
                        "Python bir programlama dilidir, çok popülerdir.")
        c, _ = st.cache_cevap("Python nedir")
        check("cache kaydet/bul", c is not None)

        st.kisi_set("ad", "Ali")
        check("kisi kaydet", st.kisi_get("ad") == "Ali")

        st.not_ekle("süt al")
        check("not ekle", st.not_sayisi() == 1)

        ok1 = st.kalip_ekle("selamlasma", "Merhaba, hoş geldin!")
        ok2 = st.kalip_ekle("selamlasma", "Selam, nasılsın?")
        ok3 = st.kalip_ekle("selamlasma", "Merhaba, hoş geldin!")
        check("kalip ekle", ok1 and ok2)
        check("kalip duplicate engel", not ok3)
        check("kalip sayısı", st.kalip_sayisi("selamlasma") == 2)
        r = st.kalip_rastgele("selamlasma")
        check("kalip rastgele", r in ("Merhaba, hoş geldin!",
                                      "Selam, nasılsın?"))

        # web cache
        st.web_cache_set("atatürk", [{"baslik": "Atatürk",
                                      "url": "http://x",
                                      "snippet": "1881",
                                      "kaynak": "test"}])
        wc = st.web_cache_get("atatürk")
        check("web cache set/get",
              wc is not None and wc[0]["baslik"] == "Atatürk")
        check("web cache sayısı", st.web_cache_sayisi() == 1)
        st.kapat()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # --- kişi regex ---
    tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp2.close()
    try:
        cfg2 = Config(db_path=tmp2.name)
        st2 = Store(tmp2.name, cfg2)
        kisi_regex_kaydet(st2, "adım Mehmet")
        check("kisi case korunur", st2.kisi_get("ad") == "Mehmet")
        kisi_regex_kaydet(st2, "adım ne")
        check("kisi soru filtresi", st2.kisi_get("ad") == "Mehmet")
        kisi_regex_kaydet(st2, "adım Mehmet Ali Yılmaz")
        check("kisi 3 kelime", st2.kisi_get("ad") == "Mehmet Ali Yılmaz")
        st2.kapat()
    finally:
        try:
            os.unlink(tmp2.name)
        except OSError:
            pass

    # --- not ---
    tmp3 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp3.close()
    try:
        cfg3 = Config(db_path=tmp3.name)
        st3 = Store(tmp3.name, cfg3)
        n = not_ekle_komut(st3, "Not ekle: Süt Al")
        check("not case", n is not None and "Süt Al" in n)
        check("not aldım değil",
              not_ekle_komut(st3, "not aldım") is None)
        check("not: süt", not_ekle_komut(st3, "not: ekmek") is not None)
        st3.kapat()
    finally:
        try:
            os.unlink(tmp3.name)
        except OSError:
            pass

    # --- entegrasyon ---
    tmp4 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp4.close()
    try:
        cfg4 = Config(db_path=tmp4.name, api_key="", web_aktif=False)
        st4 = Store(tmp4.name, cfg4)
        bot = Bot(st4, cfg4)

        c, _ = bot.cevapla("adım Ayşe", None)
        check("bot kisi kaydet", st4.kisi_get("ad") == "Ayşe")

        c, _ = bot.cevapla("5 + 3", None)
        check("bot matematik", "8" in c)

        c, _ = bot.cevapla("not ekle: yarın spor", None)
        check("bot not ekle", st4.not_sayisi() == 1)

        st4.kalip_ekle("selamlasma", "Merhaba, hoş geldin!")
        st4.kalip_ekle("selamlasma", "Selam, nasılsın?")
        st4.kalip_ekle("selamlasma", "Hey, naber?")
        st4.kalip_ekle("selamlasma", "Merhabalar!")
        st4.kalip_ekle("selamlasma", "Selamlar!")
        c, _ = bot.cevapla("selam", None)
        check("bot sosyal kalıptan",
              c in ("Merhaba, hoş geldin!", "Selam, nasılsın?",
                    "Hey, naber?", "Merhabalar!", "Selamlar!"))

        st4.ogret("merhaba bebek", "selam insan")
        c, _ = bot.cevapla("merhaba bebek", None)
        check("bot öğretilen", c == "selam insan")

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            r = komut_calistir("/stat", st4)
        check("komut /stat", r is True and "kalıp" in buf.getvalue())

        buf = io.StringIO()
        with redirect_stdout(buf):
            r = komut_calistir("/webdurum", st4)
        check("komut /webdurum", r is True and "web" in buf.getvalue())

        r = komut_calistir("/cik", st4)
        check("komut /cik", r is False)

        st4.kapat()
    finally:
        try:
            os.unlink(tmp4.name)
        except OSError:
            pass

    # --- kategori ---
    check("kategori kod",
          kategori_bul("python fonksiyon nasıl yazılır") == "kod")
    check("kategori genel", kategori_bul("bugün hava nasıl") == "genel")
    check("kategori selam", kategori_bul("selam naber") == "selamlasma")
    check("kategori veda", kategori_bul("hoşçakal") == "veda")

    print()
    if hata:
        print(f"❌ {hata} test başarısız")
        return 1
    print("✅ Tüm testler geçti")
    return 0


if __name__ == "__main__":
    sys.exit(main())
