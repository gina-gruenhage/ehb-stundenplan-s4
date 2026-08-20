#!/usr/bin/env python3
"""
EHB Stundenplan JSON → ICS-Feeds pro Gruppe.

Liest das von ehb_stundenplan_fetch.py erzeugte JSON und schreibt pro Gruppe
eine ICS-Datei. Zwei unabhaengige Einteilungen:

  - Grossgruppen A-D: enthalten Plenum + Events dieser Gruppe (1 Abo reicht)
  - Kleingruppen 1a-3b: nur die Events dieser Kleingruppe (zusaetzlich abonnieren)
  - plenum.ics: alle Events ohne Gruppen-Marker (Fallback, fuer Lehrende o.ae.)
  - full.ics: alle Events (Debug)

Event-UIDs sind stabil (Hash aus Datum+Zeit+Titel+Raum), damit Kalender-Clients
Updates korrekt zuordnen. DTSTAMP wird bei jedem Lauf aktualisiert, damit
abonnierende Clients Aenderungen erkennen.

Merge statt Ueberschreiben:
    Existiert im Ausgabeordner bereits ein Feed, wird er eingelesen und mit den
    neuen Terminen zusammengefuehrt — er wird NICHT komplett ueberschrieben. Der
    aktuelle Fetch ist nur fuer die Datumsbereiche seiner Quellen maßgeblich
    (`sources` im JSON): Innerhalb dieser Bereiche gilt der neue Plan (Union,
    Absagen fallen raus, keine Dubletten). Bereits veroeffentlichte Termine
    AUSSERHALB der aktuell geladenen Bereiche bleiben erhalten — so gehen alte
    Semester nicht verloren, wenn ihr Link offline genommen wird.
    Fehlt `sources` (altes JSON), wird der Datumsbereich aus den Events selbst
    abgeleitet.

Usage:
    python3 execution/ehb_stundenplan_to_ics.py --semester 2
    python3 execution/ehb_stundenplan_to_ics.py \
        --json tmp/ehb_events_s4.json \
        --out ../ehb-stundenplan-s4/docs/ics \
        --semester 4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

DEFAULT_JSON = Path("tmp/ehb_events.json")
DEFAULT_OUT = Path("studium/EHB-Stundenplan/ics")
TZ = ZoneInfo("Europe/Berlin")

GROSS = ["A", "B", "C", "D"]
KLEIN = ["1a", "1b", "2a", "2b", "3a", "3b"]

# Ab 6. Sem gibt es eine dritte Einteilung "Zweiergruppe" (bloße "Gr. 1"/"Gr. 2",
# im JSON als "G1"/"G2"). Sie ist aus der Grossgruppe ableitbar: A und D = Gruppe 1,
# B und C = Gruppe 2. Solche Termine werden daher in die passenden Grossgruppen-
# Feeds einsortiert — kein eigener Feed, keine extra Abfrage noetig.
GROSS_TO_ZWEIER = {"A": "G1", "D": "G1", "B": "G2", "C": "G2"}

UID_DOMAIN = "ehb-stundenplan.claudette"


def stable_uid(event: dict) -> str:
    key = f"{event['date']}|{event['start']}|{event['title']}|{event['room']}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{h}@{UID_DOMAIN}"


def to_ical_event(ev: dict, dtstamp: datetime) -> Event:
    ical = Event()
    ical.add("uid", stable_uid(ev))
    ical.add("dtstamp", dtstamp)

    dt_start = datetime.fromisoformat(f"{ev['date']}T{ev['start']}:00").replace(tzinfo=TZ)
    dt_end = datetime.fromisoformat(f"{ev['date']}T{ev['end']}:00").replace(tzinfo=TZ)
    ical.add("dtstart", dt_start)
    ical.add("dtend", dt_end)

    summary = ev["title"]
    if ev["groups"]:
        summary = f"[{'/'.join(ev['groups'])}] {summary}"
    ical.add("summary", summary)

    if ev["room"]:
        ical.add("location", ev["room"])

    desc_lines = []
    if ev["lecturer"]:
        desc_lines.append(f"Dozent: {ev['lecturer']}")
    if ev["beschreibung"]:
        desc_lines.append(ev["beschreibung"])
    if ev["modul_code"]:
        desc_lines.append(f"Modul: {ev['modul_code']}")
    if ev["groups"]:
        desc_lines.append(f"Gruppe: {', '.join(ev['groups'])}")
    if desc_lines:
        ical.add("description", "\n".join(desc_lines))

    return ical


def covered_spans(data: dict, events: list[dict]) -> list[tuple[date, date]]:
    """Datumsbereiche, fuer die der aktuelle Fetch maßgeblich ist.

    Bevorzugt die `sources` aus dem JSON (ein Bereich je erfolgreich geladenem
    Plan). Fehlen sie, wird der Gesamtbereich aus den Events abgeleitet.

    INVARIANTE: Eine Quelle deckt in ihrem Datumsbereich ALLE Gruppen ab (die
    EHB-Kohortenplaene enthalten alle Gruppen in einem HTML). Nur dann ist es
    korrekt, den Bereich global auf alle Feeds anzuwenden — ein bestehender
    Termin im Bereich, den die Quelle nicht liefert, gilt als Absage. Liefe eine
    Quelle nur Teildaten (z.B. eine Gruppe), wuerden fremde Gruppen faelschlich
    geloescht.
    """
    spans: list[tuple[date, date]] = []
    for src in data.get("sources", []):
        if not src.get("ok"):
            continue
        lo, hi = src.get("date_min"), src.get("date_max")
        if lo and hi:
            spans.append((date.fromisoformat(lo), date.fromisoformat(hi)))
    if not spans and events:
        dates = [date.fromisoformat(e["date"]) for e in events if e.get("date")]
        if dates:
            spans.append((min(dates), max(dates)))
    return spans


def in_any_span(d: date, spans: list[tuple[date, date]]) -> bool:
    return any(lo <= d <= hi for lo, hi in spans)


def event_date(comp: Event) -> date | None:
    dtstart = comp.get("dtstart")
    if dtstart is None:
        return None
    v = dtstart.dt
    return v.date() if isinstance(v, datetime) else v


def load_existing_events(path: Path) -> list[Event]:
    """Liest die VEVENT-Komponenten eines bestehenden Feeds (leer, wenn keiner).

    Best effort: Eine leere/kaputte/abgeschnittene ICS-Datei (z.B. Rest eines
    abgebrochenen Laufs) darf den Lauf nicht abreissen — dann lieber [] liefern
    und den Feed neu aufbauen, statt mit ValueError zu crashen.
    """
    if not path.exists():
        return []
    try:
        cal = Calendar.from_ical(path.read_bytes())
        return list(cal.walk("VEVENT"))
    except Exception as exc:  # noqa: BLE001 — icalendar wirft ValueError u.a. breit
        print(
            f"[ehb-ics] WARN: bestehender Feed {path} nicht lesbar ({exc}) — "
            "wird neu aufgebaut.",
            file=sys.stderr,
        )
        return []


def build_calendar(
    calname: str,
    events: list[dict],
    dtstamp: datetime,
    existing: list[Event] | None = None,
    spans: list[tuple[date, date]] | None = None,
) -> Calendar:
    cal = Calendar()
    cal.add("prodid", f"-//Claudette//EHB Stundenplan {calname}//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", calname)
    cal.add("x-wr-timezone", "Europe/Berlin")
    cal.add("method", "PUBLISH")

    new_comps = [to_ical_event(ev, dtstamp) for ev in events]
    new_uids = {str(c["uid"]) for c in new_comps}

    # Bestehende Termine AUSSERHALB der aktuell geladenen Datumsbereiche erhalten
    # (alte Semester, deren Link evtl. schon offline ist). Termine innerhalb der
    # Bereiche verwirft der neue Plan (Union/Absagen). UID-Kollisionen gewinnt
    # immer der neue Plan.
    spans = spans or []
    preserved: list[Event] = []
    for comp in existing or []:
        d = event_date(comp)
        if d is None:
            continue
        if in_any_span(d, spans):
            continue
        if str(comp.get("uid")) in new_uids:
            continue
        preserved.append(comp)

    combined = preserved + new_comps
    # Deterministische Reihenfolge → identischer Input erzeugt identisches ICS
    # (keine Leer-Commits).
    combined.sort(key=lambda c: (event_date(c) or date.min, str(c.get("uid"))))
    for comp in combined:
        cal.add_component(comp)
    return cal


def filter_gross(events: list[dict], gross: str) -> list[dict]:
    """Plenum + Events dieser Grossgruppe + zugehoerige Zweiergruppen-Events.

    Kleingruppen-Events (1a-3b) werden ausgeschlossen. Zweiergruppen-Events
    ("G1"/"G2") kommen in die Grossgruppen-Feeds gemaess GROSS_TO_ZWEIER
    (A/D → G1, B/C → G2).
    """
    zweier = GROSS_TO_ZWEIER.get(gross)
    out = []
    for e in events:
        groups = e["groups"]
        if not groups:
            out.append(e)
        elif gross in groups:
            out.append(e)
        elif zweier and zweier in groups:
            out.append(e)
    return out


def filter_klein(events: list[dict], klein: str) -> list[dict]:
    return [e for e in events if klein in e["groups"]]


def filter_plenum(events: list[dict]) -> list[dict]:
    return [e for e in events if not e["groups"]]


def write_ics(cal: Calendar, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cal.to_ical())


# Fester Fallback-DTSTAMP, falls im JSON kein `fetched` steht (z.B. handgebautes
# JSON). Konstant, damit auch dann identischer Input identisches ICS erzeugt.
FALLBACK_DTSTAMP = datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC"))


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", default=str(DEFAULT_JSON), type=Path,
                   help="Eingabe-JSON von ehb_stundenplan_fetch.py (Default: %(default)s)")
    p.add_argument("--out", default=str(DEFAULT_OUT), type=Path,
                   help="Ausgabeordner fuer die ICS-Feeds (Default: %(default)s)")
    p.add_argument(
        "--semester",
        required=True,
        help="Semester-Nummer fuer Kalender-Anzeigenamen, z.B. '2' oder '4'. "
        "Wird zu 'EHB HW {N}. Sem Plenum & X' bzw. 'EHB HW {N}. Sem Klein 1a'.",
    )
    args = p.parse_args()

    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    events: list[dict] = data["events"]

    # DTSTAMP stabil aus dem JSON-fetched-Datum ableiten, damit identische
    # Stundenplaene auch identische ICS-Dateien erzeugen (keine Leer-Commits).
    fetched = data.get("fetched")
    if fetched:
        dtstamp = datetime.fromisoformat(fetched).replace(tzinfo=ZoneInfo("UTC"))
    else:
        dtstamp = FALLBACK_DTSTAMP

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    spans = covered_spans(data, events)

    summary: list[tuple[str, int, int]] = []

    # Erst ALLE Kalender im Speicher bauen, dann geschlossen schreiben. So bleibt
    # der publizierte Ordner konsistent: Faellt der Bau eines spaeteren Feeds aus
    # (z.B. kaputte Bestands-ICS), ist noch keine Datei ueberschrieben.
    to_write: list[tuple[Path, Calendar]] = []

    def stage_feed(filename: str, calname: str, sel: list[dict]) -> None:
        path = out_dir / filename
        existing = load_existing_events(path)
        cal = build_calendar(calname, sel, dtstamp, existing=existing, spans=spans)
        to_write.append((path, cal))
        summary.append((filename, len(sel), len(list(cal.walk("VEVENT")))))

    # Grossgruppen (Plenum + eigene Events)
    for g in GROSS:
        sel = filter_gross(events, g)
        stage_feed(f"gross-{g.lower()}.ics", f"EHB HW {args.semester}. Sem Plenum & {g}", sel)

    # Kleingruppen (nur eigene Events)
    for k in KLEIN:
        sel = filter_klein(events, k)
        stage_feed(f"klein-{k}.ics", f"EHB HW {args.semester}. Sem Klein {k}", sel)

    for path, cal in to_write:
        write_ics(cal, path)

    span_str = ", ".join(f"{lo}…{hi}" for lo, hi in spans) or "—"
    print(f"[ehb-ics] Ausgabe: {out_dir} | maßgebliche Bereiche: {span_str}", file=sys.stderr)
    print(f"  {'Feed':20s} {'neu':>5s} {'gesamt':>7s}", file=sys.stderr)
    for name, new_count, total in summary:
        kept = total - new_count
        extra = f" (+{kept} erhalten)" if kept else ""
        print(f"  {name:20s} {new_count:5d} {total:7d}{extra}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
