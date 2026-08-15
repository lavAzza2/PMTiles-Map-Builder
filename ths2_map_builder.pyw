from __future__ import annotations

from pathlib import Path
import os
import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from i18n import get_language, save_language, set_language, tr
from map_builder_core import (
    cache_to_pmtiles,
    convert_mbtiles,
    find_pmtiles_cli,
    format_bytes,
    parse_hlg,
    raster_to_pmtiles,
    read_pmtiles_header,
    write_report,
    working_space_requirements,
    xyz_to_pmtiles,
)


APP_VERSION = "0.4.0"

COLORS = {
    "background": "#0b1220",
    "surface": "#111b2e",
    "surface_hover": "#1a2942",
    "field": "#0d1728",
    "border": "#293a55",
    "text": "#eef4ff",
    "muted": "#9aabc2",
    "accent": "#4da3ff",
    "accent_hover": "#70b5ff",
    "warning": "#f4bd62",
    "success": "#54d394",
}


def find_sas_root() -> Path | None:
    configured = os.environ.get("SASPLANET_HOME", "").strip()
    if configured and Path(configured).is_dir():
        return Path(configured)
    desktop = Path.home() / "Desktop"
    candidates = [path for path in desktop.glob("SASPlanet*") if path.is_dir()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


SAS_ROOT = find_sas_root()
DEFAULT_CACHE = SAS_ROOT / "cache_sqlite" / "sat" if SAS_ROOT else Path()
DEFAULT_SELECTION = SAS_ROOT / "LastSelection.hlg" if SAS_ROOT else Path()
DEFAULT_OUTPUT_DIR = Path.home() / "Desktop"
if not DEFAULT_OUTPUT_DIR.is_dir():
    DEFAULT_OUTPUT_DIR = Path.home()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("THS2 Map Builder")
        self.geometry("920x870")
        self.minsize(780, 780)
        self.configure(background=COLORS["background"])
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.mode = tk.StringVar(value="cache")
        self.mbtiles = tk.StringVar()
        self.raster = tk.StringVar()
        self.xyz_folder = tk.StringVar()
        self.cache = tk.StringVar(value=str(DEFAULT_CACHE) if SAS_ROOT else "")
        self.selection = tk.StringVar(value=str(DEFAULT_SELECTION) if SAS_ROOT else "")
        self.output = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR / "THS2-map.pmtiles"))
        self.name = tk.StringVar(value=tr("Карта THS2", "THS2 Map"))
        self.attribution = tk.StringVar()
        self.min_zoom = tk.IntVar(value=13)
        self.max_zoom = tk.IntVar(value=19)
        self.raster_format = tk.StringVar(value="PNG")
        self.jpeg_quality = tk.IntVar(value=85)
        self.temp_dir = tk.StringVar()
        self.fast_mode = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value=tr("Готов к работе", "Ready"))
        self.started_at: float | None = None
        self.current_stage = ""
        self.current_percent: int | None = None
        self.diagnostic_path: Path | None = None
        self._configure_theme()
        self._build()
        self.after_idle(self._apply_dark_titlebar)
        self.after(100, self._poll)

    def _configure_theme(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background=COLORS["background"])
        style.configure(
            "Card.TFrame", background=COLORS["surface"], relief="flat"
        )
        style.configure(
            "TLabel", background=COLORS["background"], foreground=COLORS["text"]
        )
        style.configure(
            "Card.TLabel", background=COLORS["surface"], foreground=COLORS["text"]
        )
        style.configure(
            "Muted.TLabel", background=COLORS["background"], foreground=COLORS["muted"]
        )
        style.configure(
            "CardMuted.TLabel", background=COLORS["surface"], foreground=COLORS["muted"]
        )
        style.configure(
            "Warning.TLabel", background=COLORS["surface"], foreground=COLORS["warning"]
        )
        style.configure(
            "Title.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Segoe UI Semibold", 21),
        )
        style.configure(
            "TLabelframe",
            background=COLORS["surface"],
            bordercolor=COLORS["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "TEntry",
            fieldbackground=COLORS["field"],
            foreground=COLORS["text"],
            insertcolor=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=6,
        )
        style.map("TEntry", bordercolor=[("focus", COLORS["accent"])])
        for widget_style in ("TSpinbox", "TCombobox"):
            style.configure(
                widget_style,
                fieldbackground=COLORS["field"],
                background=COLORS["surface_hover"],
                foreground=COLORS["text"],
                arrowcolor=COLORS["text"],
                bordercolor=COLORS["border"],
                padding=5,
            )
            style.map(
                widget_style,
                fieldbackground=[("readonly", COLORS["field"])],
                foreground=[("readonly", COLORS["text"])],
                bordercolor=[("focus", COLORS["accent"])],
            )
        style.configure(
            "TButton",
            background=COLORS["surface_hover"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            padding=(12, 7),
        )
        style.map(
            "TButton",
            background=[("active", COLORS["border"]), ("pressed", COLORS["field"])],
            foreground=[("disabled", COLORS["muted"])],
        )
        style.configure(
            "Primary.TButton",
            background=COLORS["accent"],
            foreground="#07111f",
            bordercolor=COLORS["accent"],
            font=("Segoe UI Semibold", 10),
            padding=(16, 8),
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLORS["accent_hover"]), ("pressed", COLORS["accent"])],
        )
        style.configure(
            "TCheckbutton", background=COLORS["surface"], foreground=COLORS["text"]
        )
        style.map(
            "TCheckbutton",
            background=[("active", COLORS["surface"])],
            indicatorcolor=[("selected", COLORS["accent"]), ("!selected", COLORS["field"])],
        )
        style.configure(
            "TNotebook", background=COLORS["background"], borderwidth=0, tabmargins=(0, 0, 0, 0)
        )
        style.configure(
            "TNotebook.Tab",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
            bordercolor=COLORS["border"],
            padding=(16, 9),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["accent"]), ("active", COLORS["surface_hover"])],
            foreground=[("selected", "#07111f"), ("active", COLORS["text"])],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=COLORS["accent"],
            troughcolor=COLORS["field"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
        )

    def _apply_dark_titlebar(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            enabled = ctypes.c_int(1)
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            for attribute in (20, 19):
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                )
                if result == 0:
                    break
        except Exception:
            pass

    def _build(self) -> None:
        root = ttk.Frame(self, padding=(24, 20))
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(
            header, text=f"THS2 Map Builder  {APP_VERSION}", style="Title.TLabel"
        ).pack(side="left")
        self.settings_button = ttk.Button(
            header,
            text=tr("⚙  Настройки", "⚙  Settings"),
            command=self._open_settings,
        )
        self.settings_button.pack(side="right")
        ttk.Label(
            root,
            text=tr(
                "Создаёт и проверяет офлайн-карту для THS2. Кэш SAS.Planet не изменяется.",
                "Creates and verifies offline maps for THS2. The SAS.Planet cache is never modified.",
            ),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 16))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="x")
        self.mode_tabs: dict[str, ttk.Frame] = {}
        for mode, title in (
            ("cache", "SAS.Planet"),
            ("mbtiles", "MBTiles"),
            ("xyz", "XYZ"),
            ("raster", "GeoTIFF / KMZ"),
        ):
            tab = ttk.Frame(self.notebook, padding=14, style="Card.TFrame")
            self.mode_tabs[mode] = tab
            self.notebook.add(tab, text=title)
            tab.columnconfigure(0, weight=1)

        cache_tab = self.mode_tabs["cache"]
        self._path_row(cache_tab, tr("Папка cache_sqlite", "cache_sqlite folder"), self.cache, self._choose_cache, 0)
        self._path_row(cache_tab, tr("Выделение .hlg", ".hlg selection"), self.selection, self._choose_selection, 1)
        self._zoom_controls(cache_tab, tr("SAS.Planet показывает масштаб на единицу выше", "SAS.Planet displays zoom levels one number higher"), 2)
        ttk.Label(
            cache_tab,
            text=tr(
                "Прямая безопасная сборка из кэша — исходные файлы открываются только для чтения.",
                "Safe direct cache conversion — source files are opened read-only.",
            ),
            style="CardMuted.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))

        mbtiles_tab = self.mode_tabs["mbtiles"]
        self._path_row(mbtiles_tab, tr("Файл MBTiles", "MBTiles file"), self.mbtiles, self._choose_mbtiles, 0)
        ttk.Label(
            mbtiles_tab,
            text=tr(
                "Подходит для готового экспорта из SAS.Planet и других программ.",
                "Use an existing export from SAS.Planet or another application.",
            ),
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))

        xyz_tab = self.mode_tabs["xyz"]
        self._path_row(xyz_tab, tr("Папка тайлов XYZ", "XYZ tile folder"), self.xyz_folder, self._choose_xyz, 0)
        ttk.Label(
            xyz_tab,
            text=tr(
                "Поддерживаются 13/5424/2568.jpg и z13/x5424/y2568.jpg. Масштабы определяются автоматически.",
                "Supports 13/5424/2568.jpg and z13/x5424/y2568.jpg. Zoom levels are detected automatically.",
            ),
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))

        raster_tab = self.mode_tabs["raster"]
        self._path_row(raster_tab, tr("GeoTIFF или KMZ", "GeoTIFF or KMZ"), self.raster, self._choose_raster, 0)
        self._zoom_controls(raster_tab, tr("уровни итоговой карты", "output map zoom levels"), 1)
        raster_options = ttk.Frame(raster_tab, style="Card.TFrame")
        raster_options.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(raster_options, text=tr("Формат тайлов:", "Tile format:"), style="Card.TLabel").pack(side="left")
        ttk.Combobox(
            raster_options,
            textvariable=self.raster_format,
            values=("PNG", "JPEG"),
            state="readonly",
            width=7,
        ).pack(side="left", padx=(8, 14))
        ttk.Label(raster_options, text=tr("Качество JPEG:", "JPEG quality:"), style="Card.TLabel").pack(side="left")
        ttk.Spinbox(
            raster_options, from_=50, to=100, width=4, textvariable=self.jpeg_quality
        ).pack(side="left", padx=8)
        ttk.Label(
            raster_options,
            text=tr(
                "PNG сохраняет прозрачность; JPEG обычно заметно меньше",
                "PNG preserves transparency; JPEG is usually much smaller",
            ),
            style="CardMuted.TLabel",
        ).pack(side="left", padx=6)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        details = ttk.LabelFrame(root, text=tr("Готовая карта", "Output map"), padding=12)
        details.pack(fill="x", pady=(12, 0))
        self._entry_row(details, tr("Название", "Name"), self.name, 0)
        self._entry_row(details, tr("Атрибуция", "Attribution"), self.attribution, 1)
        self._path_row(details, tr("Сохранить как", "Save as"), self.output, self._choose_output, 2)
        ttk.Label(
            details,
            text=tr(
                "Укажи правообладателя и условия источника. Пустая атрибуция будет отмечена предупреждением.",
                "Enter the source owner and terms. Empty attribution will trigger a warning.",
            ),
            style="Warning.TLabel",
            wraplength=650,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        advanced = ttk.LabelFrame(root, text=tr("Большие карты", "Large maps"), padding=12)
        advanced.pack(fill="x", pady=(12, 0))
        self._path_row(
            advanced, tr("Временная папка", "Temporary folder"), self.temp_dir, self._choose_temp_dir, 0
        )
        ttk.Checkbutton(
            advanced,
            text=tr(
                "Быстрый режим — без дедупликации (быстрее, итоговый файл может быть больше)",
                "Fast mode — no deduplication (faster, output may be larger)",
            ),
            variable=self.fast_mode,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(
            advanced,
            text=tr(
                "Оставь поле пустым для системной папки TEMP. Для больших карт лучше выбрать диск с запасом места.",
                "Leave empty to use the system TEMP folder. For large maps, choose a drive with ample free space.",
            ),
            style="CardMuted.TLabel",
            wraplength=720,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(14, 8))
        self.create_button = ttk.Button(
            actions, text=tr("Создать карту THS2", "Create THS2 map"), command=self._start, style="Primary.TButton"
        )
        self.create_button.pack(side="left")
        ttk.Button(actions, text=tr("Проверить готовый PMTiles", "Verify PMTiles"), command=self._check).pack(
            side="left", padx=8
        )
        ttk.Label(actions, textvariable=self.status).pack(side="right")

        self.progress_bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(0, 8))

        self.log = tk.Text(
            root,
            height=6,
            wrap="word",
            state="disabled",
            font=("Cascadia Mono", 9),
            background=COLORS["field"],
            foreground=COLORS["muted"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#07111f",
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
        )
        self.log.pack(fill="both", expand=True)
        self._refresh_mode()

    def _zoom_controls(self, parent, hint: str, row: int):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text=tr("Масштабы THS2:", "THS2 zoom levels:"), style="Card.TLabel").pack(side="left")
        ttk.Spinbox(frame, from_=0, to=24, width=4, textvariable=self.min_zoom).pack(
            side="left", padx=(8, 4)
        )
        ttk.Label(frame, text="—", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(frame, from_=0, to=24, width=4, textvariable=self.max_zoom).pack(
            side="left", padx=4
        )
        ttk.Label(frame, text=f"({hint})", style="CardMuted.TLabel").pack(side="left", padx=8)
        return frame

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(tr("Настройки", "Settings"))
        dialog.configure(background=COLORS["background"])
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        content = ttk.Frame(dialog, padding=20)
        content.pack(fill="both", expand=True)
        ttk.Label(
            content,
            text=tr("Настройки приложения", "Application settings"),
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            content,
            text=tr(
                "При первом запуске язык выбирается по настройкам Windows.",
                "On first launch, the language follows your Windows settings.",
            ),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 16))

        language_row = ttk.Frame(content)
        language_row.pack(fill="x")
        ttk.Label(language_row, text=tr("Язык", "Language"), width=16).pack(side="left")
        choices = {"Русский": "ru", "English": "en"}
        selected_name = tk.StringVar(
            value=next(name for name, code in choices.items() if code == get_language())
        )
        language_box = ttk.Combobox(
            language_row,
            textvariable=selected_name,
            values=tuple(choices),
            state="readonly",
            width=20,
        )
        language_box.pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(20, 0))

        def apply_settings() -> None:
            language = choices[selected_name.get()]
            previous_language = get_language()
            old_default_name = tr("Карта THS2", "THS2 Map")
            set_language(language)
            try:
                save_language(language)
            except OSError as error:
                set_language(previous_language)
                messagebox.showerror(
                    tr("Настройки", "Settings"),
                    tr(f"Не удалось сохранить настройки:\n{error}", f"Could not save settings:\n{error}"),
                    parent=dialog,
                )
                return
            if self.name.get() == old_default_name:
                self.name.set(tr("Карта THS2", "THS2 Map"))
            self.status.set(tr("Готов к работе", "Ready"))
            dialog.destroy()
            for child in self.winfo_children():
                child.destroy()
            self._configure_theme()
            self._build()

        ttk.Button(
            buttons,
            text=tr("Сохранить", "Save"),
            command=apply_settings,
            style="Primary.TButton",
        ).pack(side="right")
        ttk.Button(
            buttons, text=tr("Отмена", "Cancel"), command=dialog.destroy
        ).pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _path_row(self, parent, label, variable, command, row):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=3)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label, width=20, style="Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(frame, textvariable=variable).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(frame, text=tr("Выбрать…", "Browse…"), command=command).grid(row=0, column=2)
        parent.columnconfigure(0, weight=1)
        return frame

    def _entry_row(self, parent, label, variable, row):
        ttk.Label(parent, text=label, width=20, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=3
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=3
        )
        parent.columnconfigure(1, weight=1)

    def _refresh_mode(self):
        if hasattr(self, "notebook") and self.mode.get() in self.mode_tabs:
            self.notebook.select(self.mode_tabs[self.mode.get()])

    def _on_tab_changed(self, _event=None):
        selected = self.notebook.select()
        for mode, tab in self.mode_tabs.items():
            if str(tab) == selected:
                self.mode.set(mode)
                break

    def _choose_mbtiles(self):
        value = filedialog.askopenfilename(filetypes=[("MBTiles", "*.mbtiles")])
        if value:
            self.mbtiles.set(value)
            self.output.set(str(Path(value).with_suffix(".pmtiles")))

    def _choose_raster(self):
        value = filedialog.askopenfilename(
            filetypes=[
                (tr("Геопривязанные растры", "Georeferenced rasters"), "*.tif *.tiff *.kmz"),
                ("GeoTIFF", "*.tif *.tiff"),
                ("KMZ", "*.kmz"),
                (tr("Все файлы", "All files"), "*.*"),
            ]
        )
        if value:
            self.raster.set(value)
            self.name.set(Path(value).stem)
            self.output.set(str(Path(value).with_suffix(".pmtiles")))

    def _choose_xyz(self):
        value = filedialog.askdirectory(
            title=tr(
                "Выбери папку, внутри которой находятся z/x/y",
                "Select the folder that contains z/x/y tiles",
            )
        )
        if value:
            self.xyz_folder.set(value)
            self.name.set(Path(value).name)
            self.output.set(str(DEFAULT_OUTPUT_DIR / f"{Path(value).name}.pmtiles"))

    def _choose_cache(self):
        value = filedialog.askdirectory()
        if value:
            self.cache.set(value)

    def _choose_selection(self):
        value = filedialog.askopenfilename(filetypes=[("SAS selection", "*.hlg")])
        if value:
            self.selection.set(value)

    def _choose_output(self):
        value = filedialog.asksaveasfilename(
            defaultextension=".pmtiles", filetypes=[("PMTiles", "*.pmtiles")]
        )
        if value:
            self.output.set(value)

    def _choose_temp_dir(self):
        value = filedialog.askdirectory(
            title=tr("Временная папка для больших файлов", "Temporary folder for large files")
        )
        if value:
            self.temp_dir.set(value)

    def _selected_source(self) -> Path:
        raw_value = {
            "mbtiles": self.mbtiles.get(),
            "raster": self.raster.get(),
            "xyz": self.xyz_folder.get(),
            "cache": self.cache.get(),
        }[self.mode.get()].strip()
        if not raw_value:
            raise ValueError(tr("Сначала выбери источник карты.", "Select a map source first."))
        return Path(raw_value)

    @staticmethod
    def _existing_parent(path: Path) -> Path:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate

    def _preflight(self, output: Path) -> tuple[Path, str | None]:
        source = self._selected_source()
        if not source.exists():
            raise FileNotFoundError(
                tr(f"Не найден источник карты: {source}", f"Map source not found: {source}")
            )
        if self.mode.get() == "cache":
            selection = Path(self.selection.get().strip()) if self.selection.get().strip() else None
            if selection is None or not selection.is_file():
                raise FileNotFoundError(
                    tr(
                        "Выбери существующий файл выделения LastSelection.hlg.",
                        "Select an existing LastSelection.hlg selection file.",
                    )
                )
        temporary = Path(self.temp_dir.get().strip()) if self.temp_dir.get().strip() else Path(tempfile.gettempdir())
        if not temporary.is_dir():
            raise FileNotFoundError(
                tr(f"Не найдена временная папка: {temporary}", f"Temporary folder not found: {temporary}")
            )
        output_parent = self._existing_parent(output.parent)
        if not output_parent.is_dir():
            raise FileNotFoundError(
                tr(f"Не найдена папка результата: {output.parent}", f"Output folder not found: {output.parent}")
            )

        input_size = source.stat().st_size if source.is_file() else 0
        requirements = working_space_requirements(input_size, self.mode.get())
        temp_free = shutil.disk_usage(temporary).free
        output_free = shutil.disk_usage(output_parent).free
        same_drive = temporary.resolve().anchor.lower() == output_parent.resolve().anchor.lower()
        required = requirements["temporary"] + requirements["output"] if same_drive else requirements["temporary"]
        available = temp_free if same_drive else min(temp_free, output_free)
        if available < max(512 * 1024 ** 2, input_size):
            raise OSError(
                tr(
                    f"Недостаточно свободного места для безопасной конвертации. Доступно {format_bytes(available)}.",
                    f"Not enough free space for safe conversion. Available: {format_bytes(available)}.",
                )
            )
        warning = None
        if temp_free < required or (not same_drive and output_free < requirements["output"]):
            warning = (
                tr(
                    "Свободного места меньше рекомендуемого.\n\n"
                    f"Временная папка: {temporary}\n"
                    f"Свободно: {format_bytes(temp_free)}\n"
                    f"Рекомендуется: {format_bytes(requirements['temporary'])}\n\n"
                    f"Папка результата: {output_parent}\n"
                    f"Свободно: {format_bytes(output_free)}\n"
                    f"Рекомендуется: {format_bytes(requirements['output'])}\n\n"
                    "Продолжить на свой риск?",
                    "Free space is below the recommendation.\n\n"
                    f"Temporary folder: {temporary}\n"
                    f"Free: {format_bytes(temp_free)}\n"
                    f"Recommended: {format_bytes(requirements['temporary'])}\n\n"
                    f"Output folder: {output_parent}\n"
                    f"Free: {format_bytes(output_free)}\n"
                    f"Recommended: {format_bytes(requirements['output'])}\n\n"
                    "Continue at your own risk?",
                )
            )
        return temporary, warning

    def _append(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _worker_log(self, line: str):
        if self.diagnostic_path:
            try:
                with self.diagnostic_path.open("a", encoding="utf-8") as stream:
                    stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(line) + "\n")
            except OSError:
                pass
        self.queue.put(("log", line))

    def _worker_progress(self, stage: str, percent: int | None):
        self.queue.put(("progress", (stage, percent)))

    def _start(self):
        if int(self.min_zoom.get()) > int(self.max_zoom.get()):
            messagebox.showerror(
                tr("Масштабы", "Zoom levels"),
                tr("Минимальный масштаб больше максимального.", "Minimum zoom is greater than maximum zoom."),
            )
            return
        output = Path(self.output.get())
        if not self.output.get().strip():
            messagebox.showerror(
                tr("Результат", "Output"),
                tr("Укажи, куда сохранить готовый PMTiles.", "Choose where to save the PMTiles file."),
            )
            return
        try:
            temporary, disk_warning = self._preflight(output)
        except Exception as error:
            messagebox.showerror(tr("Проверка перед запуском", "Preflight check"), str(error))
            return
        if disk_warning and not messagebox.askyesno(tr("Мало свободного места", "Low disk space"), disk_warning):
            return
        if output.exists() and not messagebox.askyesno(
            tr("Файл уже существует", "File already exists"),
            tr(
                f"{output}\n\nЗаменить этот файл новой картой?",
                f"{output}\n\nReplace this file with the new map?",
            ),
        ):
            return
        if not self.attribution.get().strip():
            if not messagebox.askyesno(
                tr("Нет атрибуции", "Missing attribution"),
                tr(
                    "Атрибуция источника не указана. Продолжить только если условия источника это допускают?",
                    "Source attribution is empty. Continue only if the source terms allow this?",
                ),
            ):
                return
        job = {
            "mode": self.mode.get(),
            "mbtiles": self.mbtiles.get(),
            "raster": self.raster.get(),
            "xyz_folder": self.xyz_folder.get(),
            "cache": self.cache.get(),
            "selection": self.selection.get(),
            "output": str(output),
            "name": self.name.get().strip(),
            "attribution": self.attribution.get().strip(),
            "min_zoom": int(self.min_zoom.get()),
            "max_zoom": int(self.max_zoom.get()),
            "raster_format": self.raster_format.get(),
            "jpeg_quality": int(self.jpeg_quality.get()),
            "temp_dir": str(temporary),
            "fast_mode": bool(self.fast_mode.get()),
        }
        self.diagnostic_path = output.with_suffix(".diagnostic.log")
        try:
            self.diagnostic_path.write_text(
                f"THS2 Map Builder {APP_VERSION}\n"
                + tr(
                    f"Запуск: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Режим: {job['mode']}\nИсточник: {self._selected_source()}\n"
                    f"Результат: {output}\nВременная папка: {temporary}\n"
                    f"Быстрый режим: {'да' if job['fast_mode'] else 'нет'}\n",
                    f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Mode: {job['mode']}\nSource: {self._selected_source()}\n"
                    f"Output: {output}\nTemporary folder: {temporary}\n"
                    f"Fast mode: {'yes' if job['fast_mode'] else 'no'}\n",
                ),
                encoding="utf-8",
            )
        except OSError as error:
            messagebox.showerror(
                tr("Диагностический лог", "Diagnostic log"),
                tr(f"Не удалось создать лог:\n{error}", f"Could not create the log:\n{error}"),
            )
            return
        self.create_button.configure(state="disabled")
        self.settings_button.configure(state="disabled")
        self.started_at = time.monotonic()
        self.current_stage = tr("Подготовка", "Preparing")
        self.current_percent = 0
        self.progress_bar["value"] = 0
        self.status.set(tr("Подготовка — 0%", "Preparing — 0%"))
        self._append("—" * 54)
        self._append(
            tr(f"Диагностический лог: {self.diagnostic_path}", f"Diagnostic log: {self.diagnostic_path}")
        )
        threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job: dict):
        try:
            cli = find_pmtiles_cli()
            output = Path(job["output"])
            temporary = Path(job["temp_dir"])
            if job["mode"] == "mbtiles":
                result = convert_mbtiles(
                    Path(job["mbtiles"]),
                    output,
                    cli,
                    self._worker_log,
                    job["name"],
                    job["attribution"],
                    no_deduplication=job["fast_mode"],
                    temp_dir=temporary,
                    progress=self._worker_progress,
                )
                report = {
                    "status": "ok",
                    "source": job["mbtiles"],
                    "output": str(output),
                    "pmtiles": result,
                    "attribution_notice": job["attribution"],
                    "fast_mode": job["fast_mode"],
                    "diagnostic_log": str(self.diagnostic_path),
                }
                write_report(output.with_suffix(".report.json"), report)
            elif job["mode"] == "raster":
                result = raster_to_pmtiles(
                    Path(job["raster"]), output,
                    job["min_zoom"], job["max_zoom"],
                    job["name"], job["attribution"], cli,
                    job["raster_format"], job["jpeg_quality"],
                    log=self._worker_log,
                    no_deduplication=job["fast_mode"],
                    temp_dir=temporary,
                    progress=self._worker_progress,
                )
            elif job["mode"] == "xyz":
                result = xyz_to_pmtiles(
                    Path(job["xyz_folder"]), output,
                    job["name"], job["attribution"],
                    cli, self._worker_log,
                    no_deduplication=job["fast_mode"],
                    temp_dir=temporary,
                    progress=self._worker_progress,
                )
            else:
                selection = parse_hlg(Path(job["selection"]))
                self._worker_log(
                    tr(f"Границы выделения: {selection['bbox']}", f"Selection bounds: {selection['bbox']}")
                )
                result = cache_to_pmtiles(
                    Path(job["cache"]),
                    Path(job["selection"]),
                    output,
                    job["min_zoom"],
                    job["max_zoom"],
                    job["name"],
                    job["attribution"],
                    cli,
                    self._worker_log,
                    no_deduplication=job["fast_mode"],
                    temp_dir=temporary,
                    progress=self._worker_progress,
                )
            self._worker_log(
                tr("Конвертация и проверка завершены успешно.", "Conversion and verification completed successfully.")
            )
            self.queue.put(("done", (output, result)))
        except Exception as error:
            self._worker_log(tr(f"ОШИБКА: {error}", f"ERROR: {error}"))
            self.queue.put(("error", str(error)))

    def _check(self):
        value = filedialog.askopenfilename(filetypes=[("PMTiles", "*.pmtiles")])
        if not value:
            return
        try:
            header = read_pmtiles_header(Path(value))
            cli = find_pmtiles_cli()
            from map_builder_core import run_pmtiles
            run_pmtiles(cli, ["verify", value], self._append)
            messagebox.showinfo(
                tr("Карта исправна", "Map is valid"),
                f"PMTiles v{header['version']}\n"
                + tr(
                    f"Масштабы: {header['min_zoom']}–{header['max_zoom']}\n"
                    f"Тайлов: {header['addressed_tiles']}\n"
                    f"Размер: {header['size'] / 1024 / 1024:.1f} МБ",
                    f"Zoom levels: {header['min_zoom']}–{header['max_zoom']}\n"
                    f"Tiles: {header['addressed_tiles']}\n"
                    f"Size: {header['size'] / 1024 / 1024:.1f} MB",
                ),
            )
        except Exception as error:
            messagebox.showerror(tr("Проверка не пройдена", "Verification failed"), str(error))

    def _poll(self):
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "log":
                    self._append(str(value))
                elif kind == "progress":
                    self.current_stage, self.current_percent = value
                    if self.current_percent is None:
                        self.progress_bar.configure(mode="indeterminate")
                        self.progress_bar.start(12)
                    else:
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                        self.progress_bar["value"] = self.current_percent
                elif kind == "done":
                    output, result = value
                    self.create_button.configure(state="normal")
                    self.settings_button.configure(state="normal")
                    elapsed = time.monotonic() - self.started_at if self.started_at else 0
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self.progress_bar["value"] = 100
                    self.status.set(
                        tr(f"Готово за {elapsed / 60:.1f} мин", f"Completed in {elapsed / 60:.1f} min")
                    )
                    self.started_at = None
                    pm = result.get("pmtiles", result)
                    cache = result.get("cache")
                    missing = cache.get("missing", 0) if cache else 0
                    text = (
                        f"{output}\n\n"
                        + tr(
                            f"Тайлов: {pm.get('addressed_tiles', '—')}\n"
                            f"Масштабы: {pm.get('min_zoom', '—')}–{pm.get('max_zoom', '—')}",
                            f"Tiles: {pm.get('addressed_tiles', '—')}\n"
                            f"Zoom levels: {pm.get('min_zoom', '—')}–{pm.get('max_zoom', '—')}",
                        )
                    )
                    if missing:
                        messagebox.showwarning(
                            tr("Карта создана с пропусками", "Map created with missing tiles"),
                            text + tr(
                                f"\n\nНе найдено тайлов: {missing}. Повтори скачивание в SAS.Planet.",
                                f"\n\nMissing tiles: {missing}. Download them again in SAS.Planet.",
                            ),
                        )
                    else:
                        messagebox.showinfo(tr("Карта готова", "Map is ready"), text)
                elif kind == "error":
                    self.create_button.configure(state="normal")
                    self.settings_button.configure(state="normal")
                    self.progress_bar.stop()
                    self.status.set(tr("Ошибка", "Error"))
                    self.started_at = None
                    messagebox.showerror(tr("Не удалось создать карту", "Could not create the map"), str(value))
        except queue.Empty:
            pass
        if self.started_at is not None:
            elapsed = int(time.monotonic() - self.started_at)
            suffix = f" — {self.current_percent}%" if self.current_percent is not None else ""
            self.status.set(
                f"{self.current_stage}{suffix} · {elapsed // 60:02d}:{elapsed % 60:02d}"
            )
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
