import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))


class _HttpError(Exception):
    pass


# Mock external packages before handler.py is imported during test collection
for _mod in [
    "anthropic",
    "boto3",
    "pypdf",
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.http",
]:
    sys.modules.setdefault(_mod, MagicMock())

_errors_mock = MagicMock()
_errors_mock.HttpError = _HttpError
sys.modules["googleapiclient.errors"] = _errors_mock
