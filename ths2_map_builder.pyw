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


APP_VERSION = "0.3.0"


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
        self.geometry("840x890")
        self.minsize(740, 690)
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.mode = tk.StringVar(value="cache")
        self.mbtiles = tk.StringVar()
        self.raster = tk.StringVar()
        self.xyz_folder = tk.StringVar()
        self.cache = tk.StringVar(value=str(DEFAULT_CACHE) if SAS_ROOT else "")
        self.selection = tk.StringVar(value=str(DEFAULT_SELECTION) if SAS_ROOT else "")
        self.output = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR / "THS2-map.pmtiles"))
        self.name = tk.StringVar(value="Карта THS2")
        self.attribution = tk.StringVar()
        self.min_zoom = tk.IntVar(value=13)
        self.max_zoom = tk.IntVar(value=19)
        self.raster_format = tk.StringVar(value="PNG")
        self.jpeg_quality = tk.IntVar(value=85)
        self.temp_dir = tk.StringVar()
        self.fast_mode = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Готов к работе")
        self.started_at: float | None = None
        self.current_stage = ""
        self.current_percent: int | None = None
        self.diagnostic_path: Path | None = None
        self._build()
        self.after(100, self._poll)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=22)
        root.pack(fill="both", expand=True)
        ttk.Label(
            root, text=f"THS2 Map Builder {APP_VERSION}", font=("Segoe UI", 20, "bold")
        ).pack(anchor="w")
        ttk.Label(
            root,
            text="Создаёт и проверяет офлайн-карту для THS2. Кэш SAS.Planet не изменяется.",
            foreground="#555555",
        ).pack(anchor="w", pady=(3, 18))

        modes = ttk.LabelFrame(root, text="1. Откуда взять карту", padding=12)
        modes.pack(fill="x")
        ttk.Radiobutton(
            modes,
            text="MBTiles — готовый архив тайлов",
            variable=self.mode,
            value="mbtiles",
            command=self._refresh_mode,
        ).pack(anchor="w")
        ttk.Radiobutton(
            modes,
            text="Напрямую из кэша SAS.Planet — рекомендуется для этой версии",
            variable=self.mode,
            value="cache",
            command=self._refresh_mode,
        ).pack(anchor="w", pady=(5, 0))
        ttk.Radiobutton(
            modes,
            text="GeoTIFF или KMZ — геопривязанный растр",
            variable=self.mode,
            value="raster",
            command=self._refresh_mode,
        ).pack(anchor="w", pady=(5, 0))
        ttk.Radiobutton(
            modes,
            text="Папка XYZ — тайлы в папках z/x/y",
            variable=self.mode,
            value="xyz",
            command=self._refresh_mode,
        ).pack(anchor="w", pady=(5, 0))

        self.inputs = ttk.LabelFrame(root, text="2. Файлы и область", padding=12)
        self.inputs.pack(fill="x", pady=12)
        self.mb_row = self._path_row(
            self.inputs, "Файл MBTiles", self.mbtiles, self._choose_mbtiles, 0
        )
        self.raster_row = self._path_row(
            self.inputs, "GeoTIFF или KMZ", self.raster, self._choose_raster, 1
        )
        self.xyz_row = self._path_row(
            self.inputs, "Папка тайлов XYZ", self.xyz_folder, self._choose_xyz, 2
        )
        self.cache_row = self._path_row(
            self.inputs, "Папка cache_sqlite", self.cache, self._choose_cache, 3
        )
        self.selection_row = self._path_row(
            self.inputs, "Выделение .hlg", self.selection, self._choose_selection, 4
        )
        self.zoom_row = ttk.Frame(self.inputs)
        self.zoom_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(self.zoom_row, text="Масштабы THS2:").pack(side="left")
        ttk.Spinbox(self.zoom_row, from_=0, to=24, width=4, textvariable=self.min_zoom).pack(
            side="left", padx=(8, 4)
        )
        ttk.Label(self.zoom_row, text="—").pack(side="left")
        ttk.Spinbox(self.zoom_row, from_=0, to=24, width=4, textvariable=self.max_zoom).pack(
            side="left", padx=4
        )
        self.zoom_hint = ttk.Label(self.zoom_row, foreground="#666666")
        self.zoom_hint.pack(side="left", padx=8)

        self.raster_options = ttk.Frame(self.inputs)
        self.raster_options.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ttk.Label(self.raster_options, text="Формат тайлов:").pack(side="left")
        ttk.Combobox(
            self.raster_options,
            textvariable=self.raster_format,
            values=("PNG", "JPEG"),
            state="readonly",
            width=7,
        ).pack(side="left", padx=(8, 14))
        ttk.Label(self.raster_options, text="Качество JPEG:").pack(side="left")
        ttk.Spinbox(
            self.raster_options, from_=50, to=100, width=4, textvariable=self.jpeg_quality
        ).pack(side="left", padx=8)
        ttk.Label(
            self.raster_options,
            text="PNG сохраняет прозрачность; JPEG обычно заметно меньше",
            foreground="#666666",
        ).pack(side="left", padx=6)

        details = ttk.LabelFrame(root, text="3. Готовая карта", padding=12)
        details.pack(fill="x")
        self._entry_row(details, "Название", self.name, 0)
        self._entry_row(details, "Атрибуция", self.attribution, 1)
        self._path_row(details, "Сохранить как", self.output, self._choose_output, 2)
        ttk.Label(
            details,
            text="Укажи правообладателя и условия источника. Пустая атрибуция будет отмечена предупреждением.",
            foreground="#8a5a00",
            wraplength=650,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        advanced = ttk.LabelFrame(root, text="4. Большие карты", padding=12)
        advanced.pack(fill="x", pady=(12, 0))
        self._path_row(
            advanced, "Временная папка", self.temp_dir, self._choose_temp_dir, 0
        )
        ttk.Checkbutton(
            advanced,
            text="Быстрый режим — без дедупликации (быстрее, итоговый файл может быть больше)",
            variable=self.fast_mode,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(
            advanced,
            text="Оставь поле пустым для системной папки TEMP. Для больших карт лучше выбрать диск с запасом места.",
            foreground="#666666",
            wraplength=720,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(14, 8))
        self.create_button = ttk.Button(
            actions, text="Создать карту THS2", command=self._start
        )
        self.create_button.pack(side="left")
        ttk.Button(actions, text="Проверить готовый PMTiles", command=self._check).pack(
            side="left", padx=8
        )
        ttk.Label(actions, textvariable=self.status).pack(side="right")

        self.progress_bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(0, 8))

        self.log = tk.Text(root, height=6, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self._refresh_mode()

    def _path_row(self, parent, label, variable, command, row):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=3)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label, width=20).grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=variable).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(frame, text="Выбрать…", command=command).grid(row=0, column=2)
        parent.columnconfigure(0, weight=1)
        return frame

    def _entry_row(self, parent, label, variable, row):
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=3
        )
        parent.columnconfigure(1, weight=1)

    def _refresh_mode(self):
        mode = self.mode.get()
        for row in (self.mb_row, self.raster_row, self.xyz_row, self.cache_row, self.selection_row):
            row.grid_remove()
        if mode == "mbtiles":
            self.mb_row.grid()
        elif mode == "raster":
            self.raster_row.grid()
        elif mode == "xyz":
            self.xyz_row.grid()
        else:
            self.cache_row.grid()
            self.selection_row.grid()
        if mode in {"cache", "raster"}:
            self.zoom_row.grid()
            self.zoom_hint.configure(
                text="(SAS показывает на единицу выше)" if mode == "cache" else "(уровни итоговой карты)"
            )
        else:
            self.zoom_row.grid_remove()
        self.raster_options.grid() if mode == "raster" else self.raster_options.grid_remove()

    def _choose_mbtiles(self):
        value = filedialog.askopenfilename(filetypes=[("MBTiles", "*.mbtiles")])
        if value:
            self.mbtiles.set(value)
            self.output.set(str(Path(value).with_suffix(".pmtiles")))

    def _choose_raster(self):
        value = filedialog.askopenfilename(
            filetypes=[
                ("Геопривязанные растры", "*.tif *.tiff *.kmz"),
                ("GeoTIFF", "*.tif *.tiff"),
                ("KMZ", "*.kmz"),
                ("Все файлы", "*.*"),
            ]
        )
        if value:
            self.raster.set(value)
            self.name.set(Path(value).stem)
            self.output.set(str(Path(value).with_suffix(".pmtiles")))

    def _choose_xyz(self):
        value = filedialog.askdirectory(title="Выбери папку, внутри которой находятся z/x/y")
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
        value = filedialog.askdirectory(title="Временная папка для больших файлов")
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
            raise ValueError("Сначала выбери источник карты.")
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
            raise FileNotFoundError(f"Не найден источник карты: {source}")
        if self.mode.get() == "cache":
            selection = Path(self.selection.get().strip()) if self.selection.get().strip() else None
            if selection is None or not selection.is_file():
                raise FileNotFoundError("Выбери существующий файл выделения LastSelection.hlg.")
        temporary = Path(self.temp_dir.get().strip()) if self.temp_dir.get().strip() else Path(tempfile.gettempdir())
        if not temporary.is_dir():
            raise FileNotFoundError(f"Не найдена временная папка: {temporary}")
        output_parent = self._existing_parent(output.parent)
        if not output_parent.is_dir():
            raise FileNotFoundError(f"Не найдена папка результата: {output.parent}")

        input_size = source.stat().st_size if source.is_file() else 0
        requirements = working_space_requirements(input_size, self.mode.get())
        temp_free = shutil.disk_usage(temporary).free
        output_free = shutil.disk_usage(output_parent).free
        same_drive = temporary.resolve().anchor.lower() == output_parent.resolve().anchor.lower()
        required = requirements["temporary"] + requirements["output"] if same_drive else requirements["temporary"]
        available = temp_free if same_drive else min(temp_free, output_free)
        if available < max(512 * 1024 ** 2, input_size):
            raise OSError(
                "Недостаточно свободного места для безопасной конвертации. "
                f"Доступно {format_bytes(available)}."
            )
        warning = None
        if temp_free < required or (not same_drive and output_free < requirements["output"]):
            warning = (
                "Свободного места меньше рекомендуемого.\n\n"
                f"Временная папка: {temporary}\n"
                f"Свободно: {format_bytes(temp_free)}\n"
                f"Рекомендуется: {format_bytes(requirements['temporary'])}\n\n"
                f"Папка результата: {output_parent}\n"
                f"Свободно: {format_bytes(output_free)}\n"
                f"Рекомендуется: {format_bytes(requirements['output'])}\n\n"
                "Продолжить на свой риск?"
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
            messagebox.showerror("Масштабы", "Минимальный масштаб больше максимального.")
            return
        output = Path(self.output.get())
        if not self.output.get().strip():
            messagebox.showerror("Результат", "Укажи, куда сохранить готовый PMTiles.")
            return
        try:
            temporary, disk_warning = self._preflight(output)
        except Exception as error:
            messagebox.showerror("Проверка перед запуском", str(error))
            return
        if disk_warning and not messagebox.askyesno("Мало свободного места", disk_warning):
            return
        if output.exists() and not messagebox.askyesno(
            "Файл уже существует",
            f"{output}\n\nЗаменить этот файл новой картой?",
        ):
            return
        if not self.attribution.get().strip():
            if not messagebox.askyesno(
                "Нет атрибуции",
                "Атрибуция источника не указана. Продолжить только если условия источника это допускают?",
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
                f"Запуск: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Режим: {job['mode']}\nИсточник: {self._selected_source()}\n"
                f"Результат: {output}\nВременная папка: {temporary}\n"
                f"Быстрый режим: {'да' if job['fast_mode'] else 'нет'}\n",
                encoding="utf-8",
            )
        except OSError as error:
            messagebox.showerror("Диагностический лог", f"Не удалось создать лог:\n{error}")
            return
        self.create_button.configure(state="disabled")
        self.started_at = time.monotonic()
        self.current_stage = "Подготовка"
        self.current_percent = 0
        self.progress_bar["value"] = 0
        self.status.set("Подготовка — 0%")
        self._append("—" * 54)
        self._append(f"Диагностический лог: {self.diagnostic_path}")
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
                self._worker_log(f"Границы выделения: {selection['bbox']}")
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
            self._worker_log("Конвертация и проверка завершены успешно.")
            self.queue.put(("done", (output, result)))
        except Exception as error:
            self._worker_log(f"ОШИБКА: {error}")
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
                "Карта исправна",
                f"PMTiles v{header['version']}\n"
                f"Масштабы: {header['min_zoom']}–{header['max_zoom']}\n"
                f"Тайлов: {header['addressed_tiles']}\n"
                f"Размер: {header['size'] / 1024 / 1024:.1f} МБ",
            )
        except Exception as error:
            messagebox.showerror("Проверка не пройдена", str(error))

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
                    elapsed = time.monotonic() - self.started_at if self.started_at else 0
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self.progress_bar["value"] = 100
                    self.status.set(f"Готово за {elapsed / 60:.1f} мин")
                    self.started_at = None
                    pm = result.get("pmtiles", result)
                    cache = result.get("cache")
                    missing = cache.get("missing", 0) if cache else 0
                    text = (
                        f"{output}\n\n"
                        f"Тайлов: {pm.get('addressed_tiles', '—')}\n"
                        f"Масштабы: {pm.get('min_zoom', '—')}–{pm.get('max_zoom', '—')}"
                    )
                    if missing:
                        messagebox.showwarning(
                            "Карта создана с пропусками",
                            text + f"\n\nНе найдено тайлов: {missing}. Повтори скачивание в SAS.Planet.",
                        )
                    else:
                        messagebox.showinfo("Карта готова", text)
                elif kind == "error":
                    self.create_button.configure(state="normal")
                    self.progress_bar.stop()
                    self.status.set("Ошибка")
                    self.started_at = None
                    messagebox.showerror("Не удалось создать карту", str(value))
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
