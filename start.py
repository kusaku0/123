"""One-click local launcher; creates a virtualenv and opens the website."""
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser


def main():
    if sys.version_info < (3, 11):
        raise RuntimeError("Install Python 3.11 or newer from python.org first.")
    root = Path(__file__).resolve().parent
    os.chdir(root)
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("Preparing Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(root / ".venv")
    print("Installing dependencies (first launch may take a few minutes)...", flush=True)
    subprocess.run([str(python), "-m", "pip", "install", "--require-hashes", "-r", str(root / "requirements.txt")], check=True)
    subprocess.run([str(python), "-m", "playwright", "install", "chromium"], check=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    # A separate browser window/profile on this PC lets the user complete checks.
    environment.setdefault("LOCAL_BROWSER_MODE", "true")
    process = subprocess.Popen([str(python), "-m", "pricebot.web"], env=environment)
    try:
        # This is a local readiness request only; remote requests keep host settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError("The website could not start. See the error above.")
            try:
                with opener.open("http://127.0.0.1:8000/api/health", timeout=1) as response:
                    ready = response.status == 200
                if ready:
                    webbrowser.open("http://127.0.0.1:8000")
                    print("Website started. Keep this window open. Stop with Ctrl+C.", flush=True)
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Website startup timed out. See the error above.")
        process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        sys.exit(1)
