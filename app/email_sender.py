"""Envoi d'e-mails (version minimale de celui de JaKOB) : Resend ou SMTP.

Tout vient des variables d'environnement, aucun secret dans le code :
- `EMAIL_FROM` (obligatoire) : « HandyPay équipe <equipe@ton-domaine> » ;
- `RESEND_API_KEY` : envoi par l'API HTTP de Resend (le plus simple) ;
- ou `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`,
  `SMTP_STARTTLS` (« 0 » pour le couper ; le port 465 passe en TLS direct) ;
- `EMAIL_PROVIDER` (« resend » ou « smtp ») force le choix ;
- `EMAIL_REPLY_TO` (facultatif).

Ces e-mails sont internes (l'e-mail du matin de chaque membre, qui le coupe
dans son profil) : pas de liste de diffusion, pas de lien de désinscription.
"""

from __future__ import annotations

import os
import re
import smtplib
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage as _MimeMessage
from email.utils import formataddr, parseaddr
from html import unescape as html_unescape
from typing import Any, Protocol

ENV_PROVIDER = "EMAIL_PROVIDER"
ENV_RESEND_KEY = "RESEND_API_KEY"
ENV_FROM = "EMAIL_FROM"
ENV_REPLY_TO = "EMAIL_REPLY_TO"
ENV_SMTP_HOST = "SMTP_HOST"
ENV_SMTP_PORT = "SMTP_PORT"
ENV_SMTP_USER = "SMTP_USER"
ENV_SMTP_PASSWORD = "SMTP_PASSWORD"
ENV_SMTP_STARTTLS = "SMTP_STARTTLS"

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_HTTP_TIMEOUT_S = 15.0
_SMTP_TIMEOUT_S = 20.0

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")


class EmailNotConfigured(RuntimeError):
    """Aucun fournisseur d'envoi utilisable (clé, hôte SMTP ou expéditeur absent)."""


def clean_email(raw: Any) -> str:
    """Adresse normalisée (minuscules, sans espaces), ou "" si elle est invalide."""
    value = str(raw or "").strip().lower()
    if len(value) > 254 or not EMAIL_RE.match(value):
        return ""
    return value


@dataclass
class Message:
    to: str
    subject: str
    html: str
    headers: dict = field(default_factory=dict)


class EmailTransport(Protocol):
    def send(self, message: Message) -> tuple[bool, str]:
        """→ (ok, code d'erreur). Le code vaut '' en cas de succès."""


_A_RE = re.compile(r'<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_BLOCK_END_RE = re.compile(r"</(p|h1|h2|h3|blockquote|ul|ol|tr|div)>|<hr\b[^>]*>", re.I)


def html_to_text(html: str) -> str:
    """Version texte d'un HTML déjà échappé (partie `text/plain`)."""
    def _link(match) -> str:
        url = match.group(1)
        label = re.sub(r"<[^>]*>", "", match.group(2)).strip()
        return "%s (%s)" % (label, url) if label and label != url else url

    text = _A_RE.sub(_link, str(html or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _BLOCK_END_RE.sub("\n\n", text)
    text = re.sub(r"<li\b[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"</li>", "\n", text, flags=re.I)
    text = html_unescape(re.sub(r"<[^>]*>", "", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def sender_address() -> str:
    raw = os.environ.get(ENV_FROM, "").strip()
    if not raw:
        return ""
    name, addr = parseaddr(raw)
    if not clean_email(addr):
        return ""
    return formataddr((name, addr)) if name else addr


def provider_name() -> str:
    forced = os.environ.get(ENV_PROVIDER, "").strip().lower()
    has_resend = bool(os.environ.get(ENV_RESEND_KEY, "").strip())
    has_smtp = bool(os.environ.get(ENV_SMTP_HOST, "").strip())
    if forced == "resend":
        return "resend" if has_resend else ""
    if forced == "smtp":
        return "smtp" if has_smtp else ""
    if has_resend:
        return "resend"
    return "smtp" if has_smtp else ""


class ResendTransport:
    def __init__(self, api_key: str, sender: str, reply_to: str = "") -> None:
        self._key, self._sender, self._reply_to = api_key, sender, reply_to

    def send(self, message: Message) -> tuple[bool, str]:
        import httpx

        payload: dict = {"from": self._sender, "to": [message.to], "subject": message.subject,
                         "html": message.html, "text": html_to_text(message.html)}
        if self._reply_to:
            payload["reply_to"] = self._reply_to
        if message.headers:
            payload["headers"] = message.headers
        try:
            resp = httpx.post(_RESEND_ENDPOINT, json=payload, headers={"Authorization": "Bearer " + self._key},
                              timeout=_HTTP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return False, "NETWORK:" + type(exc).__name__
        if resp.status_code < 300:
            return True, ""
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if isinstance(body, dict):
            for key in ("name", "error", "message"):
                if isinstance(body.get(key), str) and body[key]:
                    return False, body[key][:80]
        return False, "HTTP_%s" % resp.status_code


class SmtpTransport:
    """Une connexion par message, sérialisée ; le port 465 bascule en SMTPS."""

    def __init__(self, host: str, port: int, user: str, password: str, sender: str,
                 *, starttls: bool = True, reply_to: str = "") -> None:
        self._host, self._port, self._user, self._password = host, port, user, password
        self._sender, self._starttls, self._reply_to = sender, starttls, reply_to
        self._lock = threading.Lock()

    def _build(self, message: Message) -> _MimeMessage:
        mime = _MimeMessage()
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        if self._reply_to:
            mime["Reply-To"] = self._reply_to
        for key, value in (message.headers or {}).items():
            mime[key] = value
        mime.set_content(html_to_text(message.html) or "Ce message nécessite un client HTML.")
        mime.add_alternative(message.html, subtype="html")
        return mime

    def send(self, message: Message) -> tuple[bool, str]:
        try:
            with self._lock:
                smtp_cls = smtplib.SMTP_SSL if self._port == 465 else smtplib.SMTP
                with smtp_cls(self._host, self._port, timeout=_SMTP_TIMEOUT_S) as smtp:
                    if self._starttls and self._port != 465:
                        smtp.starttls()
                    if self._user:
                        smtp.login(self._user, self._password)
                    smtp.send_message(self._build(message))
            return True, ""
        except smtplib.SMTPRecipientsRefused:
            return False, "SMTP_RECIPIENT_REFUSED"
        except smtplib.SMTPResponseException as exc:
            return False, "SMTP_%s" % exc.smtp_code
        except Exception as exc:  # noqa: BLE001
            return False, "NETWORK:" + type(exc).__name__


def default_transport() -> EmailTransport:
    sender = sender_address()
    if not sender:
        raise EmailNotConfigured("Expéditeur absent : renseigne %s." % ENV_FROM)
    reply_to = os.environ.get(ENV_REPLY_TO, "").strip()
    name = provider_name()
    if name == "resend":
        return ResendTransport(os.environ[ENV_RESEND_KEY].strip(), sender, reply_to=reply_to)
    if name == "smtp":
        try:
            port = int(os.environ.get(ENV_SMTP_PORT, "587") or 587)
        except ValueError:
            port = 587
        return SmtpTransport(
            os.environ[ENV_SMTP_HOST].strip(), port, os.environ.get(ENV_SMTP_USER, "").strip(),
            os.environ.get(ENV_SMTP_PASSWORD, ""), sender,
            starttls=os.environ.get(ENV_SMTP_STARTTLS, "1").strip() != "0", reply_to=reply_to,
        )
    raise EmailNotConfigured("Aucun fournisseur d'e-mail : renseigne %s ou %s." % (ENV_RESEND_KEY, ENV_SMTP_HOST))
