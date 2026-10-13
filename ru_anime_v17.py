#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, TypeVar
from urllib.parse import urlparse
import urllib.request
import urllib.error

VERSION = "v17"

def history_path() -> str:
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state_home, "ru-anime", "history.json")


def legacy_history_path() -> str:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(config_home, "ru-anime", "history.json")

def load_history() -> dict:
    for path in (history_path(), legacy_history_path()):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("searches", [])
                if path != history_path():
                    save_history(data)
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"searches": [], "last_session": None}

def save_history(data: dict) -> None:
    path = history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError as e:
        print(f"[История] Не удалось сохранить историю: {e}", file=sys.stderr)

def remember_search(query: str) -> None:
    query = query.strip()
    if not query:
        return
    data = load_history()
    searches = [q for q in data.get("searches", []) if isinstance(q, str) and q.casefold() != query.casefold()]
    data["searches"] = ([query] + searches)[:20]
    save_history(data)

def remember_session(query: str, candidate: Candidate, episode: int, status: str = "current") -> None:
    if not query:
        return
    data = load_history()
    now = int(time.time())
    session = {
        "query": query,
        "title": candidate.title,
        "label": candidate.label,
        "source": candidate.source,
        "episode": episode,
        "status": status,
        "updated_at": now,
    }
    data["last_session"] = session
    items = data.setdefault("watch_history", [])
    if not isinstance(items, list):
        items = []
        data["watch_history"] = items
    key = (query.casefold(), str(candidate.label).casefold())
    existing = next((item for item in items if isinstance(item, dict) and
                      (str(item.get("query", "")).casefold(), str(item.get("label", "")).casefold()) == key), None)
    if existing is None:
        existing = {"query": query, "title": candidate.title, "label": candidate.label,
                    "source": candidate.source, "episode": episode, "watched_episodes": [],
                    "status": status, "updated_at": now}
        items.insert(0, existing)
    else:
        existing.update({"query": query, "title": candidate.title, "label": candidate.label,
                         "source": candidate.source, "episode": episode,
                         "status": status, "updated_at": now})
    data["watch_history"] = items[:50]
    searches = [q for q in data.get("searches", []) if isinstance(q, str) and q.casefold() != query.casefold()]
    data["searches"] = ([query] + searches)[:20]
    save_history(data)

def mark_episode_watched(query: str, candidate: Candidate, episode: int) -> None:
    if not query:
        return
    data = load_history()
    items = data.get("watch_history", [])
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict) and str(item.get("query", "")).casefold() == query.casefold() and str(item.get("label", "")).casefold() == candidate.label.casefold():
            watched = item.setdefault("watched_episodes", [])
            if episode not in watched:
                watched.append(episode)
                watched.sort()
            item["status"] = "paused"
            item["updated_at"] = int(time.time())
            break
    save_history(data)

def set_session_status(query: str, candidate: Candidate, episode: int, status: str) -> None:
    if not query:
        return
    data = load_history()
    session = data.get("last_session")
    if isinstance(session, dict) and str(session.get("query", "")).casefold() == query.casefold() and str(session.get("label", "")).casefold() == candidate.label.casefold():
        session["status"] = status
        session["episode"] = episode
        session["updated_at"] = int(time.time())
    items = data.get("watch_history", [])
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and str(item.get("query", "")).casefold() == query.casefold() and str(item.get("label", "")).casefold() == candidate.label.casefold():
                item["status"] = status
                item["episode"] = episode
                item["updated_at"] = int(time.time())
                break
    save_history(data)

def _managed_venv_path() -> str:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache_home, "ru-anime", "venv")


def ensure_python_dependencies() -> None:
    if sys.version_info < (3, 10):
        print("Для ru-anime требуется Python 3.10 или новее.", file=sys.stderr)
        raise SystemExit(1)

    venv_dir = os.path.realpath(_managed_venv_path())
    current_prefix = os.path.realpath(sys.prefix)
    if current_prefix != venv_dir:
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv_dir, "bin", "python")
        if not os.path.isfile(venv_python):
            os.makedirs(os.path.dirname(venv_dir), exist_ok=True)
            print("Первый запуск: создаю изолированное окружение Python…", flush=True)
            result = subprocess.run([sys.executable, "-m", "venv", venv_dir], check=False)
            if result.returncode != 0 or not os.path.isfile(venv_python):
                if shutil.which("apt-get"):
                    print("Похоже, в системе не установлена поддержка venv. Пытаюсь установить python3-venv…", flush=True)
                    prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ([shutil.which("sudo")] if shutil.which("sudo") else [])
                    install_result = subprocess.run(prefix + ["apt-get", "update"], check=False)
                    if install_result.returncode == 0:
                        install_result = subprocess.run(prefix + ["apt-get", "install", "-y", "python3-venv"], check=False)
                    if install_result.returncode == 0:
                        result = subprocess.run([sys.executable, "-m", "venv", venv_dir], check=False)
                if result.returncode != 0 or not os.path.isfile(venv_python):
                    print("Не удалось создать виртуальное окружение. Установите поддержку venv для Python и запустите скрипт снова.", file=sys.stderr)
                    raise SystemExit(1)
        os.execv(venv_python, [venv_python, os.path.abspath(__file__), *sys.argv[1:]])

    try:
        import importlib.util
        missing = []
        if importlib.util.find_spec("anime_parsers_ru") is None:
            missing.append("anime-parsers-ru")
        if importlib.util.find_spec("anicli_api") is None:
            missing.append("anicli-api")
        upgrade_anicli = False
        if not missing:
            try:
                upgrade_anicli = importlib.util.find_spec("anicli_api.source.anilib_me") is None
            except (ImportError, ModuleNotFoundError, ValueError):
                upgrade_anicli = True
    except (ImportError, ValueError):
        missing = ["anime-parsers-ru", "anicli-api"]
        upgrade_anicli = False

    if missing or upgrade_anicli:
        packages = list(missing)
        if upgrade_anicli and "anicli-api" not in packages:
            packages.extend(["--upgrade", "anicli-api"])
        print("Устанавливаю/обновляю зависимости в пользовательском окружении: " + ", ".join(packages), flush=True)
        result = subprocess.run([sys.executable, "-m", "pip", "install", *packages], check=False)
        if result.returncode != 0:
            print("Не удалось установить Python-зависимости. Проверьте подключение к интернету и повторите запуск.", file=sys.stderr)
            raise SystemExit(result.returncode or 1)
        if upgrade_anicli and not missing:
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])


def _privileged_command(command: list[str]) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return command
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo, *command]
    return command


def ensure_mpv() -> None:
    if shutil.which("mpv"):
        return

    print("mpv не найден. Пытаюсь установить его системным пакетным менеджером…", flush=True)
    if shutil.which("pacman"):
        command = _privileged_command(["pacman", "-S", "--needed", "mpv"])
    elif shutil.which("apt-get"):
        prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ([shutil.which("sudo")] if shutil.which("sudo") else [])
        command = prefix + ["apt-get", "update"]
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            print("Не удалось обновить список пакетов; продолжаю попытку установки mpv.", file=sys.stderr)
        command = prefix + ["apt-get", "install", "-y", "mpv"]
    elif shutil.which("dnf"):
        command = _privileged_command(["dnf", "install", "-y", "mpv"])
    elif shutil.which("zypper"):
        command = _privileged_command(["zypper", "--non-interactive", "install", "mpv"])
    elif shutil.which("xbps-install"):
        command = _privileged_command(["xbps-install", "-S", "mpv"])
    elif shutil.which("pkg") and ("termux" in os.environ.get("PREFIX", "").lower() or os.path.isdir("/data/data/com.termux")):
        command = [shutil.which("pkg"), "install", "-y", "mpv"]
    elif shutil.which("brew"):
        command = [shutil.which("brew"), "install", "mpv"]
    elif shutil.which("winget"):
        command = [shutil.which("winget"), "install", "--id", "shinchiro.mpv", "--exact", "--accept-package-agreements", "--accept-source-agreements"]
    else:
        print("Автоустановка mpv не поддерживается в этой системе. Установите mpv вручную; поиск по источникам всё равно доступен.", file=sys.stderr)
        return

    result = subprocess.run(command, check=False)
    if result.returncode != 0 or not shutil.which("mpv"):
        print("Не удалось автоматически установить mpv. Поиск останется доступен, но воспроизведение потребует ручной установки mpv.", file=sys.stderr)


ensure_python_dependencies()

try:
    from anime_parsers_ru import AnimegoParser
except ImportError:
    print(
        "Не найден пакет anime_parsers_ru.\n"
        "Установи: pip install anime-parsers-ru\n"
        "(если pip ругается на externally managed environment — см. README.md)",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from anime_parsers_ru import KodikSearch, KodikParser
    KODIK_AVAILABLE = True
    _KODIK_IMPORT_ERROR = ""
except ImportError as e:
    KodikSearch = None
    KodikParser = None
    KODIK_AVAILABLE = False
    _KODIK_IMPORT_ERROR = str(e)

try:
    import anicli_api
    import importlib as _importlib
    ANICLI_AVAILABLE = True
    _ANICLI_IMPORT_ERROR = ""
except ImportError as e:
    _importlib = None
    ANICLI_AVAILABLE = False
    _ANICLI_IMPORT_ERROR = str(e)

ANICLI_SOURCES = [
    ("yummy_anime", "YummyAnime"),
    ("anilib_me", "AnimeLib"),
]

try:
    from anime_parsers_ru import ShikimoriParser
    SHIKIMORI_AVAILABLE = True
    _SHIKIMORI_IMPORT_ERROR = ""
except ImportError as e:
    ShikimoriParser = None
    SHIKIMORI_AVAILABLE = False
    _SHIKIMORI_IMPORT_ERROR = str(e)

class Spinner:
    def __init__(self, message: str = "Ищу"):
        self.message = message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _spin(self) -> None:
        frames = ["   ", ".  ", ".. ", "..."]
        i = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r{self.message}{frames[i % len(frames)]}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.3)

        sys.stdout.write("\r" + " " * (len(self.message) + 4) + "\r")
        sys.stdout.flush()

    def __enter__(self) -> "Spinner":
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

@dataclass
class Voice:
    label: str
    translation_id: str | None
    player: str
    cvh_id: str | None

def parse_voice(data: dict) -> Voice:

    return Voice(
        label=data.get("label") or "?",
        translation_id=data.get("translation_id"),
        player=(data.get("player") or "").strip(),
        cvh_id=data.get("cvh_id"),
    )

@dataclass
class Candidate:

    source: str
    label: str
    title: str
    episodes_released: int | None = None
    episodes_total: int | None = None
    animego_anime_id: str | None = None
    animego_voice: Voice | None = None
    kodik_id: str | None = None
    kodik_id_type: str | None = None
    kodik_translation_id: str | None = None
    resolver: Callable[[int], "tuple[Optional[str], Optional[dict]]"] | None = None
    group_options: "list[Candidate] | None" = None
    dub_label: str | None = None
    quality_label: str | None = None

_QUALITY_P_RE = re.compile(r"(\d{3,4})p\b")

def extract_quality(text: str | None) -> int:

    if not text:
        return 0
    text = str(text)
    if text.isdigit():
        return int(text)
    m = _QUALITY_P_RE.search(text)
    return int(m.group(1)) if m else 0

_RU_NUMBER_WORDS = {
    "0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре", "5": "пять",
    "6": "шесть", "7": "семь", "8": "восемь", "9": "девять", "10": "десять",
    "11": "одиннадцать", "12": "двенадцать", "13": "тринадцать", "14": "четырнадцать",
    "15": "пятнадцать", "16": "шестнадцать", "17": "семнадцать", "18": "восемнадцать",
    "19": "девятнадцать", "20": "двадцать", "30": "тридцать", "40": "сорок",
    "50": "пятьдесят", "60": "шестьдесят", "70": "семьдесят", "80": "восемьдесят",
    "90": "девяносто", "100": "сто", "1000": "тысяча",
}
_RU_WORD_NUMBERS = {v: k for k, v in _RU_NUMBER_WORDS.items()}

def _alt_number_query(query: str) -> str | None:

    words = query.split()
    changed = False
    alt_words = []
    for w in words:
        lw = w.lower()
        if lw in _RU_NUMBER_WORDS:
            alt_words.append(_RU_NUMBER_WORDS[lw])
            changed = True
        elif lw in _RU_WORD_NUMBERS:
            alt_words.append(_RU_WORD_NUMBERS[lw])
            changed = True
        else:
            alt_words.append(w)
    return " ".join(alt_words) if changed else None

T = TypeVar("T")

def choose(items: Sequence[T], render: Callable[[T], str],
           prompt: str = "Выбор (номер, 0 — назад): ") -> T | None:
    for i, item in enumerate(items, start=1):
        print(f"  {i}) {render(item)}")
    print("  0) Назад")
    while True:
        raw = input(prompt).strip()
        if raw in ("", "0"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print("Некорректный номер, попробуй ещё раз.")

def choose_paged(items: Sequence[T], render: Callable[[T], str], start_index: int = 0) -> T | None:
    if not items:
        return None
    index = max(0, min(start_index, len(items) - 1))
    while True:
        print("\n" + "═" * 64)
        print(f"Результат {index + 1} из {len(items)}")
        print(render(items[index]))
        print("[n] следующий  [p] предыдущий  [s] выбрать  [q] назад")
        raw = input("> ").strip().lower()
        if raw in ("", "q", "0"):
            return None
        if raw == "n":
            if index + 1 < len(items):
                index += 1
            else:
                print("Это последний результат.")
        elif raw == "p":
            if index > 0:
                index -= 1
            else:
                print("Это первый результат.")
        elif raw in ("s", "enter", "выбрать"):
            return items[index]
        else:
            print("Команды: n — следующий, p — предыдущий, s — выбрать, q — назад.")

def nav_choice(valid_commands: str, help_text: str) -> str:

    while True:
        raw = input("> ").strip().lower()
        if raw in ("", "q"):
            return "q"
        if raw in valid_commands:
            return raw
        print(f"Неизвестная команда «{raw}». {help_text}")

def mpv_path() -> str | None:
    return shutil.which("mpv")

def play_in_mpv(
    url: str, title: str, prefer_max_hls_bitrate: bool = True, headers: dict | None = None,
) -> int | None:

    path = mpv_path()
    if not path:
        print(
            "Не найден mpv в PATH.\n"
            "Установи: sudo pacman -S mpv   (Arch/EndeavourOS)\n"
            "          sudo apt install mpv (Debian/Ubuntu)"
        )
        return None
    cmd = [path, url, f"--force-media-title={title}", "--save-position-on-quit"]
    if prefer_max_hls_bitrate and ".m3u8" in url.lower():
        cmd.append("--hls-bitrate=max")
    header_fields = []
    for key, value in (headers or {}).items():
        key = str(key).strip()
        value = str(value).replace("\\", "\\\\").replace(",", "\\,")
        if key and "\r" not in key and "\n" not in key and "\r" not in value and "\n" not in value:
            header_fields.append(f"{key}: {value}")
    if header_fields:
        cmd.append("--http-header-fields=" + ",".join(header_fields))
    host = urlparse(url).netloc or "видео"
    print(f"\n[mpv] запускаю воспроизведение ({host})...\n")
    result = subprocess.run(cmd, check=False)
    return result.returncode

def report_mpv_exit(code: int | None) -> None:
    if code is not None and code != 0:
        print(f"mpv завершился с кодом {code} — как правило, это означает проблему (сеть, битая ссылка).")

def is_dub_label(label: str) -> bool:
    low = (label or "").lower()
    return "субтитр" not in low and "sub" not in low

def resolve_aniboom(parser: AnimegoParser, voice: Voice, anime_id: str, episode: int) -> Optional[str]:
    stream = parser.aniboom_get_stream_for_voice(
        translation_id=voice.translation_id, episode=episode, anime_id=anime_id,
    )
    return stream.get("url") if stream else None

def resolve_cvh(parser: AnimegoParser, voice: Voice, anime_id: str, episode: int) -> Optional[str]:
    if not voice.cvh_id:
        return None
    stream = parser.cvh_get_stream(
        cvh_id=voice.cvh_id, season=1, episode=episode, translation=voice.label,
    )
    if not stream:
        return None
    if stream.get("HLS"):
        return stream["HLS"]
    if stream.get("MP4s"):
        return sorted(stream["MP4s"], key=extract_quality)[-1]
    if stream.get("DASH"):
        return stream["DASH"]
    return None

def resolve_animego_voice(parser: AnimegoParser, voice: Voice, anime_id: str, episode: int) -> Optional[str]:

    player_type = (voice.player or "").strip().lower()
    if player_type == "aniboom":
        return resolve_aniboom(parser, voice, anime_id, episode)
    if player_type == "cvh":
        return resolve_cvh(parser, voice, anime_id, episode)
    return None

def build_animego_candidates(parser: AnimegoParser, anime_id: str, title: str) -> tuple[list[Candidate], Optional[str]]:

    try:
        voices_data = parser.get_voices(anime_id=anime_id, episode=1)
    except Exception as e:
        return [], f"animego: ошибка получения озвучек: {type(e).__name__}: {e}"

    episodes = get_episode_list(parser, anime_id)
    released = [e for e in episodes if e.get("is_released")]
    episodes_released = len(released) if episodes else voices_data.get("total_episodes")
    episodes_total = len(episodes) if episodes else voices_data.get("total_episodes")

    candidates: list[Candidate] = []
    for v in voices_data.get("voices", []):
        try:
            voice = parse_voice(v)
        except Exception:
            continue
        player_type = (voice.player or "").strip().lower()
        if player_type not in ("aniboom", "cvh"):
            continue
        tag = "озвучка" if is_dub_label(voice.label) else "субтитры"
        candidates.append(Candidate(
            source="animego",
            label=f"[animego/{voice.player}] {title} — {voice.label} ({tag}; качество: авто)",
            title=title,
            animego_anime_id=anime_id,
            animego_voice=voice,
            episodes_released=episodes_released,
            episodes_total=episodes_total,
            dub_label=voice.label,
            quality_label="авто",
        ))
    return candidates, None

def get_episode_list(parser: AnimegoParser, anime_id: str) -> list:
    try:
        episodes = parser.get_episodes_info(anime_id=anime_id)
    except Exception as e:
        print(f"Не удалось получить список серий: {type(e).__name__}: {e}")
        return []
    return episodes or []

def _season_score_key(a: dict):
    try:
        return -float(a.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0

def _kodik_quality_tuple(link_data):
    if not link_data or not isinstance(link_data, (list, tuple)) or len(link_data) < 2:
        return None, None
    base_url, quality = link_data[0], link_data[1]
    if not base_url or not quality:
        return None, None
    return base_url, quality

def build_kodik_candidates(query: str, kodik_token: Optional[str], limit: int = 50) -> tuple[list[Candidate], Optional[str]]:

    if not KODIK_AVAILABLE:
        return [], None

    queries = [query]
    alt = _alt_number_query(query)
    if alt and alt != query:
        queries.append(alt)

    results = []
    errors = []
    for q in queries:
        try:
            data = KodikSearch(token=kodik_token).title(q).limit(limit).execute()
            results += list(getattr(data, "results", None) or [])
        except Exception as e:
            errors.append(f"Kodik: ошибка поиска ({q!r}): {type(e).__name__}: {e}")

    if not results and errors:
        return [], "; ".join(errors)

    groups: dict[str, list] = {}
    order: list[str] = []
    for el in results:
        title = getattr(el, "title", None) or getattr(el, "title_orig", None) or query
        if title not in groups:
            groups[title] = []
            order.append(title)
        groups[title].append(el)

    top_candidates: list[Candidate] = []

    for title in order:
        elements = groups[title]
        title_orig = getattr(elements[0], "title_orig", None)
        ep_total = getattr(elements[0], "episodes_count", None)
        ep_released = getattr(elements[0], "last_episode", None) or ep_total

        sub_candidates: list[Candidate] = []
        seen = set()
        for el in elements:
            translation = getattr(el, "translation", None)
            t_id = getattr(translation, "id", None) if translation is not None else None
            t_title = getattr(translation, "title", None) if translation is not None else None

            shiki_id = getattr(el, "shikimori_id", None)
            kodik_native_id = getattr(el, "id", None)
            lookup_id = shiki_id or kodik_native_id
            if not lookup_id:
                continue
            id_type = "shikimori" if shiki_id else "kodik"

            key = (t_id, lookup_id)
            if key in seen:
                continue
            seen.add(key)

            el_ep_total = getattr(el, "episodes_count", None)
            el_ep_released = getattr(el, "last_episode", None) or el_ep_total
            label = f"{t_title}" if t_title else f"озвучка {len(sub_candidates) + 1}"

            sub_candidates.append(Candidate(
                source="kodik", label=f"[Kodik] {title} — {label}", title=title,
                episodes_released=el_ep_released, episodes_total=el_ep_total,
                kodik_id=str(lookup_id), kodik_id_type=id_type,
                kodik_translation_id=str(t_id) if t_id else "0",
                dub_label=str(t_title or "Озвучка по умолчанию"),
                quality_label=str(getattr(el, "quality", "") or "авто"),
            ))
            sub_candidates[-1].label += f" (качество: {sub_candidates[-1].quality_label})"

        if not sub_candidates:
            continue

        base_label = f"[Kodik] {title}"
        if title_orig and title_orig != title:
            base_label += f" ({title_orig})"

        if len(sub_candidates) == 1:
            top_candidates.append(sub_candidates[0])
        else:
            top_candidates.append(Candidate(
                source="kodik", label=f"{base_label} — {len(sub_candidates)} озвучек",
                title=title, episodes_released=ep_released, episodes_total=ep_total,
                group_options=sub_candidates,
            ))

    return top_candidates, None

def build_kodik_candidates_by_external_id(
    id_value: str, id_type: str, kodik_token: Optional[str], title_hint: str,
) -> tuple[list[Candidate], Optional[str]]:

    if not KODIK_AVAILABLE:
        return [], None

    candidates: list[Candidate] = []
    search_err = None

    try:
        query_obj = KodikSearch(token=kodik_token)
        filter_method = getattr(query_obj, f"{id_type}_id", None)
        if filter_method is None:
            raise AttributeError(f"KodikSearch не поддерживает фильтр {id_type}_id() в этой версии библиотеки")
        query_obj = filter_method(id_value)
        data = query_obj.execute()

        results = getattr(data, "results", None) or []
        seen = set()
        for el in results:
            title = getattr(el, "title", None) or title_hint
            title_orig = getattr(el, "title_orig", None)
            translation = getattr(el, "translation", None)
            t_id = getattr(translation, "id", None) if translation is not None else None
            t_title = getattr(translation, "title", None) if translation is not None else None
            episodes_total = getattr(el, "episodes_count", None)
            episodes_released = getattr(el, "last_episode", None) or episodes_total

            if t_id in seen:
                continue
            seen.add(t_id)

            label = f"[Kodik] {title}"
            if title_orig and title_orig != title:
                label += f" ({title_orig})"
            if t_title:
                label += f" — {t_title}"

            candidates.append(Candidate(
                source="kodik", label=label, title=title,
                episodes_released=episodes_released, episodes_total=episodes_total,
                kodik_id=id_value, kodik_id_type=id_type,
                kodik_translation_id=str(t_id) if t_id else "0",
                dub_label=str(t_title or "Озвучка по умолчанию"),
                quality_label="авто",
            ))
    except Exception as e:
        search_err = f"Kodik: поиск по {id_type}_id не сработал ({type(e).__name__}: {e}), пробую озвучку по умолчанию"

    if not candidates:
        candidates.append(Candidate(
            source="kodik", label=f"[Kodik] {title_hint} — озвучка по умолчанию (список студий получить не удалось)",
            title=title_hint, kodik_id=id_value, kodik_id_type=id_type,
            kodik_translation_id="0",
        ))

    return candidates, search_err

_KINOPOISK_ID_RE = re.compile(r"kodikplayer\.com/find-player\?kinopoiskID=(\d+)")

def fetch_doorway_kinopoisk_id(url: str) -> tuple[Optional[str], Optional[str]]:

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"Не удалось загрузить страницу: {type(e).__name__}: {e}"

    m = _KINOPOISK_ID_RE.search(html)
    if not m:
        return None, "На странице не нашёлся kodikplayer.com с kinopoiskID — структура страницы могла отличаться от ожидаемой."
    return m.group(1), None

def doorway_flow(animego_parser: AnimegoParser, slug_or_url: str, kodik_token: Optional[str]) -> None:

    url = slug_or_url if slug_or_url.startswith(("http://", "https://")) else f"https://{slug_or_url}/"

    with Spinner("Загружаю страницу и ищу kinopoiskID"):
        kinopoisk_id, err = fetch_doorway_kinopoisk_id(url)

    if err:
        print(
            f"{err}\n"
            f"Это может быть та же антибот-защита, о которую я сам споткнулся при "
            f"проверке — или структура страницы отличается от той, что я видел. "
            f"Пришли текст ошибки (и/или свежий HTML этой страницы), поправлю прицельно."
        )
        return

    print(f"[домен-прокладка] Нашёл kinopoiskID: {kinopoisk_id}")

    candidates, kerr = build_kodik_candidates_by_external_id(kinopoisk_id, "kinopoisk", kodik_token, slug_or_url)
    if kerr:
        print(f"[!] {kerr}")

    watch_flow(animego_parser, candidates, kodik_token, query=slug_or_url)

def resolve_kodik_candidate(candidate: Candidate, episode: int, kodik_token: Optional[str]) -> Optional[str]:

    kparser = KodikParser(token=kodik_token, validate_token=False)
    link_data = kparser.get_link(
        id=candidate.kodik_id, id_type=candidate.kodik_id_type,
        seria_num=episode, translation_id=candidate.kodik_translation_id,
    )
    base_url, quality = _kodik_quality_tuple(link_data)
    if not base_url:
        return None
    return f"https:{base_url}{quality}.mp4"

def shikimori_full_info(query: str) -> list[dict]:

    if not SHIKIMORI_AVAILABLE:
        return []
    try:
        parser = ShikimoriParser()
        results = parser.search(query)
    except Exception as e:
        print(f"[Shikimori] поиск не удался: {type(e).__name__}: {e}")
        return []
    if not results:
        return []

    top_results = results[:1]

    def _fetch_one(item: dict) -> dict:
        link = item.get("link")
        if not link:
            return item
        try:

            info = ShikimoriParser().anime_info(link)
            return info or item
        except Exception:
            return item

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(top_results)))) as ex:
        return list(ex.map(_fetch_one, top_results))

def _first_value(data: dict, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None

def _shikimori_poster_url(info: dict) -> str | None:
    value = _first_value(info, "poster", "picture", "image", "image_url", "poster_url", "poster_image")
    if isinstance(value, dict):
        value = _first_value(value, "original", "originalUrl", "xoriginal", "preview", "mainUrl", "url", "src")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = "https://shikimori.one" + value
    return value if value.startswith(("http://", "https://")) else None

def _show_kitty_poster(url: str | None) -> None:
    if not url or not (os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TERM", "").startswith("xterm-kitty")):
        return
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ru-anime/12"})
        with urllib.request.urlopen(request, timeout=5) as response:
            content = response.read(8 * 1024 * 1024)
        suffix = ".jpg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            suffix = ".webp"
        with tempfile.NamedTemporaryFile(prefix="ru-anime-poster-", suffix=suffix, delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = subprocess.run(
                ["kitty", "+kitten", "icat", "--align=left", "--place=16x10@0x0", path],
                stdout=sys.stdout, stderr=subprocess.DEVNULL, check=False, timeout=8,
            )
            if result.returncode == 0:
                print()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except (OSError, urllib.error.URLError, subprocess.SubprocessError):
        pass

def print_shikimori_info(infos: list[dict]) -> None:
    if not infos:
        return

    print(f"\n{'═' * 64}\nИНФОРМАЦИЯ О ТАЙТЛЕ · SHIKIMORI\n{'═' * 64}")
    width = max(50, min(shutil.get_terminal_size((100, 24)).columns - 4, 100))
    status_names = {
        "released": "Завершён", "ongoing": "Выходит сейчас", "anons": "Ещё не вышел",
        "planned": "Анонсирован", "not_yet_released": "Ещё не вышел",
        "вышел": "Завершён", "онгоинг": "Выходит сейчас", "анонс": "Ещё не вышел",
    }
    kind_names = {"tv": "ТВ-сериал", "movie": "Фильм", "ova": "OVA", "ona": "ONA", "special": "Спецвыпуск", "tv_special": "ТВ-спецвыпуск"}

    for index, info in enumerate(infos, 1):
        title = _first_value(info, "russian", "title", "name") or "Без названия"
        original = _first_value(info, "original_title", "name", "title_orig", "english")
        kind = _first_value(info, "kind", "type")
        status = str(_first_value(info, "status") or "").lower()
        aired = _first_value(info, "dates", "aired_on", "released_on", "start_date", "year")
        ended = _first_value(info, "ended_on", "end_date")
        total = _first_value(info, "episodes", "episodes_total", "episodes_count")
        aired_count = _first_value(info, "episodes_aired", "episodes_released", "episodes_aired_count")
        duration = _first_value(info, "episode_duration", "duration")
        score = _first_value(info, "score", "rating")
        description = _first_value(info, "description", "synopsis", "description_html")
        poster = _shikimori_poster_url(info)

        print(f"\n[{index}/{len(infos)}] {title}")
        if original and str(original).casefold() != str(title).casefold():
            print(f"Оригинальное название: {original}")
        if kind:
            print(f"Формат: {kind_names.get(str(kind).lower(), str(kind).upper())}")
        if aired:
            print(f"Дата выхода: {aired}")
        if status:
            print(f"Статус: {status_names.get(status, status)}")
        if total is not None:
            total_text = str(total).strip()
            split_count = re.search(r"(\d+)\s*/\s*(\d+)", total_text)
            try:
                total_num = int(split_count.group(2)) if split_count else int(total_text)
            except (ValueError, TypeError):
                total_num = None
            try:
                aired_num = int(split_count.group(1)) if split_count else (int(aired_count) if aired_count is not None else None)
            except (ValueError, TypeError):
                aired_num = None
            if total_num is not None and (status in ("ongoing", "anons", "planned", "not_yet_released", "онгоинг", "анонс") or split_count):
                aired_num = max(0, min(aired_num or 0, total_num))
                print(f"Серии: вышло {aired_num} из {total_num}; осталось {max(0, total_num - aired_num)}")
            else:
                print(f"Количество серий: {total_num if total_num is not None else total}")
        elif aired_count is not None:
            print(f"Вышло серий: {aired_count}")
        if ended:
            print(f"Дата завершения: {ended}")
        if duration:
            print(f"Длительность серии: {duration} мин.")
        if score:
            print(f"Оценка: {score}")
        if description:
            clean_description = re.sub(r"<[^>]+>", " ", str(description))
            clean_description = re.sub(r"\s+", " ", clean_description).strip()
            if clean_description:
                print("Описание:")
                print(textwrap.fill(clean_description, width=width, initial_indent="  ", subsequent_indent="  "))
        if poster:
            _show_kitty_poster(poster)
        related = _first_value(info, "related", "related_titles") or []
        related_titles = []
        if isinstance(related, list):
            for item in related[:5]:
                if isinstance(item, dict):
                    rel_title = _first_value(item, "russian", "title", "name")
                    relation = _first_value(item, "relation", "type")
                    if rel_title:
                        related_titles.append(f"{rel_title} ({relation})" if relation else str(rel_title))
        if related_titles:
            print("Связанные тайтлы: " + "; ".join(related_titles))
    print(f"\n{'═' * 64}")

def make_anicli_resolver(episodes: list, source_title: str, target_quality: int | None = None) -> Callable[[int], "tuple[Optional[str], Optional[dict]]"]:

    def _resolve(episode_num: int):
        ep_obj = next((e for e in episodes if str(getattr(e, "ordinal", "")) == str(episode_num)), None)
        if ep_obj is None:
            return None, None
        try:
            sources = ep_obj.get_sources()
        except Exception:
            return None, None
        if not sources:
            return None, None
        match = next((s for s in sources if getattr(s, "title", None) == source_title), None)
        if match is None:
            return None, None
        try:
            videos = match.get_videos()
        except Exception:
            return None, None
        if not videos:
            return None, None
        video = None
        try:
            from anicli_api.tools.helpers import get_video_by_quality
            video = get_video_by_quality(videos, target_quality or 1080)
        except Exception:
            pass
        if video is None:
            video = videos[0]
        return video.url, (video.headers or None)
    return _resolve

def build_anicli_candidates(module_name: str, label: str, query: str) -> tuple[list[Candidate], Optional[str]]:

    if not ANICLI_AVAILABLE:
        return [], None

    queries = [query]
    alt = _alt_number_query(query)
    if alt and alt != query:
        queries.append(alt)

    results = []
    errors = []
    for q in queries:
        try:
            mod = _importlib.import_module(f"anicli_api.source.{module_name}")
            ex = mod.Extractor()
            found = ex.search(q)
            if found:
                results.append((q, found[0]))
        except Exception as e:
            errors.append(f"{label}: ошибка поиска ({q!r}): {type(e).__name__}: {e}")

    if not results:
        return [], ("; ".join(errors) if errors else None)

    q_used, top = results[0]

    try:
        anime = top.get_anime()
        episodes = anime.get_episodes()
    except Exception as e:
        return [], f"{label}: ошибка получения серий: {type(e).__name__}: {e}"

    if not episodes:
        return [], f"{label}: не нашлось ни одной серии"

    title = getattr(top, "title", None) or getattr(anime, "title", None) or q_used

    try:
        sources = episodes[0].get_sources()
    except Exception as e:
        return [], f"{label}: ошибка получения источников/озвучек: {type(e).__name__}: {e}"

    if not sources:
        return [], f"{label}: не нашлось ни одного варианта озвучки"

    episodes_count = len(episodes)

    candidates = []
    for i, src in enumerate(sources):
        src_title = getattr(src, "title", None) or f"вариант {i + 1}"
        qualities = []
        try:
            for video in src.get_videos():
                try:
                    quality = int(getattr(video, "quality", 0) or 0)
                except (TypeError, ValueError):
                    quality = 0
                if quality > 0 and quality not in qualities:
                    qualities.append(quality)
        except Exception:
            pass
        qualities.sort(reverse=True)
        if not qualities:
            qualities = [None]
        for quality in qualities:
            quality_text = f"{quality}p" if quality else "авто"
            candidates.append(Candidate(
                source=label, label=f"[{label}] {title} — {src_title} ({quality_text})",
                title=title, episodes_released=episodes_count, episodes_total=episodes_count,
                resolver=make_anicli_resolver(episodes, src_title, quality),
                dub_label=str(src_title), quality_label=quality_text,
            ))
    return candidates, None


def _candidate_leaves(candidates: Sequence[Candidate]) -> list[Candidate]:
    leaves = []
    for candidate in candidates:
        if candidate.group_options:
            leaves.extend(_candidate_leaves(candidate.group_options))
        else:
            leaves.append(candidate)
    return leaves

def _candidate_dub(candidate: Candidate) -> str:
    if candidate.dub_label:
        return candidate.dub_label.strip() or "Неизвестная озвучка"
    label = candidate.label
    if " — " in label:
        return label.rsplit(" — ", 1)[-1].strip().split(" (", 1)[0]
    return "Неизвестная озвучка"

def group_candidates_by_dub(candidates: Sequence[Candidate]) -> list[Candidate]:
    leaves = _candidate_leaves(candidates)
    groups: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for candidate in leaves:
        dub = _candidate_dub(candidate)
        key = re.sub(r"\s+", " ", dub).casefold()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(candidate)
    result = []
    for key in order:
        options = groups[key]
        dub = _candidate_dub(options[0])
        label = f"{dub} — {len(options)} вариантов" if len(options) != 1 else f"{dub} — {options[0].title}"
        result.append(Candidate(
            source="group", label=label, title=options[0].title,
            episodes_released=None, episodes_total=None,
            group_options=options, dub_label=dub,
        ))
    return sorted(result, key=lambda c: c.label.casefold())

def unified_search(animego_parser: AnimegoParser, query: str, kodik_token: Optional[str]) -> list[Candidate]:

    def _animego_job():
        queries = [query]
        alt = _alt_number_query(query)
        if alt and alt != query:
            queries.append(alt)

        seen_ids: set[str] = set()
        all_candidates: list[Candidate] = []
        errors: list[str] = []
        for q in queries:
            try:
                results = animego_parser.search(query=q)
            except Exception as e:
                errors.append(f"animego: ошибка поиска ({q!r}): {type(e).__name__}: {e}")
                continue
            if not results:
                continue

            top = results[0]
            anime_id = str(top.get("id", ""))
            if not anime_id or anime_id in seen_ids:
                continue
            seen_ids.add(anime_id)
            title = top.get("title") or top.get("original_title") or q
            cands, err = build_animego_candidates(animego_parser, anime_id, title)
            all_candidates += cands
            if err:
                errors.append(err)
        return all_candidates, ("; ".join(errors) if errors else None)

    def _kodik_job():
        return build_kodik_candidates(query, kodik_token)

    def _shikimori_job():
        return shikimori_full_info(query)

    jobs = {"animego": _animego_job, "kodik": _kodik_job, "shikimori": _shikimori_job}
    for module_name, label in ANICLI_SOURCES:
        jobs[label] = (lambda m=module_name, l=label: build_anicli_candidates(m, l, query))

    with Spinner(f"Ищу по {len(jobs) - 1} источникам + Shikimori"):
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futures = {name: ex.submit(job) for name, job in jobs.items()}
            results = {name: fut.result() for name, fut in futures.items()}

    shiki_info = results.pop("shikimori")
    print_shikimori_info(shiki_info)
    print(f"  Shikimori: {'найдены сведения — ' + str(len(shiki_info)) + ' записей' if shiki_info else 'сведения не найдены или сервис недоступен'}")

    all_candidates: list[Candidate] = []
    print("\n--- Результаты поиска по источникам ---")
    for name, (cands, err) in results.items():
        all_candidates += cands
        if cands and err:
            status = f"частично: найдено вариантов — {len(cands)}, есть ошибка"
        elif cands:
            status = f"найдено вариантов — {len(cands)}"
        elif err:
            status = "ошибка источника"
        else:
            status = "ничего не найдено"
        print(f"  {name}: {status}")
        if err:
            print(f"    Подробности: {err}")
    print(f"  Итого: {len(all_candidates)} вариантов просмотра")
    print("--------------------------------------")

    return group_candidates_by_dub(all_candidates)

def _episode_session(
    resolve_fn: Callable[[int], Optional[str]], title: str,
    episodes_released: int | None = None, episodes_total: int | None = None,
    initial_episode: int | None = None,
    on_episode: Callable[[int], None] | None = None,
    on_watched: Callable[[int], None] | None = None,
) -> str:

    if episodes_released and episodes_total and episodes_released != episodes_total:
        hint = f" (доступно {episodes_released} из {episodes_total} серий, Enter = 1)"
    elif episodes_released:
        hint = f" (всего {episodes_released} серий, Enter = 1)"
    else:
        hint = " (Enter = 1)"
    if episodes_released is not None and episodes_released <= 0:
        print("У источника пока нет доступных серий.")
        return "quit"
    if initial_episode is not None and initial_episode > 0 and (episodes_released is None or initial_episode <= episodes_released):
        episode = initial_episode
        print(f"\nПродолжаю с {episode}-й серии.")
    else:
        while True:
            raw = input(f"\nС какой серии начать?{hint}: ").strip()
            episode = int(raw) if raw.isdigit() and int(raw) > 0 else 1
            if episodes_released is not None and episode > episodes_released:
                print(f"Серия {episode} ещё не вышла. Сейчас доступно серий: {episodes_released}. Выбери доступную серию.")
                continue
            break

    while True:
        if on_episode:
            on_episode(episode)
        print(f"\n=== {title} — серия {episode} ===")
        url = None
        headers = None
        try:
            with Spinner(f"Получаю ссылку на серию {episode}"):
                url, headers = resolve_fn(episode)
        except Exception as e:
            print(
                f"Библиотека упала при получении ссылки: {type(e).__name__}: {e}\n"
                f"Похоже на баг в самой библиотеке/несовпадение с ответом сайта — "
                f"попробуй другой источник/озвучку ([r])."
            )

        if not url:
            print("Не удалось получить ссылку на видео для этой серии/варианта.")
        else:
            code = play_in_mpv(url, f"{title} — {episode} серия", headers=headers)
            report_mpv_exit(code)

        while True:
            print("\n[n] следующая  [p] предыдущая  [g] перейти к серии №...  [r] другой источник/озвучка  [q] выход в поиск")
            cmd = nav_choice("npgr", "Доступно: n, p, g, r, q.")
            if cmd == "n":
                if on_watched:
                    on_watched(episode)
                if episodes_released is not None and episode >= episodes_released:
                    print(f"Следующая серия ещё не вышла. Сейчас доступно серий: {episodes_released}.")
                    continue
                episode += 1
                break
            elif cmd == "g":
                raw = input("Номер серии: ").strip()
                if raw.isdigit() and int(raw) > 0:
                    requested_episode = int(raw)
                    if episodes_released is not None and requested_episode > episodes_released:
                        print(f"Серия {requested_episode} ещё не вышла. Сейчас доступно серий: {episodes_released}.")
                        continue
                    episode = requested_episode
                    break
                print("Некорректный номер серии.")
                continue
            elif cmd == "p":
                if episode <= 1:
                    print("Это первая серия.")
                    continue
                episode -= 1
                break
            elif cmd == "r":
                return "reselect"
            else:
                return "quit"

def watch_flow(
    animego_parser: AnimegoParser, candidates: list[Candidate], kodik_token: Optional[str],
    query: str = "", resume_label: str | None = None, resume_episode: int | None = None,
) -> None:
    if not candidates:
        print(
            "Ничего не найдено ни в одном из источников (animego.me, Kodik, "
            "YummyAnime, AnimeLib). Если это адрес сайта конкретного тайтла "
            "(например \"название-jutsu.ru\") — можно ввести его прямо в "
            "строку поиска, скрипт сам распознает такие ссылки."
        )
        return

    resume_used = False
    while True:
        picked = None
        if resume_label and not resume_used:
            def find_saved(items: list[Candidate], label: str) -> Candidate | None:
                for item in items:
                    if item.label == label:
                        return item
                    if item.group_options:
                        nested = find_saved(item.group_options, label)
                        if nested is not None:
                            return nested
                return None

            picked = find_saved(candidates, resume_label)
            if picked is not None:
                print(f"\nНайден сохранённый вариант: {picked.label}")
                resume_used = True
            else:
                print("Сохранённый вариант больше не найден. Выбери источник вручную.")
                resume_used = True
        if picked is None:
            picked = choose(candidates, lambda c: c.label)
        if picked is None:
            return

        cancelled_group = False
        while picked.group_options:
            sub = choose(picked.group_options, lambda c: c.label)
            if sub is None:
                cancelled_group = True
                break
            picked = sub
        if cancelled_group:
            continue

        if picked.resolver is not None:
            resolve_fn = picked.resolver
        elif picked.source == "animego":
            resolve_fn = lambda ep, p=picked: (resolve_animego_voice(animego_parser, p.animego_voice, p.animego_anime_id, ep), None)
        else:
            resolve_fn = lambda ep, p=picked: (resolve_kodik_candidate(p, ep, kodik_token), None)

        start_episode = resume_episode if resume_label == picked.label else None
        current_episode = [start_episode or 1]
        def save_current_episode(ep: int, q=query, c=picked) -> None:
            current_episode[0] = ep
            remember_session(q, c, ep, "current")
        signal = _episode_session(
            resolve_fn, picked.title, picked.episodes_released, picked.episodes_total,
            initial_episode=start_episode,
            on_episode=save_current_episode,
            on_watched=lambda ep, q=query, c=picked: mark_episode_watched(q, c, ep),
        )
        if query:
            set_session_status(query, picked, current_episode[0], "paused")
        resume_episode = None
        if signal == "quit":
            return

def search_flow(animego_parser: AnimegoParser, query: str, kodik_token: Optional[str]) -> None:
    remember_search(query)
    candidates = unified_search(animego_parser, query, kodik_token)
    watch_flow(animego_parser, candidates, kodik_token, query=query)

def _short_status(item: dict) -> str:
    watched = item.get("watched_episodes", [])
    watched_count = len(watched) if isinstance(watched, list) else 0
    episode = item.get("episode", 1)
    if item.get("status") == "current":
        state = "СЕЙЧАС"
    elif watched_count:
        state = "СМОТРЕЛ"
    else:
        state = "НЕ ЗАВЕРШЕНО"
    return f"{state} · серия {episode} · просмотрено серий: {watched_count}"

def history_flow(animego_parser: AnimegoParser, kodik_token: Optional[str]) -> None:
    data = load_history()
    items = [item for item in data.get("watch_history", []) if isinstance(item, dict) and item.get("query")]
    if not items:
        searches = [q for q in data.get("searches", []) if isinstance(q, str) and q.strip()]
        if not searches:
            print("\nИстория пока пуста.")
            return
        print("\nИстория поиска")
        picked = choose(searches, lambda q: q, "Выбор запроса (номер, 0 — назад): ")
        if picked:
            search_flow(animego_parser, picked, kodik_token)
        return
    print("\n" + "═" * 68)
    print("  ИСТОРИЯ ПРОСМОТРА")
    print("═" * 68)
    picked = choose(items, lambda item: f"{item.get('title') or item.get('query', '?')}  |  {_short_status(item)}",
                    "Выбери тайтл (номер, 0 — назад): ")
    if not picked:
        return
    query = str(picked.get("query") or "")
    episode = picked.get("episode", 1)
    try:
        episode = int(episode)
    except (TypeError, ValueError):
        episode = 1
    candidates = unified_search(animego_parser, query, kodik_token)
    watch_flow(animego_parser, candidates, kodik_token, query=query,
               resume_label=str(picked.get("label") or "") or None, resume_episode=episode)

def home_screen() -> str:
    data = load_history()
    session = data.get("last_session")
    items = [item for item in data.get("watch_history", []) if isinstance(item, dict) and item.get("query")]
    watched = [item for item in items if item.get("watched_episodes")]
    def row(value: str) -> None:
        print("║" + value[:66].ljust(66) + "║")
    print("\n" + "╔" + "═" * 66 + "╗")
    row(" RU-ANIME")
    print("╠" + "═" * 66 + "╣")
    if isinstance(session, dict) and session.get("query"):
        title = str(session.get("title") or session.get("query"))
        episode = session.get("episode", 1)
        row(f"Продолжить: {title} · серия {episode}")
    else:
        row("Продолжить: пока нечего")
    row(f"Просмотренных тайтлов: {len(watched)}")
    recent = items[:3]
    if recent:
        print("╠" + "═" * 66 + "╣")
        row("Последнее просмотренное")
        for item in recent:
            title = str(item.get("title") or item.get("query") or "Без названия")
            row(f"{title} · {_short_status(item)}")
    print("╠" + "═" * 66 + "╣")
    row("Enter  Поиск аниме")
    row("S      Предложения текущего сезона")
    row("C      Продолжить просмотр")
    row("H      История и просмотренное")
    row("Q      Выход")
    print("╚" + "═" * 66 + "╝")
    try:
        if not sys.stdin.isatty():
            return input("Выбор: ").strip().lower() or "search"
        if os.name == "nt":
            import msvcrt
            while True:
                key = msvcrt.getwch().lower()
                if key in ("\r", "\n"):
                    return "search"
                if key in ("s", "c", "h", "q", "\x03"):
                    print(key.upper() if key != "\x03" else "^C")
                    return "q" if key == "\x03" else key
        else:
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    key = sys.stdin.read(1).lower()
                    if key in ("\r", "\n"):
                        print("Enter")
                        return "search"
                    if key in ("s", "c", "h", "q", "\x03"):
                        print(key.upper() if key != "\x03" else "^C")
                        return "q" if key == "\x03" else key
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (EOFError, KeyboardInterrupt):
        return "q"

def resume_flow(animego_parser: AnimegoParser, kodik_token: Optional[str]) -> None:
    session = load_history().get("last_session")
    if not isinstance(session, dict) or not session.get("query"):
        print("Нет сохранённого просмотра. Сначала найди тайтл и запусти серию.")
        return
    query = str(session["query"])
    label = str(session.get("label") or "")
    episode = session.get("episode")
    try:
        episode = int(episode)
    except (TypeError, ValueError):
        episode = None
    print(f"Ищу сохранённый просмотр: {session.get('title') or query}, серия {episode or 1}")
    candidates = unified_search(animego_parser, query, kodik_token)
    watch_flow(
        animego_parser, candidates, kodik_token, query=query,
        resume_label=label or None, resume_episode=episode,
    )

def season_titles_flow(animego_parser: AnimegoParser, kodik_token: Optional[str]) -> None:
    try:
        titles = animego_parser.get_anime_from_current_season()
    except Exception as e:
        print(f"Не удалось получить список тайтлов сезона: {type(e).__name__}: {e}")
        return
    if not titles:
        print("Список тайтлов текущего сезона пуст или не удалось его получить.")
        return

    titles = sorted(titles, key=_season_score_key)
    print(f"\nТайтлов в текущем сезоне: {len(titles)} (отсортировано по оценке)")
    picked = choose(
        titles,
        lambda a: f"{a.get('title', '?')}"
        + (f" ({a['other_title']})" if a.get("other_title") else "")
        + (f" — {a['score']}★" if a.get("score") else ""),
    )
    if picked is None:
        return

    link = picked.get("link")
    if not link:
        print("У этого тайтла нет ссылки на animego.me — не могу его открыть.")
        return
    try:
        anime_id = AnimegoParser.get_id_from_link(link=link)
    except Exception as e:
        print(f"Не удалось определить ID тайтла по ссылке: {type(e).__name__}: {e}")
        return
    if not anime_id:
        print("Не удалось определить ID тайтла по ссылке.")
        return

    title = picked.get("title") or "Без названия"

    def _animego_job():
        return build_animego_candidates(animego_parser, anime_id, title)

    jobs = {"animego": _animego_job, "kodik": lambda: build_kodik_candidates(title, kodik_token),
            "shikimori": lambda: shikimori_full_info(title)}
    for module_name, label in ANICLI_SOURCES:
        jobs[label] = (lambda m=module_name, l=label: build_anicli_candidates(m, l, title))

    with Spinner(f"Собираю варианты озвучки ({len(jobs) - 1} источников + Shikimori)"):
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futures = {name: ex.submit(job) for name, job in jobs.items()}
            results = {name: fut.result() for name, fut in futures.items()}

    print_shikimori_info(results.pop("shikimori"))

    all_candidates: list[Candidate] = []
    for name, (cands, err) in results.items():
        all_candidates += cands
        if err:
            print(f"[!] {err}")

    remember_search(title)
    watch_flow(animego_parser, group_candidates_by_dub(all_candidates), kodik_token, query=title)

def _is_doorway_style(text: str) -> bool:

    if text.startswith(("http://", "https://")):
        return True
    return "." in text and "/" not in text and " " not in text

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Поиск и просмотр аниме с русской озвучкой (animego.me + Kodik + YummyAnime + AnimeLib через mpv)."
    )
    ap.add_argument("--mirror", default=None, help="Домен-зеркало animego.me.")
    ap.add_argument("--proxy", default=None, help="HTTP/SOCKS5-прокси для animego.me.")
    ap.add_argument("--kodik-token", default=None, help="Свой API-токен Kodik.")
    return ap

def main() -> None:
    args = build_arg_parser().parse_args()
    ensure_mpv()
    animego_parser = AnimegoParser(mirror=args.mirror, proxy=args.proxy, use_cache=True)
    print(f"ru_anime.py {VERSION} · поиск и просмотр аниме")
    try:
        while True:
            action = home_screen()
            if action == "q":
                break
            if action == "s":
                season_titles_flow(animego_parser, args.kodik_token)
                continue
            if action == "c":
                resume_flow(animego_parser, args.kodik_token)
                continue
            if action == "h":
                history_flow(animego_parser, args.kodik_token)
                continue
            query = input("\nНазвание аниме (Enter — назад): ").strip()
            if not query:
                continue
            if _is_doorway_style(query):
                doorway_flow(animego_parser, query, args.kodik_token)
            else:
                search_flow(animego_parser, query, args.kodik_token)
    except KeyboardInterrupt:
        print("\nВыход.")

if __name__ == "__main__":
    main()
