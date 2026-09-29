"""Frozen-app entry point (PyInstaller target). Launches the Rubric tray; if Pro is not unlocked it
opens the in-app activation window. No terminal and no Python install needed once frozen."""
import licence
import tray

licence.refresh()          # best-effort online re-verify; the tray opens either way
raise SystemExit(tray.run() or 0)
