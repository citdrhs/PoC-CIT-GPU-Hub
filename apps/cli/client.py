"""HTTP transport and scoped credential handling for the terminal client."""
import ipaddress
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit


class ClientError(Exception):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def segment(value):
    if not value or value in (".", ".."):
        raise ClientError("INVALID_ID", "Enter a valid resource ID.")
    return quote(value, safe="")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer credential to a redirected endpoint."""

    def redirect_request(self, *args, **kwargs):
        return None


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Client:
    def __init__(self, url, token=None):
        self.url = url.rstrip("/")
        parsed = urlsplit(self.url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ClientError("INVALID_URL", "Specify an API origin without credentials, query, or fragment.")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback(parsed.hostname)):
            raise ClientError("INSECURE_URL", "Use HTTPS for a remote GPU Hub or HTTP on loopback.")
        self.token = token
        handlers = [NoRedirect()]
        if is_loopback(parsed.hostname):
            handlers.append(urllib.request.ProxyHandler({}))
        self.transport = urllib.request.build_opener(*handlers)

    def open(self, path, body=None, *, key=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if key:
            headers["Idempotency-Key"] = key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self.url + "/v1" + path, data=data, headers=headers)
        try:
            return self.transport.open(request, timeout=15)
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    info = json.loads(exc.read(32768)).get("error", {})
                except (ValueError, UnicodeError):
                    info = {}
            raise ClientError(info.get("code", "HTTP_ERROR"), info.get("message", str(exc)),
                              retryable=exc.code >= 500 or exc.code == 429) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ClientError("CONNECTION_ERROR", str(exc), retryable=True) from exc

    def call(self, path, body=None, *, key=None):
        with self.open(path, body, key=key) as response:
            return json.load(response)

    def login_dev(self, identity):
        if not is_loopback(urlsplit(self.url).hostname):
            raise ClientError("LOCAL_ONLY", "Development identity selection requires a loopback API.")
        self.token = None
        session = self.call("/auth/dev", {"identity": identity})
        self.token = session["token"]
        return session["user"]

    def get_credentials(self):
        self.token = os.environ.get("GPUHUB_TOKEN")
        if self.token:
            return
        try:
            import keyring
        except ImportError:
            pass
        else:
            try:
                self.token = keyring.get_password("gpuhub/" + self.url, "session")
            except (keyring.errors.KeyringError, RuntimeError) as exc:
                raise ClientError("KEYRING_UNAVAILABLE",
                                  "Unlock your OS keyring, set GPUHUB_TOKEN, or use a local --identity.") from exc
        if not self.token:
            raise ClientError("LOGIN_REQUIRED", "Run gpuhub login, set GPUHUB_TOKEN, or use --identity locally.")

    def save_credentials(self):
        try:
            import keyring
        except ImportError as exc:
            raise ClientError("KEYRING_UNAVAILABLE",
                              "Install the cli extra to save a credential, or use --identity locally.") from exc
        try:
            keyring.set_password("gpuhub/" + self.url, "session", self.token)
        except (keyring.errors.KeyringError, RuntimeError) as exc:
            raise ClientError("KEYRING_UNAVAILABLE",
                              "Install the keyring extra and unlock an OS keyring, or use --identity locally.") from exc
