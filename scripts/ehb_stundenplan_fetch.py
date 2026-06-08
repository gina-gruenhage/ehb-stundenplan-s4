#!/usr/bin/env python3
"""
EHB Hebammen-Stundenplan → strukturiertes JSON.

Laedt den sked-campus HTML-Wochenplan der Evangelischen Hochschule Berlin
und parst alle Veranstaltungen aller Wochen in eine JSON-Liste.

Jede Veranstaltung enthaelt:
  - date        (YYYY-MM-DD)
  - start, end  (HH:MM)
  - title       (Langtitel incl. Modul-Code)
  - lecturer
  - room
  - modul_code  (z.B. "84-993-20212-2060-V3")
  - beschreibung
  - groups      (Liste erkannter Gruppen-Marker, leer = Plenum)
  - raw_text    (Originalzellen-Text, fuer Debugging)

Usage:
    python3 execution/ehb_stundenplan_fetch.py
    python3 execution/ehb_stundenplan_fetch.py --url <URL> --out <pfad.json>

Default-URL: H 4. Semester SoSe 26.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

DEFAULT_URL = "https://www.eh-berlin.de/stundenplan/Studierende/HTML/H_4_H4.html"
DEFAULT_OUT = Path("tmp/ehb_events.json")

# Default python-requests Header werden vom EHB-Edge mit 415 abgewiesen,
# wenn die Anfrage von einer Cloud-IP (z.B. GitHub-Actions-Runner) kommt.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

GROUP_RE = re.compile(r"(?:Gr\.?|Gruppe)\s*([A-D]|[1-3][ab])", re.IGNORECASE)
# Eine Zeile, die nur Gruppen-Marker enthaelt (z.B. "Gr. A", "Gr. 1a, Gr. 1b")
GROUP_ONLY_LINE_RE = re.compile(
    r"^\s*(?:(?:Gr\.?|Gruppe)\s*(?:[A-D]|[1-3][ab])\s*[,;/]?\s*)+$",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")
MODUL_CODE_RE = re.compile(r"\d{2}-\d{3}-\d{5}-\d{4}-[A-Z]\d+")


def fetch_html(url: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30, headers=BROWSER_HEADERS)
            if r.status_code >= 400:
                # Block-Body fuer Debug ausgeben (z.B. WAF-Reason bei 415).
                print(
                    f"[ehb] HTTP {r.status_code} response headers: {dict(r.headers)}",
                    file=sys.stderr,
                )
                print(
                    f"[ehb] HTTP {r.status_code} body (first 500): {r.text[:500]!r}",
                    file=sys.stderr,
                )
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def parse_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    dd, mm, yy = m.groups()
    return f"20{yy}-{mm}-{dd}"


def extract_day_columns(header_row: Tag) -> list[tuple[int, int, str]]:
    """Gibt [(col_start, col_end, date_iso), ...] fuer die Tages-Header zurueck."""
    ranges: list[tuple[int, int, str]] = []
    col = 0
    for td in header_row.find_all("td", recursive=False):
        cs = int(td.get("colspan", 1) or 1)
        classes = td.get("class") or []
        if "t" in classes:
            d = parse_date(td.get_text(" ", strip=True))
            if d:
                ranges.append((col, col + cs - 1, d))
        col += cs
    return ranges


def col_to_date(col: int, day_ranges: list[tuple[int, int, str]]) -> str | None:
    for start, end, d in day_ranges:
        if start <= col <= end:
            return d
    return None


def parse_event_cell(td: Tag, iso_date: str, footnotes: dict[str, str] | None = None) -> dict:
    footnotes = footnotes or {}
    # Zellen-Inhalt ist durch <br/> getrennt:
    # Zeile 0: "8:30 - 11:45 Uhr"
    # Zeile 1: "V HW 2.6.3 Paediatrie II/Vorlesung"
    # Zeile 2: "Dr. Stiff"
    # Zeile 3: "E 207"
    # Zeile 4: "84-993-20212-2060-V3"
    # Zeile 5: "Paediatrische Betreuung ... Seminar"
    parts: list[str] = []
    current = ""
    for node in td.children:
        if getattr(node, "name", None) == "br":
            parts.append(current.strip())
            current = ""
        else:
            current += node.get_text() if hasattr(node, "get_text") else str(node)
    if current.strip():
        parts.append(current.strip())

    raw_text = " | ".join(parts)

    start = end = ""
    if parts:
        m = TIME_RE.search(parts[0])
        if m:
            sh, sm, eh, em = m.groups()
            start = f"{int(sh):02d}:{sm}"
            end = f"{int(eh):02d}:{em}"

    # Gruppen einsammeln und reine Gruppen-Zeilen aus der Struktur entfernen,
    # damit sie nicht faelschlich als Dozent/Raum interpretiert werden.
    group_hits: set[str] = set()
    structural_parts: list[str] = []
    for p in parts:
        for gm in GROUP_RE.finditer(p):
            g = gm.group(1)
            group_hits.add(g.lower() if g[0].isdigit() else g.upper())
        if GROUP_ONLY_LINE_RE.match(p):
            continue  # Zeile besteht nur aus Gruppen-Markern → nicht strukturell
        structural_parts.append(p)

    # structural_parts Layout:
    #   [0] Zeit
    #   [1] Titel
    #   [2..n-1] Dozent(en), Raum, Modul-Code
    #   [n] Beschreibung (nach Modul-Code)
    # Wir verankern am Modul-Code:
    code_idx = next(
        (i for i, p in enumerate(structural_parts) if MODUL_CODE_RE.search(p)),
        -1,
    )

    title = structural_parts[1] if len(structural_parts) > 1 else ""
    lecturer = ""
    room = ""
    modul_code = ""
    beschreibung = ""

    if code_idx >= 0:
        modul_code = structural_parts[code_idx]
        # Alles zwischen Titel und Modul-Code ist Dozent + Raum.
        # Konvention: vorletztes Element vor Code = Raum, Rest dazwischen = Dozent(en).
        middle = structural_parts[2:code_idx]
        if len(middle) == 0:
            pass
        elif len(middle) == 1:
            # nur Dozent, kein Raum (oder umgekehrt) — wir nehmen an: Dozent
            lecturer = middle[0]
        else:
            room = middle[-1]
            lecturer = ", ".join(middle[:-1])
        if code_idx + 1 < len(structural_parts):
            beschreibung = " ".join(structural_parts[code_idx + 1 :])
    else:
        # Fallback: altes Positions-Schema
        lecturer = structural_parts[2] if len(structural_parts) > 2 else ""
        room = structural_parts[3] if len(structural_parts) > 3 else ""
        beschreibung = structural_parts[4] if len(structural_parts) > 4 else ""

    # Fussnoten aufloesen: Referenzen [N] im Text → Fussnoten-Texte.
    # Verwendet, um fehlende Raumangaben ("Online") aus Fussnoten zu ergaenzen.
    referenced_notes: list[str] = []
    for p in parts:
        for m in FOOTNOTE_REF_RE.finditer(p):
            note = footnotes.get(m.group(1))
            if note and note not in referenced_notes:
                referenced_notes.append(note)

    # Auch Gruppen aus Fussnoten einsammeln (z.B. "[5] Gruppe A / Online")
    for note in referenced_notes:
        for gm in GROUP_RE.finditer(note):
            g = gm.group(1)
            group_hits.add(g.lower() if g[0].isdigit() else g.upper())

    # Raum-Fallback: wenn leer und Fussnote enthaelt "Online" → "Online"
    if not room:
        for note in referenced_notes:
            if re.search(r"\bOnline\b", note, re.IGNORECASE):
                room = "Online"
                break

    return {
        "date": iso_date,
        "start": start,
        "end": end,
        "title": title,
        "lecturer": lecturer,
        "room": room,
        "modul_code": modul_code,
        "beschreibung": beschreibung,
        "groups": sorted(group_hits),
        "footnotes": referenced_notes,
        "raw_text": raw_text,
    }


FOOTNOTE_RE = re.compile(r"^\s*\[(\d+)\]\s*(.+?)\s*$")
FOOTNOTE_REF_RE = re.compile(r"\[(\d+)\]")


def extract_footnotes(table: Tag) -> dict[str, str]:
    """Sammelt alle <td class='fn'>[N] text</td> Fussnoten der Wochen-Tabelle."""
    notes: dict[str, str] = {}
    for td in table.find_all("td", class_="fn"):
        m = FOOTNOTE_RE.match(td.get_text(" ", strip=True))
        if m:
            notes[m.group(1)] = m.group(2)
    return notes


def parse_week_table(table: Tag) -> list[dict]:
    rows = table.find_all("tr", recursive=False)
    if not rows:
        return []
    day_ranges = extract_day_columns(rows[0])
    if not day_ranges:
        return []

    footnotes = extract_footnotes(table)
    events: list[dict] = []
    # Grid-Tracking: Set aller belegten (row_idx, col_idx) durch rowspan-Zellen aus vorigen Reihen.
    occupied: set[tuple[int, int]] = set()

    for row_idx, tr in enumerate(rows[1:]):
        col = 0
        for td in tr.find_all("td", recursive=False):
            while (row_idx, col) in occupied:
                col += 1
            rs = int(td.get("rowspan", 1) or 1)
            cs = int(td.get("colspan", 1) or 1)
            classes = td.get("class") or []
            if "v" in classes:
                iso = col_to_date(col, day_ranges)
                if iso:
                    ev = parse_event_cell(td, iso, footnotes)
                    events.append(ev)
            for dr in range(rs):
                for dc in range(cs):
                    occupied.add((row_idx + dr, col + dc))
            col += cs
    return events


def parse_all(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    # Jede Woche: <div class='w2'>...</div> gefolgt von einer <table>.
    all_events: list[dict] = []
    for table in soup.find_all("table"):
        # Nur Tabellen mit Tages-Header (class='t' colspan='3') beruecksichtigen
        first_row = table.find("tr")
        if not first_row:
            continue
        has_day_headers = any(
            "t" in (td.get("class") or []) and int(td.get("colspan", 1) or 1) == 3
            for td in first_row.find_all("td", recursive=False)
        )
        if not has_day_headers:
            continue
        all_events.extend(parse_week_table(table))
    # Duplikate vermeiden (sollte nicht vorkommen, aber sicher ist sicher)
    seen = set()
    unique: list[dict] = []
    for ev in all_events:
        key = (ev["date"], ev["start"], ev["title"], ev["room"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)
    unique.sort(key=lambda e: (e["date"], e["start"]))
    return unique


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--out", default=str(DEFAULT_OUT), type=Path)
    p.add_argument("--html-file", type=Path, help="Lokale HTML-Datei statt URL")
    args = p.parse_args()

    if args.html_file:
        html = Path(args.html_file).read_text(encoding="utf-8")
    else:
        html = fetch_html(args.url)

    events = parse_all(html)

    # Schutz gegen WAF-/Block-Seiten: Der EHB-Edge liefert Cloud-IPs
    # (z.B. GitHub-Actions-Runner) teils HTTP 200 mit einer Seite OHNE
    # Stundenplan-Tabelle. parse_all() findet dann 0 Events. Ein leerer
    # Plan ist mitten im Semester nie legitim — hart abbrechen, damit der
    # nachgelagerte ICS-Schritt die guten Kalender nicht mit leeren
    # ueberschreibt und published.
    if not args.html_file and not events:
        print(
            "[ehb] FEHLER: 0 Veranstaltungen geparst — vermutlich WAF-/Block-Seite "
            "statt Stundenplan. Breche ab, ohne JSON zu schreiben.",
            file=sys.stderr,
        )
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "source": args.url if not args.html_file else str(args.html_file),
                "fetched": date.today().isoformat(),
                "event_count": len(events),
                "events": events,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Kurze Zusammenfassung auf stderr
    print(f"[ehb] {len(events)} Veranstaltungen → {out_path}", file=sys.stderr)
    with_groups = [e for e in events if e["groups"]]
    print(f"[ehb] davon mit Gruppen-Marker: {len(with_groups)}", file=sys.stderr)
    all_groups = sorted({g for e in events for g in e["groups"]})
    print(f"[ehb] erkannte Gruppen: {all_groups}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
