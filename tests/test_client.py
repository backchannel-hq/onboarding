import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("backchannel", Path(__file__).parents[1] / "backchannel.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class ClientTests(unittest.TestCase):
    def test_secret_files_are_private_exclusive_and_refuse_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.json"
            client.write_new_private(path, {"token": "private-fixture"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(client.read_private(path), {"token": "private-fixture"})
            with self.assertRaises(FileExistsError):
                client.write_new_private(path, {})
            path.chmod(0o644)
            with self.assertRaises(client.SetupError):
                client.read_private(path)
            path.chmod(0o600)
            alias = Path(folder) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                client.read_private(alias)

    def test_only_explicit_https_origins_or_opted_in_loopback(self):
        self.assertEqual(client.origin("https://api.example.com/"), "https://api.example.com")
        for value in ["http://api.example.com", "https://user:pass@api.example.com", "https://api.example.com/path", "https://api.example.com/?token=secret", "https://api.example.com/#secret", "https://api.example.com:invalid", "https://api.example.com\n"]:
            with self.assertRaises(client.SetupError):
                client.origin(value)
        with self.assertRaises(client.SetupError):
            client.origin("http://127.0.0.1:8080")
        self.assertEqual(client.origin("http://127.0.0.1:8080", True), "http://127.0.0.1:8080")
        with self.assertRaises(client.SetupError):
            client.origin("http://127.0.0.1.evil.example", True)

    def test_redirects_never_forward_credentials_and_errors_do_not_echo(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/leak")
                else:
                    self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"private-fixture"}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for route in ["/redirect", "/failure"]:
                with self.assertRaises(client.SetupError) as caught:
                    client.request(f"http://127.0.0.1:{server.server_port}", route, "private-fixture")
                self.assertNotIn("private-fixture", str(caught.exception))
            self.assertEqual(seen, ["/redirect", "/failure"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
