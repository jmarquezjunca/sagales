#!/usr/bin/env python3
"""Descarga el GTFS de autobuses interurbanos de la Generalitat y genera data/data.json
con las líneas 360, 361 y 389.1-389.4 (solo paradas de Sant Fost y Mollet).
Uso:  python scripts/build_data.py            (descarga el fichero)
      python scripts/build_data.py --zip f.zip (usa un fichero local)"""
import argparse, csv, io, json, re, sys, time, urllib.request, zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

URL = ("https://territori.gencat.cat/web/.content/home/serveis/visors-cartografia/"
       "bases-cartografiques/infraestructures-mobilitat/autobusos-interurbans/busos-interurbans-GTFS.zip")
ROOT = Path(__file__).resolve().parent.parent
LINEAS = ["360", "361", "389.1", "389.2", "389.3", "389.4"]
LINE_RE = re.compile(r"(?<![\w.])(360|361|389\.[1-4])(?!\d)")
MUNI = {"Mollet": "M", "Sant Fost": "S"}

def limpiar(n):  # mismo criterio con el que se hizo el Excel de paradas
    n = re.sub(r"\s*(MB\d+|PNL|parada bus|TAD)\b", "", n)
    n = re.sub(r"\s*\((A|D)\)\s*", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"^c\. ", "", n)
    return n[:1].upper() + n[1:]

def filas(z, nombre):
    try:
        f = z.open(nombre)
    except KeyError:
        return
    with f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

def minutos(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 60 + m + (1 if s >= 30 else 0)

def descargar(destino):
    for i in range(3):
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (horarios-sagales)"})
            with urllib.request.urlopen(req, timeout=120) as r, open(destino, "wb") as f:
                f.write(r.read())
            return
        except Exception as e:
            print(f"Intento {i+1} fallido: {e}", file=sys.stderr)
            time.sleep(10)
    sys.exit("No se pudo descargar el GTFS de la Generalitat")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip")
    ap.add_argument("--out", default=str(ROOT / "data" / "data.json"))
    a = ap.parse_args()
    ruta = a.zip
    if not ruta:
        ruta = "/tmp/gtfs.zip"
        descargar(ruta)
    z = zipfile.ZipFile(ruta)
    muni = {r["nombre_limpio"]: r["municipio"] for r in csv.DictReader(open(ROOT / "paradas_municipios.csv", encoding="utf-8"))}
    # 1) líneas
    linea_de = {r["route_id"]: m.group(1) for r in filas(z, "routes.txt")
                if (m := LINE_RE.search(r["route_long_name"]))}
    trips = {r["trip_id"]: (linea_de[r["route_id"]], r["direction_id"], r["service_id"])
             for r in filas(z, "trips.txt") if r["route_id"] in linea_de}
    # 2) paradas y horas
    parada = {r["stop_id"]: (limpiar(r["stop_name"]), float(r["stop_lat"]), float(r["stop_lon"]))
              for r in filas(z, "stops.txt")}
    st = defaultdict(list)
    for r in filas(z, "stop_times.txt"):
        if r["trip_id"] in trips:
            st[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"], r["departure_time"] or r["arrival_time"]))
    sin_clasificar, coords, unicos = set(), defaultdict(list), {}
    for tid, lst in st.items():
        linea, d, serv = trips[tid]
        vistos, nombres, mins = set(), [], []
        for _, sid, hora in sorted(lst):
            nom, lat, lon = parada[sid]
            if nom not in muni:
                sin_clasificar.add(nom)
            if muni.get(nom) not in MUNI or nom in vistos:
                continue
            vistos.add(nom); nombres.append(nom); mins.append(minutos(hora)); coords[nom].append((lat, lon))
        if len(nombres) >= 2:
            unicos.setdefault((linea, d, tuple(nombres), tuple(mins)), set()).add(serv)
    if not unicos:
        sys.exit("No se encontró ningún trayecto: ¿ha cambiado el formato del fichero?")
    orden = sorted(coords)
    idx = {n: i for i, n in enumerate(orden)}
    stops = [[n, MUNI[muni[n]], round(sum(c[0] for c in coords[n]) / len(coords[n]), 5),
              round(sum(c[1] for c in coords[n]) / len(coords[n]), 5)] for n in orden]
    out_trips = sorted([k[0], k[1], sorted(s), [idx[n] for n in k[2]], list(k[3])] for k, s in unicos.items())
    # 3) calendario (flags semanales + excepciones)
    usados = {s for t in out_trips for s in t[2]}
    cal = defaultdict(set)
    dias = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for r in filas(z, "calendar.txt"):
        if r["service_id"] in usados:
            d0 = datetime.strptime(r["start_date"], "%Y%m%d").date(); d1 = datetime.strptime(r["end_date"], "%Y%m%d").date()
            while d0 <= d1:
                if r[dias[d0.weekday()]] == "1":
                    cal[d0.strftime("%Y%m%d")].add(r["service_id"])
                d0 += timedelta(days=1)
    for r in filas(z, "calendar_dates.txt"):
        if r["service_id"] in usados:
            (cal[r["date"]].add if r["exception_type"] == "1" else cal[r["date"]].discard)(r["service_id"])
    cal = {d: sorted(s) for d, s in sorted(cal.items()) if s}
    meta = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": URL,
            "feed_from": min(cal), "feed_to": max(cal), "trips": len(out_trips),
            "unclassified": sorted(sin_clasificar)}
    data = {"meta": meta, "lines": LINEAS, "stops": stops, "trips": out_trips, "cal": cal}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"OK: {len(out_trips)} trayectos, {len(stops)} paradas, calendario {meta['feed_from']}-{meta['feed_to']}, "
          f"paradas sin clasificar: {len(sin_clasificar)}")
    if sin_clasificar:
        print("AVISO: paradas nuevas sin clasificar (se ignoran hasta añadirlas a paradas_municipios.csv):")
        for n in sorted(sin_clasificar): print("  -", n)

if __name__ == "__main__":
    main()
