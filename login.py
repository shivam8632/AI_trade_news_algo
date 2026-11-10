# login.py
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import upstox_client
import config


def generate_login_url():
    """Generates the correct secure login link for Upstox API v2."""
    base_url = "https://api.upstox.com/v2/login/authorization/dialog"
    params = {
        "client_id": config.API_KEY,
        "redirect_uri": config.REDIRECT_URI,
        "response_type": "code"
    }
    login_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    print("👉 STEP 1: Open this secure URL in your web browser and log in:")
    print(f"\n{login_url}\n")
    return login_url


def extract_code_from_url(url):
    """Extracts the OAuth code from a redirect URL."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    code = query.get("code", [None])[0]
    return code


def wait_for_auth_code(timeout_seconds=120):
    """Starts a temporary local callback server and waits for the OAuth code."""
    result = {"code": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            code = extract_code_from_url(f"http://localhost{self.path}")
            if code:
                result["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h3>Authentication successful. You can close this window.</h3></body></html>"
                )
                print(f"\n✅ Redirect received. Auth code captured: {code}\n")
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Missing code parameter.")

            shutdown = threading.Thread(target=self.server.shutdown, daemon=True)
            shutdown.start()

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 8000), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("👉 STEP 2: Waiting for the browser redirect on http://localhost:8000/ ...")
    thread.join(timeout_seconds)
    server.shutdown()
    server.server_close()
    return result["code"]


def exchange_code_for_token(auth_code):
    """Exchanges your browser code for a functional Upstox Access Token."""
    api_instance = upstox_client.LoginApi()

    try:
        api_response = api_instance.token(
            api_version="2.0",
            code=auth_code,
            client_id=config.API_KEY,
            client_secret=config.API_SECRET,
            redirect_uri=config.REDIRECT_URI,
            grant_type="authorization_code"
        )
        print("✅ SUCCESS! Copy your new Access Token below:")
        print(f"\n{api_response.access_token}\n")
        print("👉 Action Required: Open config.py and paste this token inside ACCESS_TOKEN = \"\"")
        return api_response.access_token
    except Exception as e:
        print(f"❌ Error exchanging login code: {e}")
        return None


if __name__ == "__main__":
    login_url = generate_login_url()
    webbrowser.open(login_url)

    auth_code = wait_for_auth_code()

    if not auth_code:
        print("👉 STEP 2: The redirect did not complete. Please paste the 'code' parameter manually.")
        auth_code = input("Paste the browser 'code' value here and press Enter: ").strip()

    if auth_code:
        exchange_code_for_token(auth_code)
    else:
        print("No auth code received. Please retry the login flow.")
