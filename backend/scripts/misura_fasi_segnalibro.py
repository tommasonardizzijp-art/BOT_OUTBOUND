"""Misura le due fasi di una sessione inbox con segnalibro acceso.

FASE 1 (inseguimento): dall'avvio alla PRIMA apertura — quanto costa
attraversare la zona gia' lavorata, e quanto e' lunga.
FASE 2 (raccolta): da li' in poi — a che ritmo si pescano contatti.

Serve a proiettare i tempi su un archivio molto piu' grande: la fase 1 cresce
con la lista, la fase 2 no.
"""
import re
import sys

TS = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]")


def secondi(riga):
    m = TS.match(riga)
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def main(percorso):
    righe = open(percorso, encoding="utf-8", errors="replace").read().splitlines()
    eventi = [(secondi(r), r) for r in righe if TS.match(r)]
    if not eventi:
        print("nessun evento con timestamp")
        return

    t0 = next(t for t, r in eventi if "FASE B" in r)
    aperture = [(t, r) for t, r in eventi if "APERTA" in r]
    fallite = [(t, r) for t, r in eventi if "fallita apertura" in r]
    lanci = [(t, r) for t, r in eventi if "[lancio]" in r]
    fine = eventi[-1][0]

    if not aperture:
        print(f"nessuna apertura in {fine - t0}s: la sessione e' tutta inseguimento")
        return

    t_prima = aperture[0][0]
    inseguimento = t_prima - t0
    raccolta = fine - t_prima

    top_fine_inseguimento = None
    for t, r in eventi:
        if t > t_prima:
            break
        m = re.search(r"top=(\d+)", r)
        if m:
            top_fine_inseguimento = int(m.group(1))

    print("== FASE 1 — inseguimento della zona gia' lavorata ==")
    print(f"  durata            : {inseguimento}s ({inseguimento/60:.1f} min)")
    print(f"  lanci             : {len([1 for t,_ in lanci if t <= t_prima])}")
    print(f"  profondita' finale: {top_fine_inseguimento} px "
          f"(~{(top_fine_inseguimento or 0)//72} righe attraversate)")
    if top_fine_inseguimento:
        print(f"  velocita'         : {top_fine_inseguimento/max(1,inseguimento):.0f} px/s "
              f"(~{(top_fine_inseguimento//72)/max(1,inseguimento/60):.0f} righe/min)")

    ok = [t for t, _ in aperture if t >= t_prima]
    ko = [t for t, _ in fallite if t >= t_prima]
    print("== FASE 2 — raccolta ==")
    print(f"  durata            : {raccolta}s ({raccolta/60:.1f} min)")
    print(f"  aperture riuscite : {len(ok)}")
    print(f"  fallite           : {len(ko)}")
    if raccolta > 0:
        print(f"  ritmo             : {len(ok)/(raccolta/60):.1f} aperture/min "
              f"({len(ok)/(raccolta/3600):.0f}/ora)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/task12_runner_segnalibro.log")
