/* HandyPay · Espace d'équipe — service worker.

   CE QU'IL MET EN CACHE : la coquille statique, et rien d'autre — la page « / »
   (du HTML sans aucune donnée : tout passe ensuite par l'API et la session),
   le manifeste et les icônes.

   CE QU'IL NE MET JAMAIS EN CACHE : /api/* (ventes, fiches, numéros de clients).
   Ces requêtes ne passent même pas par lui : elles vont au réseau, point.
   Ni /compteur, ni aucune autre page : seule « / » est gardée.

   SANS RÉSEAU : « / » sort du cache et la page dit « Pas de réseau » ; toute
   autre adresse reçoit un court message hors ligne. */
"use strict";

const VERSION = "hp-coquille-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
  "/icons/favicon-32.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // Règle n° 1 : l'API ne passe jamais par le cache (données des clients).
  if (url.pathname.startsWith("/api/")) return;
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    event.respondWith(page(event, req, url));
    return;
  }
  if (url.pathname !== "/" && SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(url.pathname).then((hit) => hit || fetch(req)));
  }
});

// Pages : le réseau d'abord ; la coquille « / » seulement si le réseau manque.
async function page(event, req, url) {
  try {
    const res = await fetch(req);
    if (url.pathname === "/" && res.ok && res.type === "basic" && !res.redirected) {
      const copy = res.clone();
      event.waitUntil(caches.open(VERSION).then((cache) => cache.put("/", copy)));
    }
    return res;
  } catch (err) {
    if (url.pathname === "/") {
      const hit = await caches.match("/");
      if (hit) return hit;
    }
    return offline();
  }
}

function offline() {
  const html =
    '<!doctype html><html lang="fr"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' +
    "<title>Pas de réseau · HandyPay</title><style>" +
    ":root{color-scheme:light dark}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;" +
    "font:16px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#F5F6F8;color:#101828}" +
    "@media (prefers-color-scheme:dark){body{background:#0B111A;color:#E6EBF2}}" +
    "main{max-width:360px;text-align:center}.m{width:56px;height:56px;border-radius:16px;margin:0 auto 16px;display:grid;" +
    "place-items:center;color:#fff;font-weight:900;font-size:26px;background:linear-gradient(135deg,#12B76A,#0E7C66)}" +
    "h1{font-size:22px;margin:0 0 8px}p{margin:0 0 20px;opacity:.8}a{display:inline-block;padding:12px 22px;border-radius:999px;" +
    "background:#0E7C66;color:#fff;font-weight:700;text-decoration:none;min-height:44px;box-sizing:border-box}" +
    '</style></head><body><main><div class="m">H</div><h1>Pas de réseau</h1>' +
    "<p>L'espace d'équipe a besoin d'Internet pour afficher les ventes et les relances. " +
    "Vérifie les données mobiles ou le Wi-Fi, puis réessaie.</p>" +
    '<a href="">Réessayer</a></main></body></html>';
  return new Response(html, {
    status: 503,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    },
  });
}
