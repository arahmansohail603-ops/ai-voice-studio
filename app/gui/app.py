from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.core.async_runner import AsyncRunner
from app.core.errors import module_installed, soft_import
from app.core.player import AudioPlayer
from app.core.qwen_cloner import QwenVoiceCloner
from app.core.recorder import Recorder, default_input_device, device_info
from app.core.stt_engine import STTEngine
from app.core.translator import Translator
from app.core.tts_engine import TTSEngine
from app.core.voice_cloner import VoiceCloner
from app.gui import theme
from app.gui.screens import (
    HistoryScreen,
    HomeScreen,
    ModelsScreen,
    MyVoiceScreen,
    SettingsScreen,
    SpeechToTextScreen,
    TextToSpeechScreen,
    VoiceRecorderScreen,
    VoicesScreen,
)
from app.gui.widgets import Screen, Toast
from app.services.file_service import ensure_dirs
from app.services.history_service import HistoryService
from app.services.settings_service import SettingsService

WINDOW_SIZE = "1240x760"
MIN_SIZE = (1020, 640)
MAX_WIDTH = 1380
MAX_HEIGHT = 780

SCREENS = [
    ("home", "Home"),
    ("text_to_speech", "Text to Speech"),
    ("voice_recorder", "Voice Recorder"),
    ("speech_to_text", "Speech to Text"),
    ("my_voice", "My Voice"),
    ("models", "Models"),
    ("voices", "Voices"),
    ("history", "History"),
    ("settings", "Settings"),
]

_SUBTITLES = {
    "home": "Everything in one place.",
    "text_to_speech": "Convert text into natural speech.",
    "voice_recorder": "Capture your own voice.",
    "speech_to_text": "Turn spoken words into text.",
    "my_voice": "Generate speech that sounds like your voice.",
    "voices": "Download the offline voices you can speak with.",
    "history": "All your generated and recorded items.",
    "settings": "Defaults, devices and storage.",
}


class VoiceStudioApp(QMainWindow):
    #: Guards the one-time startup offer. A class-level default so reading it
    #: never depends on __init__ having run.
    _prefetch_offer_started = False
    #: Set when a saved input device had to be replaced at startup, so the
    #: window can tell the user instead of silently changing a setting.
    mic_recovery = None

    def _usable_mic_device(self):
        """The saved microphone, or the default one if the saved one is a speaker.

        Windows lists output endpoints such as "PC Speaker" and "Stereo Mix"
        among the inputs, and an older build let one of them be saved. Opening
        such a device fails with MMSYSERR_INVALPARAM (9996) with no useful
        message, which reads as "the microphone is broken". Recovering here means
        a machine stuck in that state records again on the next launch, without
        the user having to know that such a setting exists.
        """
        saved = self.settings.get("recorder", "device")
        if saved is None:
            return None
        info = device_info(saved)
        if info is None or not info.get("is_output"):
            return saved
        fallback = default_input_device()
        if fallback is None:
            return saved
        replacement = device_info(fallback) or {}
        self.settings.set("recorder", "device", fallback)
        self.mic_recovery = (
            f"The saved input device '{info['name']}' is a speaker, not a "
            "microphone, so recording would have failed every time. Switched to "
            f"'{replacement.get('name', 'the default microphone')}'. You can "
            "change this under Settings > Microphone."
        )
        return fallback

    def __init__(self) -> None:
        self._application = QApplication.instance() or QApplication(sys.argv[:1])
        super().__init__()
        theme.apply_theme(self._application)
        ensure_dirs()

        self.settings = SettingsService().load()
        if self.settings.get("tts", "backend", "system") == "edge":
            self.settings.set("tts", "backend", "qwen3")
        self.history = HistoryService().load()
        self.runner = AsyncRunner()

        from app.services import file_service

        saved_output = self.settings.get("output", "folder", "")
        if (
            saved_output
            and Path(saved_output) != file_service.category_dir("tts").parent
        ):
            try:
                file_service.set_output_root(saved_output)
            except Exception:
                pass

        self.recorder = Recorder(
            samplerate=int(self.settings.get("recorder", "samplerate", 44100)),
            channels=int(self.settings.get("recorder", "channels", 1)),
            device=self._usable_mic_device(),
        )
        self.cloner = VoiceCloner()
        self.qwen_cloner = QwenVoiceCloner()
        self.tts = TTSEngine(
            self.runner,
            file_service.category_dir("tts"),
            backend=self.settings.get("tts", "backend", "system"),
            qwen=self.qwen_cloner,
            settings=self.settings,
        )
        self.stt = STTEngine(
            language=self.settings.get("stt", "language", "en-US"),
            engine=self.settings.get("stt", "engine", "vosk"),
        )
        self.translator = Translator()
        try:
            self.player = AudioPlayer()
        except Exception:
            self.player = None

        self.setWindowTitle(f"{config.APP_NAME}  ·  v{config.APP_VERSION}")
        self._auto_fit_window()
        self.setMinimumSize(*MIN_SIZE)

        icon = config.BASE_DIR / "assets" / "app.ico"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        self._build_shell()
        self._current: str | None = None
        self._closing = False
        self._screens: dict[str, Screen] = {}
        self._build_screens()
        self.show_screen("home")

        self.toast_widget = Toast(self)
        self.toast_widget.hide()
        self.ui_queue: queue.Queue = queue.Queue()
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(100)
        self._queue_timer.timeout.connect(self._drain_ui_queue)
        self._queue_timer.start()

        QTimer.singleShot(300, self._background_startup)
        if self.mic_recovery:
            QTimer.singleShot(1200, lambda: self.toast(self.mic_recovery, "warn"))

    def _build_shell(self) -> None:
        central = QWidget(self)
        central.setStyleSheet(theme.frame_style(theme.APP_BG, 0))
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = QFrame(central)
        self.sidebar.setFixedWidth(220)
        self.sidebar.setStyleSheet(theme.frame_style(theme.SIDEBAR_BG, 0))
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(12, 22, 12, 14)
        sidebar_layout.setSpacing(4)

        brand = QLabel("AI Voice Studio", self.sidebar)
        brand.setFont(theme.font(20, "bold"))
        brand.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        brand.setContentsMargins(4, 0, 0, 0)
        sidebar_layout.addWidget(brand)
        subtitle = QLabel("Text · Speech · Voice", self.sidebar)
        subtitle.setFont(theme.font(11))
        subtitle.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        subtitle.setContentsMargins(4, 0, 0, 14)
        sidebar_layout.addWidget(subtitle)

        self._nav_buttons: dict[str, QPushButton] = {}
        for key, label in SCREENS:
            button = QPushButton(label, self.sidebar)
            button.setFixedHeight(40)
            button.setFont(theme.font(14))
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                theme.button_style("transparent", theme.CARD_BG, theme.TEXT, 10)
            )
            button.clicked.connect(
                lambda _checked=False, name=key: self.show_screen(name)
            )
            sidebar_layout.addWidget(button)
            self._nav_buttons[key] = button

        sidebar_layout.addStretch(1)
        self.version_lbl = QLabel(f"v{config.APP_VERSION}", self.sidebar)
        self.version_lbl.setFont(theme.font(11))
        self.version_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        self.version_lbl.setContentsMargins(4, 8, 0, 0)
        sidebar_layout.addWidget(self.version_lbl)
        root.addWidget(self.sidebar)

        self.content = QFrame(central)
        self.content.setStyleSheet(theme.frame_style(theme.APP_BG, 0))
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(28, 18, 28, 20)
        content_layout.setSpacing(6)

        self.title_lbl = QLabel("Home", self.content)
        self.title_lbl.setFont(theme.font(24, "bold"))
        self.title_lbl.setStyleSheet(theme.label_style(theme.TEXT, "left"))
        content_layout.addWidget(self.title_lbl)

        self.subtitle_lbl = QLabel("", self.content)
        self.subtitle_lbl.setFont(theme.font(13))
        self.subtitle_lbl.setStyleSheet(theme.label_style(theme.SUBTEXT, "left"))
        content_layout.addWidget(self.subtitle_lbl)

        self.screen_container = QStackedWidget(self.content)
        self.screen_container.setStyleSheet(theme.frame_style(theme.APP_BG, 0))
        content_layout.addWidget(self.screen_container, 1)
        root.addWidget(self.content, 1)
        self.setCentralWidget(central)

    def _auto_fit_window(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(*map(int, WINDOW_SIZE.split("x")))
            return
        available = screen.availableGeometry()
        target_w = min(MAX_WIDTH, max(MIN_SIZE[0], available.width() - 60))
        target_h = min(MAX_HEIGHT, max(MIN_SIZE[1], available.height() - 110))
        self.resize(target_w, target_h)
        self.move(
            available.x() + max(0, (available.width() - target_w) // 2),
            available.y() + max(0, (available.height() - target_h) // 2),
        )

    def _build_screens(self) -> None:
        self._screens = {
            "home": HomeScreen(self.screen_container, self),
            "text_to_speech": TextToSpeechScreen(self.screen_container, self),
            "voice_recorder": VoiceRecorderScreen(self.screen_container, self),
            "speech_to_text": SpeechToTextScreen(self.screen_container, self),
            "my_voice": MyVoiceScreen(self.screen_container, self),
            "models": ModelsScreen(self.screen_container, self),
        "voices": VoicesScreen(self.screen_container, self),
            "history": HistoryScreen(self.screen_container, self),
            "settings": SettingsScreen(self.screen_container, self),
        }
        for screen in self._screens.values():
            self.screen_container.addWidget(screen)

    def show_screen(self, key: str, **kwargs) -> None:
        if key not in self._screens:
            return
        if self._current == key and not kwargs:
            return
        if self._current is not None:
            self._screens[self._current].on_hide()
        self._current = key
        screen = self._screens[key]
        self.screen_container.setCurrentWidget(screen)
        screen.on_show(**kwargs)
        self.title_lbl.setText(dict(SCREENS)[key])
        self.subtitle_lbl.setText(_SUBTITLES.get(key, ""))
        for nav_key, button in self._nav_buttons.items():
            if nav_key == key:
                button.setStyleSheet(
                    theme.button_style(
                        theme.ACCENT, theme.ACCENT_HOVER, theme.ON_ACCENT, 10
                    )
                )
            else:
                button.setStyleSheet(
                    theme.button_style("transparent", theme.CARD_BG, theme.TEXT, 10)
                )

    def send_text_to_tts(self, text: str, language: str | None = None) -> None:
        self.show_screen("text_to_speech", prefill=text, prefill_lang=language)

    @property
    def active_cloner(self) -> VoiceCloner | QwenVoiceCloner:
        engine = self.settings.get("clone", "engine", "xtts")
        return self.qwen_cloner if engine == "qwen" else self.cloner

    def schedule(self, fn: Callable, *args, **kwargs) -> None:
        self.ui_queue.put((fn, args, kwargs))

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                fn, args, kwargs = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception:
                pass

    def toast(self, message: str, kind: str = "info") -> None:
        if self.toast_widget is not None:
            self.toast_widget.show(message, kind)

    def _background_startup(self) -> None:
        self._repair_tts_defaults()
        self._prewarm_translation()
        self.tts.refresh_voices_async(on_done=self._offer_voice_prefetch)

    def _offer_voice_prefetch(self) -> None:
        """Ask once whether to fetch every Piper voice.

        Two things this has to get right. The catalog is not cached on a fresh
        install, so reading the cache alone would mean the very first launch --
        the only launch where the offer matters -- never asks at all; so the
        catalog is fetched here, in the background, before asking. And this runs
        as a completion callback on the runner's thread, so the question is
        marshalled onto the UI thread with schedule(): a QMessageBox built
        off-thread is undefined behaviour, and it is the first thing a new user
        sees.
        """
        if self._prefetch_offer_started:
            return
        self._prefetch_offer_started = True

        from app.gui.screens.voices import prefetch_already_offered

        if prefetch_already_offered():
            # Already asked (or already installed everything). Do not spend a
            # network request on the catalog just to decide not to speak.
            return

        async def warm_then_ask() -> None:
            from app.core.piper_voices import PiperVoiceLibrary

            try:
                PiperVoiceLibrary().catalog()
            except Exception:  # noqa: BLE001 - offline start-up is not an error
                return
            self.schedule(self._ask_voice_prefetch)

        try:
            self.runner.run(warm_then_ask())
        except Exception:  # noqa: BLE001 - never let this break startup
            pass

    def _ask_voice_prefetch(self) -> None:
        from app.gui.screens.voices import offer_bulk_prefetch

        try:
            offer_bulk_prefetch(self)
        except Exception:  # noqa: BLE001 - a failed offer must not block startup
            pass

    @staticmethod
    def _prewarm_translation() -> None:
        """Warm Argos' Python packages off the UI thread.

        This used to double as the guard against Windows error 1114, but it
        could not be: it runs from ``_background_startup`` on a 0 ms timer, long
        after Qt is loaded, and the first runtime bound into the process is the
        one everything else inherits. The real guard is
        :func:`app.core.native_runtime.preload_native_runtime`, called from
        ``main.py`` before PyQt5 is imported. What is left here is the part that
        is genuinely a background concern -- reading and importing the Argos
        package so the first translation does not pay for it on the UI thread.
        """
        if not module_installed("argostranslate"):
            return

        def warm() -> None:
            soft_import("argostranslate.translate")

        threading.Thread(target=warm, name="argos-prewarm", daemon=True).start()

    def _repair_tts_defaults(self) -> None:
        if (
            self.tts.backend == "qwen3"
            and config.QWEN_GPU_ONLY
            and not self.tts.qwen_gpu_present
        ):
            self.settings.set("tts", "backend", "system")
            self.tts.set_backend("system")
        voice = str(self.settings.get("tts", "voice", "") or "")
        if voice.startswith("qwen-") and self.tts.backend != "qwen3":
            lang = (
                voice.split("-", 1)[1]
                if "-" in voice
                else str(self.settings.get("tts", "language", "en") or "en")
            )
            resolved = self.tts._system_fallback_voice(lang)
            if resolved:
                self.settings.set("tts", "voice", resolved)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._current is not None:
            self._screens[self._current].on_hide()
        for screen in self._screens.values():
            try:
                screen.shutdown()
            except Exception:
                pass
        try:
            self.runner.stop()
        except Exception:
            pass
        if self.player is not None:
            try:
                self.player.close()
            except Exception:
                pass
        self.close()

    def closeEvent(self, event) -> None:
        if not self._closing:
            self._on_close()
        event.accept()


def run() -> None:
    from main import main

    raise SystemExit(main())
