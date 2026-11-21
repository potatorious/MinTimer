"""Read the old timer's preferences on first use and save the Python copy separately."""

from dataclasses import dataclass
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import xml.etree.ElementTree as xml

LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
DATA = LOCAL / "MiniTimerPython"
ORIGINAL = LOCAL / "MiniTimer"
CHANNELS = ("Seconds", "Minutes", "Hours")


@dataclass
class Channel:
    enabled: bool = False
    volume: int = 50
    path: str = ""


def existing(name):
    path = DATA / name
    return path if path.exists() else ORIGINAL / name


def load_window(default):
    try:
        x, y, zoom, pinned = existing("settings.txt").read_text(encoding="utf-8-sig").splitlines()
        return int(x), int(y), max(1.0, min(2.0, float(zoom))), pinned.lower() == "true"
    except (OSError, ValueError):
        return *default, 1.0, True


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        temporary = Path(pending.name)
        try:
            pending.write(content)
        except BaseException:
            pending.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_window(x, y, zoom, pinned):
    atomic_write(DATA / "settings.txt", f"{x}\n{y}\n{zoom:.2f}\n{pinned}\n".encode("utf-8"))


def load_sounds():
    try:
        document = xml.parse(existing("sounds.xml")).getroot()
        result = []
        for name in CHANNELS:
            node = document.find(name)
            if node is None:
                raise ValueError("Invalid sound settings")
            result.append(Channel(
                node.findtext("Enabled", "false").lower() == "true",
                max(0, min(100, int(node.findtext("Volume", "50")))),
                node.findtext("FilePath", ""),
            ))
        return result
    except (OSError, ValueError, xml.ParseError):
        return [Channel() for _ in CHANNELS]


def save_sounds(channels):
    document = xml.Element("SoundSettings")
    for name, channel in zip(CHANNELS, channels):
        node = xml.SubElement(document, name)
        xml.SubElement(node, "Enabled").text = str(channel.enabled).lower()
        xml.SubElement(node, "Volume").text = str(channel.volume)
        xml.SubElement(node, "FilePath").text = channel.path
    atomic_write(DATA / "sounds.xml", xml.tostring(document, encoding="utf-8", xml_declaration=True))
