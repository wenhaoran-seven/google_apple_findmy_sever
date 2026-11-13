"""Entry point used by the embedded Python Windows distribution."""
import argparse
import getpass
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent


def self_test():
    from findmy import KeyPair
    from findmy.util.tls import apple_trust_context
    from localfindmy.storage import Vault
    from localfindmy.mac_anisette import MacAnisetteProvider
    from localfindmy.app import app
    with tempfile.TemporaryDirectory() as directory:
        vault = Vault(Path(directory))
        vault.write("probe", {"ok": True})
        assert vault.read("probe") == {"ok": True}
    assert len(KeyPair.new().adv_key_bytes) == 28
    assert apple_trust_context().check_hostname
    provider = MacAnisetteProvider()
    assert provider._run("is_machine_provisioned", -2).ret == -45061
    print("PASS: Python, web server, TLS certificates, key generation, encrypted vault, CoreADI/Unicorn.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    os.chdir(ROOT)
    os.environ["FINDMY_DATA_DIR"] = str(ROOT / "data")
    os.environ["FINDMY_PORT"] = str(args.port)
    if args.self_test:
        self_test()
        return
    if args.restore:
        from localfindmy.transfer import restore
        print("Import device keys, history and schedules. Apple credentials are not included.")
        print("Use a newly extracted folder. Stop this copy of the service before importing.")
        password = getpass.getpass("Migration password (input hidden): ")
        result = restore((ROOT / "migration.enc").read_bytes(), password, ROOT / "data")
        print(f"Imported {result['devices']} devices and {result['reports']} reports.")
        print("Now start the app, sign in to Apple, then enable the desired tasks.")
        return
    if not 1024 <= args.port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            raise RuntimeError(f"Port {args.port} is already in use. Stop the other service or use --port 8766.") from None
    url = f"http://127.0.0.1:{args.port}/"
    print(f"FindMy Local: {url}\nKeep this window open. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        def open_when_ready():
            for _ in range(60):
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        if response.status == 200:
                            webbrowser.open(url)
                            return
                except Exception:
                    time.sleep(0.5)
        threading.Thread(target=open_when_ready, daemon=True).start()
    import uvicorn
    uvicorn.run("localfindmy.app:app", host="127.0.0.1", port=args.port,
                access_log=False, log_level="critical")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Do not print account data, decrypted transfer data or tracebacks.
        print(f"Unable to complete ({type(exc).__name__}). Check the folder, port or migration password.")
        sys.exit(1)
