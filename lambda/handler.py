"""
Lambda handler : classe les PDFs reçus par Gmail et les archive dans Google Drive.

Paramètres SSM requis (SecureString, tous lus en un seul appel) :
  /mail-sort-pdfs/google/client_id
  /mail-sort-pdfs/google/client_secret
  /mail-sort-pdfs/google/refresh_token
  /mail-sort-pdfs/anthropic/api_key
"""

import base64
import io
import json
import logging

import anthropic
import boto3
import pypdf
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

log = logging.getLogger()
log.setLevel(logging.INFO)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

SSM_PREFIX = "/mail-sort-pdfs"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]

AMBIGUOUS_LABEL = "pdf-a-classer"
CONFIDENCE_THRESHOLD = 0.80
GMAIL_QUERY = f"has:attachment filename:pdf label:INBOX -label:{AMBIGUOUS_LABEL}"

CLASSIFICATION_SYSTEM = """\
Tu es un assistant qui classe des documents PDF dans des dossiers Google Drive.

DOSSIERS DISPONIBLES :
- factures : factures d'achats personnels (électronique, électroménager, abonnements, e-commerce...)
- parking : avis d'échéance mensuel d'un garage (géré par FONCIA)
- appt_gestion : appels de fonds / relevés mensuels pour un appartement (Citya ou FONCIA)
- copro_appel_fonds : appels de fonds trimestriels d'une copropriété (syndic Citya)
- copro_gestion : comptes rendus de gestion mensuels d'une copropriété (Citya)
- copro_ag : convocations et PV d'assemblée générale d'une copropriété
- appt_ag : convocations et PV d'assemblée générale d'un immeuble
- appt_travaux : factures et devis de travaux pour un appartement
- copro_travaux : factures et devis de travaux pour une copropriété
- appt_conseil_syndical : documents du conseil syndical d'un immeuble
- copro_conseil_syndical : documents du conseil syndical d'une copropriété
- appt_tenant : documents relatifs au locataire (bail, état des lieux, quittances...)
- appt_fiscal : documents fiscaux annuels d'un appartement (aide à la déclaration des revenus fonciers, bilan annuel des charges, récapitulatif fiscal)
- AMBIGUOUS : si le document ne correspond à aucune catégorie OU si confiance < 80%

CONVENTIONS DE NOMMAGE (utilise la date du document si visible dans le PDF, sinon la date du mail) :
- factures               → YYYY-MM-DD-description-courte.pdf
- parking                → YYYY-MM_avis-echeance-parking.pdf
- appt_gestion           → YYYY-MM_appel-fonds-appt.pdf
- copro_appel_fonds      → YYYY-MM_appel-fonds-copro.pdf
- copro_gestion          → YYYY-MM_compte-rendu-gestion.pdf
- copro_ag               → YYYY-MM-DD_ag-copro.pdf
- appt_ag                → YYYY-MM-DD_ag-appt.pdf
- appt_travaux           → YYYY-MM-DD-description-appt.pdf
- copro_travaux          → YYYY-MM-DD-description-copro.pdf
- appt_conseil_syndical  → YYYY-MM-DD-description-cs-appt.pdf
- copro_conseil_syndical → YYYY-MM-DD-description-cs-copro.pdf
- appt_tenant            → YYYY-MM-DD-description-tenant.pdf
- appt_fiscal            → YYYY_aide-declaration-revenus-fonciers.pdf  ou  YYYY-bilan-charges-[periode].pdf

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown, sans texte autour :
{"folder": "<clé>", "filename": "<nom.pdf>", "confidence": <0.0-1.0>, "reason": "<raison courte en français>"}"""


# ─── SSM ─────────────────────────────────────────────────────────────────────

def load_params() -> dict:
    log.info("SSM: chargement des paramètres")
    ssm = boto3.client("ssm")
    names = [
        f"{SSM_PREFIX}/google/client_id",
        f"{SSM_PREFIX}/google/client_secret",
        f"{SSM_PREFIX}/google/refresh_token",
        f"{SSM_PREFIX}/anthropic/api_key",
        f"{SSM_PREFIX}/drive_folders",
    ]
    result = ssm.get_parameters(Names=names, WithDecryption=True)
    return {p["Name"]: p["Value"] for p in result["Parameters"]}


# ─── Google Auth ──────────────────────────────────────────────────────────────

def get_google_creds(params: dict) -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=params[f"{SSM_PREFIX}/google/refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=params[f"{SSM_PREFIX}/google/client_id"],
        client_secret=params[f"{SSM_PREFIX}/google/client_secret"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


# ─── Gmail helpers ────────────────────────────────────────────────────────────

def get_or_create_label(gmail, label_name: str) -> str:
    labels = gmail.users().labels().list(userId="me").execute()
    for label in labels.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    label = gmail.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    log.info(f"Label Gmail créé : '{label_name}'")
    return label["id"]


def list_inbox_messages_with_pdf(gmail) -> list:
    messages, page_token = [], None
    while True:
        kwargs = {"userId": "me", "q": GMAIL_QUERY}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = gmail.users().messages().list(**kwargs).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message(gmail, message_id: str) -> dict:
    return gmail.users().messages().get(userId="me", id=message_id, format="full").execute()


def extract_header(message: dict, name: str) -> str:
    for h in message["payload"]["headers"]:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def find_pdf_parts(payload: dict) -> list:
    parts = []
    mime = payload.get("mimeType", "")
    filename = payload.get("filename", "")
    attachment_id = payload.get("body", {}).get("attachmentId")
    is_pdf = (
        mime == "application/pdf"
        or (mime == "application/octet-stream" and filename.lower().endswith(".pdf"))
    )
    if is_pdf and attachment_id:
        parts.append(payload)
    for sub in payload.get("parts", []):
        parts.extend(find_pdf_parts(sub))
    return parts


def download_attachment(gmail, message_id: str, attachment_id: str) -> bytes:
    att = gmail.users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id
    ).execute()
    return base64.urlsafe_b64decode(att["data"])


def label_as_ambiguous(gmail, message_id: str, label_id: str) -> None:
    gmail.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute()


def trash_message(gmail, message_id: str) -> None:
    gmail.users().messages().trash(userId="me", id=message_id).execute()


# ─── Claude classification ────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 3000) -> str | None:
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        return text[:max_chars] if text else None
    except Exception:
        return None


def classify_pdf(client, pdf_bytes: bytes, sender: str, subject: str, date: str, filename: str) -> dict:
    pdf_text = extract_pdf_text(pdf_bytes)
    context = (
        f"Expéditeur : {sender}\n"
        f"Sujet : {subject}\n"
        f"Date du mail : {date}\n"
        f"Nom du fichier original : {filename}\n"
    )
    if pdf_text:
        content = [{"type": "text", "text": f"{context}\nContenu du PDF :\n{pdf_text}\n\nClasse ce document."}]
    else:
        log.info("    (PDF sans texte extractible, envoi en mode image)")
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": f"{context}\nClasse ce document."},
        ]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=CLASSIFICATION_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    return json.loads(raw)


# ─── Drive upload ─────────────────────────────────────────────────────────────

def upload_to_drive(drive, pdf_bytes: bytes, folder_id: str, filename: str) -> dict:
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    return drive.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id,name,webViewLink",
    ).execute()


# ─── Lambda entry point ───────────────────────────────────────────────────────

def handler(event, context):
    params        = load_params()
    drive_folders = json.loads(params[f"{SSM_PREFIX}/drive_folders"])
    creds         = get_google_creds(params)

    gmail  = build("gmail", "v1",  credentials=creds, cache_discovery=False)
    drive  = build("drive", "v3",  credentials=creds, cache_discovery=False)
    client = anthropic.Anthropic(api_key=params[f"{SSM_PREFIX}/anthropic/api_key"])

    ambiguous_label_id = get_or_create_label(gmail, AMBIGUOUS_LABEL)
    messages = list_inbox_messages_with_pdf(gmail)
    log.info(f"{len(messages)} mail(s) avec pièce jointe PDF trouvé(s)")

    ok_count = ambiguous_count = error_count = 0
    seen_message_ids: set[str] = set()

    for msg_ref in messages:
        message    = get_message(gmail, msg_ref["id"])
        sender     = extract_header(message, "from")
        subject    = extract_header(message, "subject")
        date       = extract_header(message, "date")
        message_id = extract_header(message, "message-id")

        if message_id and message_id in seen_message_ids:
            log.info(f"  ↷ Doublon ignoré ({subject})")
            continue
        if message_id:
            seen_message_ids.add(message_id)

        pdf_parts = find_pdf_parts(message["payload"])
        if not pdf_parts:
            continue

        log.info("=" * 60)
        log.info(f"De    : {sender}")
        log.info(f"Sujet : {subject}")
        log.info(f"Date  : {date}")

        classifications = []
        had_error = False

        for part in pdf_parts:
            fname         = part.get("filename", "document.pdf")
            attachment_id = part["body"]["attachmentId"]
            try:
                pdf_bytes = download_attachment(gmail, msg_ref["id"], attachment_id)
                result    = classify_pdf(client, pdf_bytes, sender, subject, date, fname)
                classifications.append((fname, pdf_bytes, result))
                log.info(
                    f"  [{result['confidence']:.0%}] {fname} → "
                    f"{result['folder']}/{result['filename']} ({result['reason']})"
                )
            except Exception as e:
                log.error(f"  Erreur sur {fname} : {e}")
                had_error = True

        if had_error:
            error_count += 1
            continue

        all_out_of_scope = all(r["folder"] == "AMBIGUOUS" for _, _, r in classifications)
        all_confident    = all(
            r["confidence"] >= CONFIDENCE_THRESHOLD and r["folder"] != "AMBIGUOUS"
            for _, _, r in classifications
        )

        if all_out_of_scope:
            log.info("  ↷ Ignoré (hors périmètre)")
        elif all_confident:
            try:
                for fname, pdf_bytes, result in classifications:
                    uploaded = upload_to_drive(drive, pdf_bytes, drive_folders[result["folder"]], result["filename"])
                    log.info(f"  ✓ Drive : {uploaded['name']}  {uploaded['webViewLink']}")
                trash_message(gmail, msg_ref["id"])
                log.info("  🗑 Mail mis à la corbeille")
                ok_count += 1
            except (HttpError, KeyError) as e:
                log.error(f"  Erreur Drive/Gmail : {e}")
                error_count += 1
        else:
            label_as_ambiguous(gmail, msg_ref["id"], ambiguous_label_id)
            log.info(f"  ⚠ Labellisé '{AMBIGUOUS_LABEL}'")
            ambiguous_count += 1

    summary = {"archived": ok_count, "ambiguous": ambiguous_count, "errors": error_count}
    log.info(f"Résultat : {summary}")
    return summary
