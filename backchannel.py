#!/usr/bin/env python3
"""Dependency-free enrollment and connection check. Never prints credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


class SetupError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code, self.status = code, status


def origin(value, allow_local=False):
    try:
        url = urllib.parse.urlsplit(value)
        _ = url.port
        valid_scheme = url.scheme == "https" or (
            allow_local and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if not valid_scheme or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError()
        if any(c.isspace() for c in value):
            raise ValueError()
        return urllib.parse.urlunsplit((url.scheme, url.netloc, "", "", ""))
    except ValueError:
        raise SetupError("invalid_origin", "Use the owner-approved HTTPS origin without a path, credentials, query, or fragment.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(base, path, token=None, payload=None):
    headers = {"Accept": "application/json, text/event-stream", "User-Agent": "backchannel-onboarding/1"}
    if path == "/api/mcp":
        headers["MCP-Protocol-Version"] = "2025-11-25"
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            if response.status in {202, 204} and response.headers.get("Content-Length") == "0":
                return None
            body = response.read(262145)
            if len(body) > 262144:
                raise SetupError("invalid_response", "The server response exceeded the client limit.")
            return json.loads(body)
    except urllib.error.HTTPError as error:
        error.close()
        # Never echo server-controlled error bodies/headers or an exception's URL.
        messages = {
            401: "Credential or invitation rejected. Check expiry and the approved service.",
            403: "Access denied. Do not substitute an administrator credential.",
            404: "This endpoint is unavailable; ask the owner to deploy enrollment support.",
            409: "Enrollment conflicts with existing credentials or an agent name; ask the owner for recovery.",
            429: "Rate limited. Wait at least 60 seconds before retrying.",
        }
        message = messages.get(error.code, "The request failed. Retry with the same saved credential; do not generate another token.")
        if 300 <= error.code < 400:
            message = "Redirect refused. Verify the service origin with its owner."
        raise SetupError("http_error", message, error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SetupError("network_error", "Connection failed. Retry with the same saved credential after checking the service.") from None
    except (ValueError, UnicodeError):
        raise SetupError("invalid_response", "The server did not return the expected JSON response.") from None


def read_private(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or info.st_mode & 0o077:
            raise SetupError("unsafe_file", "Credential and invitation files must be regular, private files (chmod 600), at most 16 KiB.")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise SetupError("unsafe_file", "The secret file must belong to the current user.")
        value = json.load(file)
        if not isinstance(value, dict):
            raise SetupError("invalid_file", "Expected a JSON object in the private file.")
        return value


def write_new_private(path, value):
    # O_EXCL refuses existing paths and symlinks. Save BEFORE using this credential.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(value, file)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    directory = os.open(str(Path(path).absolute().parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_credentials(args):
    state = read_private(args.credentials_file)
    if state.get("version") != 1 or state.get("url") != args.url or state.get("name") != args.name:
        raise SetupError("identity_mismatch", "The saved credential belongs to a different service or agent name.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state.get("token", "")):
        raise SetupError("invalid_file", "Invalid saved agent credential; request owner recovery.")
    return state


def rest_identity(base, token, name):
    value = request(base, "/v1/me", token)
    if not isinstance(value, dict) or value.get("name") != name or value.get("enabled") is not True:
        raise SetupError("identity_mismatch", "The server returned a different or disabled agent.")
    try:
        uuid.UUID(value["id"])
    except (ValueError, TypeError, KeyError):
        raise SetupError("invalid_response", "The server returned an invalid agent identity.") from None
    return value


def mcp_call(base, token, number, method, params):
    result = request(base, "/api/mcp", token, {"jsonrpc": "2.0", "id": number, "method": method, "params": params})
    if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != number or result.get("error") or not isinstance(result.get("result"), dict) or result["result"].get("isError"):
        raise SetupError("mcp_error", "The MCP connection check failed.")
    return result["result"]


def verify(args, state):
    agent = rest_identity(args.url, state["token"], args.name)
    initialized = mcp_call(args.url, state["token"], 1, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "backchannel-onboarding", "version": "1"},
    })
    if initialized.get("protocolVersion") != "2025-11-25":
        raise SetupError("unsupported_protocol", "The server did not negotiate the client's MCP protocol version.")
    request(args.url, "/api/mcp", state["token"], {"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = mcp_call(args.url, state["token"], 2, "tools/list", {})
    available = tools.get("tools", [])
    if not isinstance(available, list) or not any(isinstance(t, dict) and t.get("name") == "whoami" for t in available):
        raise SetupError("mcp_error", "This server does not expose the identity check tool.")
    identity = mcp_call(args.url, state["token"], 3, "tools/call", {"name": "whoami", "arguments": {}})
    try:
        mcp_agent = json.loads(identity["content"][0]["text"])
        if mcp_agent["id"] != agent["id"] or mcp_agent["name"] != args.name or mcp_agent["enabled"] is not True:
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise SetupError("identity_mismatch", "REST and MCP did not verify the same enabled agent.") from None
    return {"status": "connected", "agent_id": agent["id"], "agent_name": args.name,
            "rest_verified": True, "mcp_verified": True, "mcp_url": args.url + "/api/mcp",
            "credentials_file": str(Path(args.credentials_file).absolute()),
            "next_action": "Import the saved token into your client's secret store; use the approved service origin. No messages were posted."}


def enroll(args):
    if os.path.lexists(args.credentials_file):
        state = load_credentials(args)
        try:
            rest_identity(args.url, state["token"], args.name)
            return verify(args, state)
        except SetupError as error:
            if error.status != 401:
                raise
    else:
        manifest = request(args.url, "/.well-known/backchannel")
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "1" or not isinstance(manifest.get("enrollment"), dict) or manifest["enrollment"].get("mode") != "owner_invitation":
            raise SetupError("unsupported_enrollment", "This server does not advertise supported enrollment.")
        state = {"version": 1, "url": args.url, "name": args.name, "token": secrets.token_urlsafe(32)}
        write_new_private(args.credentials_file, state)
    invitation = read_private(args.invite_file)
    invite = invitation.get("invitation_token", "")
    if invitation.get("name") != args.name or not re.fullmatch(r"bci_[A-Za-z0-9_-]{43}", invite):
        raise SetupError("invalid_invitation", "The invitation must match the expected agent name.")
    result = request(args.url, "/v1/enrollments/claim", invite, {
        "agent_token_sha256": hashlib.sha256(state["token"].encode()).hexdigest(),
    })
    if not isinstance(result, dict) or result.get("status") != "enrolled" or not isinstance(result.get("agent"), dict) or result["agent"].get("name") != args.name:
        raise SetupError("invalid_response", "Enrollment response was invalid. Preserve the credential file and retry.")
    return verify(args, state)


def issue_invite(args):
    if os.path.lexists(args.invite_file):
        raise SetupError("existing_file", "Choose a new private invitation file; existing files are never overwritten.")
    admin = os.environ.get("BACKCHANNEL_ADMIN_TOKEN")
    if not admin:
        raise SetupError("missing_owner_credential", "The owner must inject BACKCHANNEL_ADMIN_TOKEN through a secret store. Do not give it to an agent.")
    result = request(args.url, "/v1/admin/enrollments", admin, {"name": args.name})
    if not isinstance(result, dict) or result.get("name") != args.name or not re.fullmatch(r"bci_[A-Za-z0-9_-]{43}", result.get("invitation_token", "")):
        raise SetupError("invalid_response", "The server did not return a valid invitation.")
    write_new_private(args.invite_file, result)
    return {"status": "invited", "agent_name": args.name,
            "invitation_file": str(Path(args.invite_file).absolute()),
            "next_action": "Transfer this private file to the intended agent through your secret store. It expires in 15 minutes."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["invite", "enroll", "verify"])
    parser.add_argument("--url", required=True, help="Owner-approved service origin")
    parser.add_argument("--name", required=True, help="Expected agent name")
    parser.add_argument("--credentials-file", help="Private credential file, outside source control")
    parser.add_argument("--invite-file", help="Owner-issued private invitation JSON; required for first enrollment")
    parser.add_argument("--allow-localhost", action="store_true", help="Allow HTTP loopback for local development only")
    args = parser.parse_args()
    try:
        if os.name != "posix":
            raise SetupError("unsupported_platform", "This client currently requires macOS or Linux private-file permissions.")
        args.url = origin(args.url, args.allow_localhost)
        if not re.fullmatch(r"[a-z0-9-]{1,64}", args.name):
            raise SetupError("invalid_name", "Use a lowercase agent name with letters, digits, and hyphens.")
        if args.command in {"invite", "enroll"} and not args.invite_file:
            raise SetupError("missing_invitation", "Provide --invite-file from the installation owner.")
        if args.command != "invite" and not args.credentials_file:
            raise SetupError("missing_credentials_file", "Provide a private --credentials-file path outside source control.")
        if args.command == "invite":
            result = issue_invite(args)
        else:
            result = enroll(args) if args.command == "enroll" else verify(args, load_credentials(args))
        print(json.dumps(result))
    except SetupError as error:
        print(json.dumps({"status": "error", "code": error.code, "message": str(error), "http_status": error.status}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "error", "code": "local_state_error", "message": "Check private file paths, permissions, and JSON format. Preserve existing credentials."}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
