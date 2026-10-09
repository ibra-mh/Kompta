"""
Double-click-friendly launcher for the Kompta tax engine backend.

Run this with:   python run.py
It starts the server at http://127.0.0.1:8000 and keeps running until
you close the window / press Ctrl+C. Leave it open while you use
index.html in your browser.
"""
import os

import uvicorn

if __name__ == "__main__":
    os.environ.setdefault("KOMPTA_DEV_CORS_ORIGINS", "http://127.0.0.1:5500")
    print("=" * 60)
    print("Kompta tax engine starting at http://127.0.0.1:8000")
    print("Development CORS origin: " + os.environ["KOMPTA_DEV_CORS_ORIGINS"])
    print("Leave this window open while you use index.html.")
    print("Press Ctrl+C to stop the server.")
    print("=" * 60)
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
