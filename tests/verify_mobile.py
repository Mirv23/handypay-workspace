"""Vérifications du téléphone : application installable (manifeste, icônes, service worker),
en-têtes, mode compteur. Se lance avec tests/run_verify.py --extra tests/verify_mobile.py."""
import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path

import httpx

SCR = Path(os.environ.get("VERIFY_DIR") or Path(__file__).parent)
BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8765")
OWNER_PW = re.search(r"Mot de passe : (\S+)", (SCR / "owner.txt").read_text(encoding="utf-8")).group(1)
results = []


def check(label, cond, detail=""):
    results.append((bool(cond), label, detail))
    print(("OK   " if cond else "FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))


def client():
    c = httpx.Client(base_url=BASE, timeout=30)
    assert c.get("/").status_code == 200
    return c


def h(c):
    return {"X-CSRF-Token": c.cookies.get("hp_csrf", "")}


def login(c, ident, pw):
    return c.post("/api/auth/login", json={"identifier": ident, "password": pw}, headers=h(c))


def png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def corner_alpha(data: bytes):
    """Alpha du pixel en haut à gauche d'un PNG RGBA (ligne sans filtre), sinon None."""
    pos, idat = 8, b""
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I", data[pos:pos + 4])[0], data[pos + 4:pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    try:
        raw = zlib.decompress(idat)
    except zlib.error:
        return None
    return raw[4] if raw and raw[0] == 0 and data[25] == 6 else None


def all_keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k))
            all_keys(v, out)
    elif isinstance(value, list):
        for v in value:
            all_keys(v, out)
    return out


anon = httpx.Client(base_url=BASE, timeout=30)  # aucun cookie : un visiteur sans session

# ── Manifeste et icônes : publics, sans rien de personnel ───────────
r = anon.get("/manifest.webmanifest")
check("manifeste servi sans session (200, application/manifest+json)",
      r.status_code == 200 and r.headers.get("content-type", "").startswith("application/manifest+json"), f"{r.status_code} {r.headers.get('content-type')}")
man = r.json() if r.status_code == 200 else {}
color = re.compile(r"^#[0-9A-Fa-f]{6}$")
check("manifeste : nom, nom court, display standalone, couleurs",
      man.get("name") == "HandyPay · Espace d'équipe" and man.get("short_name") and man.get("display") == "standalone"
      and color.match(man.get("theme_color", "")) and color.match(man.get("background_color", "")), json.dumps(man, ensure_ascii=False)[:300])
check("manifeste : start_url dans la portée « / », langue fr",
      man.get("scope") == "/" and str(man.get("start_url", "")).startswith("/") and man.get("lang") == "fr")
icons = man.get("icons", [])
check("manifeste : icônes 192, 512 et maskable",
      any(i["sizes"] == "192x192" for i in icons) and any(i["sizes"] == "512x512" and i.get("purpose", "any") == "any" for i in icons)
      and any(i.get("purpose") == "maskable" for i in icons), json.dumps(icons))
sizes_ok, cookies_set = True, []
for icon in icons + [{"src": "/icons/apple-touch-icon.png", "sizes": "180x180"}, {"src": "/icons/favicon-32.png", "sizes": "32x32"}]:
    ri = anon.get(icon["src"])
    w, hgt = (int(x) for x in icon["sizes"].split("x"))
    if ri.status_code != 200 or ri.headers.get("content-type") != "image/png" or png_size(ri.content) != (w, hgt):
        sizes_ok = False
    cookies_set += ri.headers.get_list("set-cookie")
check("icônes : PNG aux bonnes dimensions, servies sans session", sizes_ok)
check("icônes : « maskable » à fond plein (coin opaque), « any » arrondie (coin transparent)",
      corner_alpha(anon.get("/icons/icon-maskable-512.png").content) == 255 and corner_alpha(anon.get("/icons/icon-512.png").content) == 0)
check("icône inconnue ou chemin détourné → 404",
      anon.get("/icons/inconnu.png").status_code == 404 and anon.get("/icons/..%2F..%2Fmain.py").status_code == 404
      and anon.get("/icons/..%5Cmobile.py").status_code == 404)

# ── Service worker : public, JavaScript, ne met jamais /api/ en cache ─
r = anon.get("/sw.js")
sw = r.text
check("service worker servi sans session (200, JavaScript)",
      r.status_code == 200 and r.headers.get("content-type", "").split(";")[0] in ("text/javascript", "application/javascript"),
      f"{r.status_code} {r.headers.get('content-type')}")
check("service worker : revalidé à chaque visite (Cache-Control: no-cache)", r.headers.get("cache-control") == "no-cache", r.headers.get("cache-control"))
sw_csp = r.headers.get("content-security-policy", "")
check("service worker : CSP minimale (default-src 'none', connect-src 'self')",
      sw_csp == "default-src 'none'; connect-src 'self'", sw_csp)
fetch_at = sw.find('addEventListener("fetch"')
handler = sw[fetch_at:] if fetch_at >= 0 else ""
guard = re.search(r'if\s*\(\s*url\.pathname\.startsWith\("/api/"\)\s*\)\s*return;', handler)
uses = [i for i in (handler.find("respondWith"), handler.find("caches."), handler.find("fetch(")) if i >= 0]
check("service worker : la règle /api/ sort du gestionnaire avant tout cache ou réseau",
      bool(guard) and bool(uses) and guard.start() < min(uses), handler[:200])
shell_m = re.search(r"const SHELL = \[(.*?)\];", sw, re.S)
shell = re.findall(r'"([^"]*)"', shell_m.group(1)) if shell_m else []
check("service worker : coquille = page, manifeste, icônes (aucune adresse /api/)",
      "/" in shell and "/manifest.webmanifest" in shell and all(s == "/" or s == "/manifest.webmanifest" or s.startswith("/icons/") for s in shell),
      str(shell))
puts = re.findall(r"cache\.put\(([^,]+),", sw)
check("service worker : seule la page « / » est réécrite dans le cache (cache.put)", puts == ['"/"'], str(puts))
check("service worker : message clair sans réseau", "Pas de réseau" in sw and "Réessayer" in sw)
public = [anon.get(p) for p in ("/manifest.webmanifest", "/sw.js")]
check("routes publiques : aucun cookie posé, aucune donnée de l'espace",
      not cookies_set and not any(x.headers.get_list("set-cookie") for x in public)
      and not any(s in (public[0].text + public[1].text) for s in ("+509", "mirv", "Mirv")))

# ── Page : manifeste lié, CSP adaptée sans ouverture ─────────────────
page = anon.get("/")
csp = page.headers.get("content-security-policy", "")
check("page : CSP avec worker-src et manifest-src 'self', sans joker ni domaine extérieur",
      "default-src 'self'" in csp and "worker-src 'self'" in csp and "manifest-src 'self'" in csp and "frame-ancestors 'none'" in csp
      and "*" not in csp and "http" not in csp and "data:" not in csp.replace("img-src 'self' data:", ""), csp)
check("page : manifeste, icône Apple, viewport-fit=cover, couleur de barre d'état",
      '<link rel="manifest" href="/manifest.webmanifest">' in page.text and 'rel="apple-touch-icon"' in page.text
      and "viewport-fit=cover" in page.text and 'name="theme-color"' in page.text)
check("page : barre d'onglets, bouton flottant et inscription du service worker",
      'id="tabbar"' in page.text and 'id="fab"' in page.text and 'serviceWorker.register("/sw.js"' in page.text)

# ── Mode compteur : session obligatoire ──────────────────────────────
r = anon.get("/compteur", follow_redirects=False)
check("/compteur sans session → 303 vers la connexion (/#/compteur), sans chiffre",
      r.status_code == 303 and r.headers.get("location") == "/#/compteur" and "cumul" not in r.text, f"{r.status_code} {r.headers.get('location')}")
forged = httpx.Client(base_url=BASE, timeout=30, cookies={"hp_session": "eyJ1IjoibWlydiJ9.faux"})
check("/compteur avec un cookie de session forgé → 303", forged.get("/compteur", follow_redirects=False).status_code == 303)
check("/api/ws/compteur sans session → 401", anon.get("/api/ws/compteur").status_code == 401)

mirv = client()
check("connexion du responsable", login(mirv, "mirv", OWNER_PW).status_code == 200)
# Une vente avec un client nommé : son nom et son numéro ne doivent jamais sortir dans le compteur.
r = mirv.post("/api/ws/sales", json={"cards": 2, "client_name": "Zéphyrine Témoin", "client_phone": "3456 7890"}, headers=h(mirv))
check("vente témoin avec nom et numéro de client", r.status_code == 201, r.text)
r = mirv.get("/api/ws/compteur")
check("/api/ws/compteur avec session → 200 JSON, no-store", r.status_code == 200 and "no-store" in r.headers.get("cache-control", ""), r.text[:200])
data = r.json() if r.status_code == 200 else {}
keys = all_keys(data)
banned = ("name", "nom", "phone", "tel", "whatsapp", "user", "client", "email", "person", "member", "display", "by_")
check("compteur : aucun champ de nom ni de téléphone", keys and not [k for k in keys if any(b in k.lower() for b in banned)], str(sorted(keys)))
check("compteur : aucune valeur personnelle (client, numéro, membre)",
      not any(s in r.text for s in ("Zéphyrine", "Z\\u00e9phyrine", "34567890", "+509", "Mirv", "Michael", "michael", "mirv")), r.text[:300])
st = mirv.get("/api/ws/sales/stats").json()
c, nm = data.get("campaign", {}), data.get("next_milestone") or {}
check("compteur : mêmes chiffres que les stats (cumul, jour, objectif du jour, jour N / 30)",
      c.get("cumul") == st["campaign"]["cumul"] and data["today"]["cards"] == st["today"]["cards"] and data["today"]["target"] == 100
      and c.get("day") == st["campaign"]["elapsed"] and c.get("days") == 30 and c.get("target") == 3000, json.dumps(data)[:300])
check("compteur : prochain jalon et barre (jalons 700 → 3 000)",
      nm.get("target") == st["campaign"]["next_milestone"]["target"] and nm.get("date") == st["campaign"]["next_milestone"]["date"]
      and [m["target"] for m in data.get("milestones", [])] == [700, 1400, 2100, 3000])
check("compteur : marge fournie à part (cachée par défaut sur la page)", data.get("margin", {}).get("total") == st["campaign"]["margin"])
check("compteur en lecture seule (POST → 405)", mirv.post("/api/ws/compteur", json={}, headers=h(mirv)).status_code == 405)
pg = mirv.get("/compteur")
pcsp = pg.headers.get("content-security-policy", "")
check("/compteur avec session → 200, CSP de la page, X-Frame-Options DENY, no-store",
      pg.status_code == 200 and "default-src 'self'" in pcsp and "frame-ancestors 'none'" in pcsp
      and pg.headers.get("x-frame-options") == "DENY" and "no-store" in pg.headers.get("cache-control", ""), f"{pg.status_code} {pcsp}")
init = re.search(r'<script id="c-init" type="application/json">(.*?)</script>', pg.text, re.S)
init_data = json.loads(init.group(1)) if init else {}
check("/compteur : chiffres initiaux dans la page (aucun saut au chargement)",
      init_data.get("campaign", {}).get("cumul") == c.get("cumul"))
check("/compteur : aucun nom ni numéro dans la page",
      not any(s in pg.text for s in ("Zéphyrine", "34567890", "+509", "Mirv", "Michael")))
check("/compteur : marge cachée par défaut", re.search(r'<div class="card" id="c-margin" hidden>', pg.text) is not None)

# ── Un observateur lit le compteur ───────────────────────────────────
r = mirv.post("/api/ws/members", json={"username": "camera", "display_name": "Caméra", "role": "observateur"}, headers=h(mirv))
cam = client()
check("observateur : connexion", r.status_code == 201 and login(cam, "camera", r.json().get("password", "")).status_code == 200)
check("observateur : /compteur et /api/ws/compteur → 200",
      cam.get("/compteur").status_code == 200 and cam.get("/api/ws/compteur").status_code == 200)
cam.post("/api/auth/logout", headers=h(cam))
check("après déconnexion : /compteur → 303", cam.get("/compteur", follow_redirects=False).status_code == 303)

ok = sum(1 for x in results if x[0])
print(f"\n{ok}/{len(results)} vérifications réussies")
sys.exit(0 if ok == len(results) else 1)
