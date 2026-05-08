import base64
import json
from unittest.mock import MagicMock, patch, call

import pytest

import handler


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_gmail_message(msg_id: str = "<msg@test>", with_pdf: bool = True) -> dict:
    parts = []
    if with_pdf:
        parts.append({
            "mimeType": "application/pdf",
            "filename": "doc.pdf",
            "body": {"attachmentId": "att1"},
            "parts": [],
        })
    return {
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {},
            "parts": parts,
            "headers": [
                {"name": "From", "value": "sender@example.com"},
                {"name": "Subject", "value": "Test"},
                {"name": "Date", "value": "2026-05-01"},
                {"name": "Message-ID", "value": msg_id},
            ],
        }
    }


def _make_gmail_mock(messages: list | None = None, label_exists: bool = False):
    gmail = MagicMock()
    existing = [{"name": "pdf-a-classer", "id": "L_exist"}] if label_exists else []
    # .return_value (pas d'appel direct) pour ne pas polluer les compteurs d'appels
    gmail.users().labels().list.return_value.execute.return_value = {"labels": existing}
    gmail.users().labels().create.return_value.execute.return_value = {"id": "L_new"}
    msg_list = messages or []
    gmail.users().messages().list.return_value.execute.return_value = {
        "messages": msg_list
    }
    gmail.users().messages().get.return_value.execute.return_value = (
        _make_gmail_message()
    )
    gmail.users().messages().attachments.return_value.get.return_value.execute.return_value = {
        "data": base64.urlsafe_b64encode(b"pdfbytes").decode()
    }
    return gmail


def _make_drive_mock():
    drive = MagicMock()
    drive.files().create().execute.return_value = {
        "id": "f1",
        "name": "doc.pdf",
        "webViewLink": "http://drive/f1",
    }
    return drive


def _confident_classification(folder: str = "factures") -> dict:
    return {
        "folder": folder,
        "filename": "2026-05-01-test.pdf",
        "confidence": 0.95,
        "reason": "ok",
    }


# ── get_google_creds ──────────────────────────────────────────────────────────

class TestGetGoogleCreds:
    def test_builds_credentials_and_refreshes(self):
        params = {
            "/mail-sort-pdfs/google/refresh_token": "rt",
            "/mail-sort-pdfs/google/client_id": "cid",
            "/mail-sort-pdfs/google/client_secret": "cs",
        }
        mock_creds = MagicMock()
        with patch.object(handler, "Credentials", return_value=mock_creds) as mock_cls, \
             patch.object(handler, "Request") as mock_request:
            result = handler.get_google_creds(params)

        mock_cls.assert_called_once_with(
            token=None,
            refresh_token="rt",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="cs",
            scopes=handler.SCOPES,
        )
        mock_creds.refresh.assert_called_once_with(mock_request.return_value)
        assert result is mock_creds


# ── load_params ───────────────────────────────────────────────────────────────

class TestLoadParams:
    def test_fetches_five_params_with_decryption(self):
        ssm = MagicMock()
        ssm.get_parameters.return_value = {
            "Parameters": [
                {"Name": "/mail-sort-pdfs/google/client_id", "Value": "cid"},
                {"Name": "/mail-sort-pdfs/google/client_secret", "Value": "cs"},
                {"Name": "/mail-sort-pdfs/google/refresh_token", "Value": "rt"},
                {"Name": "/mail-sort-pdfs/anthropic/api_key", "Value": "sk"},
                {"Name": "/mail-sort-pdfs/drive_folders", "Value": "{}"},
            ]
        }
        with patch.object(handler.boto3, "client", return_value=ssm):
            params = handler.load_params()

        ssm.get_parameters.assert_called_once_with(
            Names=[
                "/mail-sort-pdfs/google/client_id",
                "/mail-sort-pdfs/google/client_secret",
                "/mail-sort-pdfs/google/refresh_token",
                "/mail-sort-pdfs/anthropic/api_key",
                "/mail-sort-pdfs/drive_folders",
            ],
            WithDecryption=True,
        )
        assert params["/mail-sort-pdfs/anthropic/api_key"] == "sk"
        assert params["/mail-sort-pdfs/google/client_id"] == "cid"


# ── get_or_create_label ───────────────────────────────────────────────────────

class TestGetOrCreateLabel:
    def test_returns_id_of_existing_label(self):
        gmail = _make_gmail_mock(label_exists=True)
        result = handler.get_or_create_label(gmail, "pdf-a-classer")
        assert result == "L_exist"
        gmail.users().labels().create.assert_not_called()

    def test_creates_label_when_not_found(self):
        gmail = _make_gmail_mock(label_exists=False)
        result = handler.get_or_create_label(gmail, "pdf-a-classer")
        assert result == "L_new"

    def test_does_not_match_on_partial_name(self):
        gmail = MagicMock()
        gmail.users().labels().list().execute.return_value = {
            "labels": [{"name": "pdf-a-classer-old", "id": "L_old"}]
        }
        gmail.users().labels().create().execute.return_value = {"id": "L_new"}
        result = handler.get_or_create_label(gmail, "pdf-a-classer")
        assert result == "L_new"


# ── list_inbox_messages_with_pdf ──────────────────────────────────────────────

class TestListInboxMessagesWithPdf:
    def test_single_page(self):
        gmail = MagicMock()
        gmail.users().messages().list.return_value.execute.return_value = {
            "messages": [{"id": "1"}, {"id": "2"}]
        }
        assert handler.list_inbox_messages_with_pdf(gmail) == [{"id": "1"}, {"id": "2"}]

    def test_pagination_fetches_all_pages(self):
        gmail = MagicMock()
        gmail.users().messages().list.return_value.execute.side_effect = [
            {"messages": [{"id": "1"}], "nextPageToken": "tok"},
            {"messages": [{"id": "2"}]},
        ]
        result = handler.list_inbox_messages_with_pdf(gmail)
        assert len(result) == 2
        assert result[0]["id"] == "1"
        assert result[1]["id"] == "2"

    def test_empty_inbox(self):
        gmail = MagicMock()
        gmail.users().messages().list.return_value.execute.return_value = {
            "messages": []
        }
        assert handler.list_inbox_messages_with_pdf(gmail) == []

    def test_missing_messages_key(self):
        gmail = MagicMock()
        gmail.users().messages().list.return_value.execute.return_value = {}
        assert handler.list_inbox_messages_with_pdf(gmail) == []


# ── find_pdf_parts ────────────────────────────────────────────────────────────

class TestFindPdfParts:
    def test_flat_pdf_attachment(self):
        payload = {
            "mimeType": "application/pdf",
            "filename": "doc.pdf",
            "body": {"attachmentId": "att1"},
            "parts": [],
        }
        result = handler.find_pdf_parts(payload)
        assert len(result) == 1
        assert result[0]["body"]["attachmentId"] == "att1"

    def test_octet_stream_with_pdf_extension(self):
        payload = {
            "mimeType": "application/octet-stream",
            "filename": "doc.PDF",
            "body": {"attachmentId": "att2"},
            "parts": [],
        }
        assert len(handler.find_pdf_parts(payload)) == 1

    def test_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {}, "parts": []},
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att3"},
                    "parts": [],
                },
            ],
        }
        result = handler.find_pdf_parts(payload)
        assert len(result) == 1
        assert result[0]["body"]["attachmentId"] == "att3"

    def test_no_pdf_returns_empty(self):
        payload = {"mimeType": "text/html", "filename": "", "body": {}, "parts": []}
        assert handler.find_pdf_parts(payload) == []

    def test_pdf_without_attachment_id_excluded(self):
        payload = {
            "mimeType": "application/pdf",
            "filename": "inline.pdf",
            "body": {},
            "parts": [],
        }
        assert handler.find_pdf_parts(payload) == []

    def test_multiple_pdfs_in_one_message(self):
        payload = {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {},
            "parts": [
                {"mimeType": "application/pdf", "filename": "a.pdf",
                 "body": {"attachmentId": "a1"}, "parts": []},
                {"mimeType": "application/pdf", "filename": "b.pdf",
                 "body": {"attachmentId": "b2"}, "parts": []},
            ],
        }
        assert len(handler.find_pdf_parts(payload)) == 2


# ── extract_pdf_text ──────────────────────────────────────────────────────────

class TestExtractPdfText:
    def _mock_reader(self, texts: list[str | None]):
        reader = MagicMock()
        reader.pages = [MagicMock() for _ in texts]
        for page, text in zip(reader.pages, texts):
            page.extract_text.return_value = text
        return reader

    def test_extracts_and_joins_pages(self):
        reader = self._mock_reader(["Page 1", "Page 2"])
        with patch.object(handler.pypdf, "PdfReader", return_value=reader):
            result = handler.extract_pdf_text(b"pdf")
        assert "Page 1" in result
        assert "Page 2" in result

    def test_returns_none_for_empty_text(self):
        reader = self._mock_reader([""])
        with patch.object(handler.pypdf, "PdfReader", return_value=reader):
            assert handler.extract_pdf_text(b"pdf") is None

    def test_returns_none_on_exception(self):
        with patch.object(handler.pypdf, "PdfReader", side_effect=Exception("corrupt")):
            assert handler.extract_pdf_text(b"bad") is None

    def test_truncates_at_max_chars(self):
        reader = self._mock_reader(["A" * 5000])
        with patch.object(handler.pypdf, "PdfReader", return_value=reader):
            result = handler.extract_pdf_text(b"pdf", max_chars=100)
        assert len(result) == 100

    def test_handles_none_from_extract_text(self):
        reader = self._mock_reader([None])
        with patch.object(handler.pypdf, "PdfReader", return_value=reader):
            assert handler.extract_pdf_text(b"pdf") is None


# ── classify_pdf ──────────────────────────────────────────────────────────────

class TestClassifyPdf:
    def _make_client(self, response_json: dict):
        client = MagicMock()
        resp = MagicMock()
        resp.content = [MagicMock()]
        resp.content[0].text = json.dumps(response_json)
        client.messages.create.return_value = resp
        return client

    def _good_result(self):
        return {"folder": "factures", "filename": "f.pdf", "confidence": 0.9, "reason": "r"}

    def test_text_pdf_sends_text_content(self):
        client = self._make_client(self._good_result())
        with patch.object(handler, "extract_pdf_text", return_value="invoice text"):
            handler.classify_pdf(client, b"pdf", "s", "sub", "d", "f.pdf")
        content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert "invoice text" in content[0]["text"]

    def test_scanned_pdf_sends_document_content(self):
        client = self._make_client(self._good_result())
        with patch.object(handler, "extract_pdf_text", return_value=None):
            handler.classify_pdf(client, b"pdfbytes", "s", "sub", "d", "f.pdf")
        content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert content[0]["type"] == "document"
        assert content[0]["source"]["media_type"] == "application/pdf"
        assert content[1]["type"] == "text"

    def test_strips_markdown_code_fence(self):
        client = MagicMock()
        raw = '```json\n{"folder":"factures","filename":"f.pdf","confidence":0.9,"reason":"r"}\n```'
        resp = MagicMock()
        resp.content = [MagicMock()]
        resp.content[0].text = raw
        client.messages.create.return_value = resp
        with patch.object(handler, "extract_pdf_text", return_value="text"):
            result = handler.classify_pdf(client, b"pdf", "s", "sub", "d", "f.pdf")
        assert result["folder"] == "factures"

    def test_uses_correct_model(self):
        client = self._make_client(self._good_result())
        with patch.object(handler, "extract_pdf_text", return_value="text"):
            handler.classify_pdf(client, b"pdf", "s", "sub", "d", "f.pdf")
        assert client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"

    def test_includes_mail_metadata_in_prompt(self):
        client = self._make_client(self._good_result())
        with patch.object(handler, "extract_pdf_text", return_value="text"):
            handler.classify_pdf(client, b"pdf", "Citya Flaubert", "Appel fonds", "2026-05-01", "appel.pdf")
        content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        prompt_text = content[0]["text"]
        assert "Citya Flaubert" in prompt_text
        assert "Appel fonds" in prompt_text
        assert "2026-05-01" in prompt_text
        assert "appel.pdf" in prompt_text


# ── extract_header ────────────────────────────────────────────────────────────

class TestExtractHeader:
    def test_returns_empty_string_when_header_absent(self):
        message = {
            "payload": {
                "headers": [{"name": "From", "value": "a@b.com"}]
            }
        }
        assert handler.extract_header(message, "Subject") == ""


# ── download_attachment ───────────────────────────────────────────────────────

class TestDownloadAttachment:
    def test_decodes_base64_data(self):
        gmail = MagicMock()
        encoded = base64.urlsafe_b64encode(b"pdf content").decode()
        gmail.users().messages().attachments().get().execute.return_value = {
            "data": encoded
        }
        result = handler.download_attachment(gmail, "msg1", "att1")
        assert result == b"pdf content"


# ── handler (intégration) ─────────────────────────────────────────────────────

_DRIVE_FOLDERS_JSON = json.dumps({k: "test_id" for k in [
    "factures", "parking", "appt_gestion", "copro_appel_fonds",
    "copro_gestion", "copro_ag", "appt_ag", "appt_travaux",
    "copro_travaux", "appt_conseil_syndical", "copro_conseil_syndical",
    "appt_tenant", "appt_fiscal",
]})

PARAMS = {
    "/mail-sort-pdfs/google/client_id": "cid",
    "/mail-sort-pdfs/google/client_secret": "cs",
    "/mail-sort-pdfs/google/refresh_token": "rt",
    "/mail-sort-pdfs/anthropic/api_key": "sk",
    "/mail-sort-pdfs/drive_folders": _DRIVE_FOLDERS_JSON,
}


class TestHandlerIntegration:
    def _run(self, gmail, drive, classification=None, messages=None, classify_side_effect=None):
        if messages is not None:
            gmail.users().messages().list.return_value.execute.return_value = {
                "messages": messages
            }
        else:
            gmail.users().messages().list.return_value.execute.return_value = {
                "messages": [{"id": "m1"}]
            }

        classify_patch = (
            patch.object(handler, "classify_pdf", side_effect=classify_side_effect)
            if classify_side_effect
            else patch.object(handler, "classify_pdf", return_value=classification or _confident_classification())
        )

        with patch.object(handler, "load_params", return_value=PARAMS), \
             patch.object(handler, "get_google_creds", return_value=MagicMock()), \
             patch.object(handler, "build", side_effect=[gmail, drive]), \
             patch.object(handler.anthropic, "Anthropic", return_value=MagicMock()), \
             classify_patch:
            return handler.handler({}, None)

    def test_confident_result_uploads_and_trashes(self):
        gmail = _make_gmail_mock(messages=[{"id": "m1"}])
        drive = _make_drive_mock()
        result = self._run(gmail, drive, _confident_classification())
        assert result == {"archived": 1, "ambiguous": 0, "errors": 0}
        gmail.users().messages().trash.assert_called_once()

    def test_low_confidence_labels_message_as_ambiguous(self):
        gmail = _make_gmail_mock(messages=[{"id": "m1"}])
        drive = _make_drive_mock()
        result = self._run(gmail, drive, {
            "folder": "factures", "filename": "f.pdf", "confidence": 0.5, "reason": "hésitant"
        })
        assert result == {"archived": 0, "ambiguous": 1, "errors": 0}
        gmail.users().messages().modify.assert_called_once()
        gmail.users().messages().trash.assert_not_called()

    def test_all_ambiguous_folder_ignores_message(self):
        gmail = _make_gmail_mock(messages=[{"id": "m1"}])
        drive = _make_drive_mock()
        result = self._run(gmail, drive, {
            "folder": "AMBIGUOUS", "filename": "f.pdf", "confidence": 0.3, "reason": "inconnu"
        })
        assert result == {"archived": 0, "ambiguous": 0, "errors": 0}
        gmail.users().messages().modify.assert_not_called()
        gmail.users().messages().trash.assert_not_called()

    def test_deduplicates_messages_by_message_id(self):
        gmail = _make_gmail_mock(messages=[{"id": "m1"}, {"id": "m2"}])
        drive = _make_drive_mock()
        # Les deux messages ont le même Message-ID → seul le premier est traité
        gmail.users().messages().get.return_value.execute.return_value = (
            _make_gmail_message(msg_id="<same@test>")
        )
        # Passe explicitement les 2 messages pour que _run ne les écrase pas
        result = self._run(gmail, drive, _confident_classification(),
                           messages=[{"id": "m1"}, {"id": "m2"}])
        assert result["archived"] == 1

    def test_drive_upload_error_increments_error_count(self):
        gmail = _make_gmail_mock(messages=[{"id": "m1"}])
        drive = _make_drive_mock()
        drive.files().create.return_value.execute.side_effect = handler.HttpError

        result = self._run(gmail, drive, _confident_classification())
        assert result == {"archived": 0, "ambiguous": 0, "errors": 1}

    def test_classify_exception_counts_as_error(self):
        gmail = _make_gmail_mock(messages=[{"id": "m1"}])
        drive = _make_drive_mock()
        result = self._run(gmail, drive, classify_side_effect=Exception("API down"))
        assert result == {"archived": 0, "ambiguous": 0, "errors": 1}

    def test_no_messages_returns_zeros(self):
        gmail = _make_gmail_mock(messages=[])
        drive = _make_drive_mock()
        result = self._run(gmail, drive, messages=[])
        assert result == {"archived": 0, "ambiguous": 0, "errors": 0}

    def test_message_without_pdf_attachment_skipped(self):
        gmail = _make_gmail_mock(messages=[{"id": "m1"}])
        drive = _make_drive_mock()
        gmail.users().messages().get.return_value.execute.return_value = (
            _make_gmail_message(with_pdf=False)
        )
        result = self._run(gmail, drive)
        assert result == {"archived": 0, "ambiguous": 0, "errors": 0}
