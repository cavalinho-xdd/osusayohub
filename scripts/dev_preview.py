"""Live-reload preview harness for overlay themes (dev tool, not packaged).

Edits to the watched theme JSON repaint the running overlay window(s)
immediately -- no process restart. Exists because the only other ways to
see the overlay render are the full app (tray + tosu supervisor, too heavy
to iterate against) or scripts/render_screenshots.py (one-shot offscreen
PNG, no live reload).

Usage:
    python scripts/dev_preview.py                # widget renderer
    python scripts/dev_preview.py --qml          # QML/layer-shell renderer
    python scripts/dev_preview.py --both         # both, side by side
    python scripts/dev_preview.py --theme mine.json

Edit the theme JSON (default scripts/dev-theme.json, scaffolded from the
built-in default theme on first run) and save -- the window(s) update
within about a second. The theme isn't tied to any skin name; it previews
independently of skin-name matching, which is exactly what authoring a new
theme needs before it's wired to a skin.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtGui import QColor

from ppeek.overlay.preview import DEMO_FRAME
from ppeek.overlay.theme import DEFAULT_THEME, OverlayTheme

DEFAULT_THEME_PATH = Path(__file__).resolve().parent / "dev-theme.json"
QML_PATH = Path(__file__).resolve().parent.parent / "ppeek" / "overlay" / "Overlay.qml"

_COLOR_FIELDS = ("ink", "body_fill", "miss", "bg_top", "bg_bottom", "accent", "deco_red")


def _color_to_json(c: QColor) -> list[int]:
    return [c.red(), c.green(), c.blue(), c.alpha()]


def _color_from_json(v: list[int]) -> QColor:
    r, g, b, *rest = v
    return QColor(r, g, b, rest[0] if rest else 255)


def theme_to_dict(theme: OverlayTheme) -> dict:
    d = {"name": theme.name, "scene": theme.scene, "font": theme.font}
    d.update((field, _color_to_json(getattr(theme, field))) for field in _COLOR_FIELDS)
    return d


def theme_from_dict(d: dict) -> OverlayTheme:
    kwargs = {
        "name": d.get("name", "dev"),
        "scene": d.get("scene", "night"),
        "font": d.get("font", "Sans"),
    }
    kwargs.update((field, _color_from_json(d[field])) for field in _COLOR_FIELDS)
    return OverlayTheme(**kwargs)


def load_theme(path: Path) -> OverlayTheme:
    return theme_from_dict(json.loads(path.read_text()))


def ensure_scaffold(path: Path) -> None:
    if path.exists():
        return
    path.write_text(json.dumps(theme_to_dict(DEFAULT_THEME), indent=2) + "\n")
    print(f"wrote scaffold theme to {path} -- edit and save to see live updates")


def _stage_demo(on_frame) -> None:
    # skin left blank so on_telemetry_frame's own skin-based theme
    # resolution never overwrites the dev theme we set right after
    on_frame(dataclasses.replace(DEMO_FRAME, skin=""))


def run_widget(theme_path: Path, x: int = 80, y: int = 80):
    from ppeek.overlay.window import OverlayWindow

    win = OverlayWindow(auto_hide=False)
    _stage_demo(win.on_telemetry_frame)
    win._theme = load_theme(theme_path)
    win.move(x, y)
    win.show()

    def reload() -> None:
        try:
            win._theme = load_theme(theme_path)
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            print(f"widget: theme reload failed: {exc}")
            return
        win.update()
        print("widget: theme reloaded")

    return win, reload


def run_qml(theme_path: Path, x: int = 480, y: int = 80):
    from PyQt6.QtQml import QQmlApplicationEngine

    from ppeek.overlay.bridge import HubState

    hub = HubState()
    hub.apply_layout(anchor_name="bottom-right", margin_x=24, margin_y=24, auto_hide=False)
    _stage_demo(hub.on_telemetry_frame)
    hub._theme = load_theme(theme_path)
    hub.themeChanged.emit()

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("hub", hub)
    engine.load(str(QML_PATH))
    if not engine.rootObjects():
        print("qml: Overlay.qml failed to load (layer-shell-qt plugin missing?) -- skipping --qml")
        return None, None

    root = engine.rootObjects()[0]
    # best-effort: under a real wlr-layer-shell compositor the window is
    # positioned by anchor/margin, not x/y, so this only takes effect when
    # the layer-shell plugin is absent and QML falls back to a plain window
    root.setProperty("x", x)
    root.setProperty("y", y)

    def reload() -> None:
        try:
            hub._theme = load_theme(theme_path)
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            print(f"qml: theme reload failed: {exc}")
            return
        hub.themeChanged.emit()
        print("qml: theme reloaded")

    return (engine, hub), reload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--theme", type=Path, default=DEFAULT_THEME_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--widget", action="store_true", help="QPainter widget renderer only (default)")
    mode.add_argument("--qml", action="store_true", help="QML/layer-shell renderer only")
    mode.add_argument("--both", action="store_true", help="both renderers, side by side")
    args = parser.parse_args()

    ensure_scaffold(args.theme)

    from PyQt6.QtCore import QFileSystemWatcher
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    want_widget = args.both or not args.qml
    want_qml = args.both or args.qml

    kept_alive = []
    reloaders = []

    if want_widget:
        win, reload_widget = run_widget(args.theme)
        kept_alive.append(win)
        reloaders.append(reload_widget)

    if want_qml:
        qml_objs, reload_qml = run_qml(args.theme)
        if qml_objs is not None:
            kept_alive.append(qml_objs)
            reloaders.append(reload_qml)

    if not reloaders:
        print("nothing to preview (QML failed to load and --widget wasn't requested)")
        sys.exit(1)

    watcher = QFileSystemWatcher([str(args.theme)])

    def on_changed(_path: str) -> None:
        for reload_fn in reloaders:
            reload_fn()
        # editors often replace-on-save rather than write-in-place;
        # re-arm the watch if that dropped it
        if str(args.theme) not in watcher.files() and args.theme.exists():
            watcher.addPath(str(args.theme))

    watcher.fileChanged.connect(on_changed)

    print(f"watching {args.theme} -- edit and save to update the preview live")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
