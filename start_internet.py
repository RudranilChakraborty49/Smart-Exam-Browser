"""
=============================================================
  INTERNET SETUP — Using ngrok to expose server online
  Run this INSTEAD of server.py when going over the internet
=============================================================
  STEP 1: Install ngrok
    → Go to https://ngrok.com/download
    → Create a free account and get your auth token
    → Run: ngrok config add-authtoken YOUR_TOKEN_HERE

  STEP 2: Install pyngrok
    pip install pyngrok

  STEP 3: Run this file
    python start_internet.py

  Your public URL will be printed — share it with students!
=============================================================
"""

import threading
import time
from pyngrok import ngrok, conf

# ── Configure ngrok (paste your token here once) ──
# Get free token at: https://dashboard.ngrok.com/get-started/your-authtoken
NGROK_AUTH_TOKEN = "3BAyNeZy7eZtkE15SLKirjimz9G_3kLA4kPDityNm3JCNbcK4"  # ← Paste your token here

def start_server():
    """Start the Flask server in a background thread."""
    import server  # Import and run server.py
    # The server.socketio.run() at the bottom of server.py is what starts it

def main():
    print("=" * 60)
    print("  SEB — INTERNET MODE (via ngrok)")
    print("=" * 60)

    # Set auth token if provided
    if NGROK_AUTH_TOKEN:
        conf.get_default().auth_token = NGROK_AUTH_TOKEN
    else:
        print("\n⚠  WARNING: No ngrok auth token set.")
        print("   Get a free token at: https://dashboard.ngrok.com")
        print("   Add it to NGROK_AUTH_TOKEN in this file.\n")

    # Open a tunnel to port 5000
    print("Starting ngrok tunnel on port 5000...")
    tunnel = ngrok.connect(5000, "http")
    public_url = tunnel.public_url

    # Convert http to https (ngrok gives https automatically)
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[7:]

    print("\n" + "=" * 60)
    print(f"  ✅ SERVER IS LIVE ON THE INTERNET!")
    print(f"")
    print(f"  📌 Student URL  → {public_url}")
    print(f"  📌 Admin Panel  → {public_url}/admin")
    print(f"")
    print(f"  Share the Student URL with your students.")
    print(f"  Open Admin Panel on YOUR computer.")
    print("=" * 60)
    print("\nPress CTRL+C to stop.\n")

    # Now start the Flask server (blocks here)
    import eventlet
    import eventlet.wsgi
    from server import app, socketio

    socketio.run(app, host='0.0.0.0', port=5000, debug=False)


if __name__ == '__main__':
    main()
