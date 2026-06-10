"""Vercel ASGI entrypoint for the Streamlit UI."""

from __future__ import annotations

from pathlib import Path

from streamlit.starlette import App

app = App(Path(__file__).resolve().parent / "app" / "streamlit_app.py")
