# EHB Hebammen Stundenplan 4. Semester &mdash; Kalender-Abos

Automatisch generierte, abonnierbare Kalender-Feeds fuer den Stundenplan des
4. Semesters Hebammenwissenschaften an der Evangelischen Hochschule Berlin (EHB).

**Landing Page:** https://gina-gruenhage.github.io/ehb-stundenplan-s4/

## Wie funktioniert es?

1. Eine GitHub Action laedt 1x taeglich den [HTML-Stundenplan der EHB](https://www.eh-berlin.de/stundenplan/Studierende/HTML/H_4_H4.html).
2. Ein Parser extrahiert alle Veranstaltungen inkl. Gruppen-Zuordnung.
3. Ein Generator schreibt 10 ICS-Feeds nach `docs/ics/`:
   - **4 Grossgruppen-Feeds** (`gross-a.ics` ... `gross-d.ics`) &mdash; Plenum + Events der jeweiligen Grossgruppe
   - **6 Kleingruppen-Feeds** (`klein-1a.ics` ... `klein-3b.ics`) &mdash; nur die Kleingruppen-Events (zusaetzlich zum Grossgruppen-Feed abonnieren)
4. Dein Kalender-Client (iOS, Google, Apple, Outlook) pollt die URL regelmaessig und uebernimmt Aenderungen automatisch.

## Inoffiziell

Dieses Projekt ist nicht von der EHB betrieben. Quelle der Daten ist der oeffentliche Stundenplan
der Hochschule. Bei Abweichungen gilt der offizielle Stundenplan.

Quellcode des Generators: https://github.com/gruenhage-ai/claudette (Ordner `execution/` und `studium/EHB-Stundenplan/`).
