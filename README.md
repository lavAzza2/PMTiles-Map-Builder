# PMTiles Map Builder for THS2

[![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D4)](https://github.com/lavAzza2/PMTiles-Map-Builder/releases)
[![PMTiles](https://img.shields.io/badge/PMTiles-v3-5B4BDB)](https://github.com/protomaps/PMTiles)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[Русский](#русский) · [English](#english)

## Русский

Простое Windows-приложение для подготовки растровых офлайн-карт для
Android-приложения THS2. Помощник принимает кэш SAS.Planet, MBTiles,
геопривязанные GeoTIFF/KMZ и папки тайлов XYZ, приводит источник к единому
MBTiles, преобразует его в PMTiles v3 и проверяет готовый архив официальным
PMTiles CLI.

Приложение работает **рядом с обычной SAS.Planet**. Это не форк и не плагин:
оно не заменяет SAS.Planet, не вмешивается в скачивание и не изменяет её кэш.

## Возможности

- современная тёмная тема и отдельные вкладки для каждого типа источника;
- русский и английский интерфейс с автовыбором по языку Windows и ручным выбором в настройках;
- автоматическое восстановление выбранной вкладки, путей, масштабов и остальных рабочих параметров;
- создание PMTiles напрямую из SQLite-кэша SAS.Planet CacheType=71;
- автоматическое чтение последнего выделения из `LastSelection.hlg`;
- выбор диапазона масштабов THS2;
- запасной путь через экспортированный SAS.Planet файл `.mbtiles`;
- конвертация геопривязанных `.tif`, `.tiff` и `.kmz` через GDAL;
- сборка папок тайлов со структурой `z/x/y.png` или `z/x/y.jpg`;
- поддержка растровых PNG, JPEG, WebP и AVIF;
- запись названия карты и обязательной атрибуции источника;
- проверка результата командой `pmtiles verify`;
- отчёт `.report.json` рядом с готовой картой;
- живой прогресс текущего этапа и прошедшее время;
- диагностический `.diagnostic.log` рядом с результатом;
- проверка свободного места и выбор временной папки;
- автоматическое индексирование рабочей копии больших MBTiles;
- быстрый режим без дедупликации для тяжёлых растров;
- предупреждение о недостающих тайлах;
- строгое чтение исходного кэша SQLite в режиме `mode=ro`.

## Скачать готовую версию

Открой раздел [Releases](https://github.com/lavAzza2/PMTiles-Map-Builder/releases),
скачай архив `THS2-Map-Builder-Windows-x64.zip` и распакуй его в отдельную
папку. Запускай `THS2 Map Builder.exe`.

Не вынимай EXE из распакованной папки: рядом находится каталог `_internal` с
Python, Tcl/Tk и официальным `pmtiles.exe`. Установка Python не требуется.

## Основной сценарий: напрямую из кэша

1. Запусти SAS.Planet и выбери источник карты.
2. Выдели прямоугольник или многоугольник.
3. Через операцию скачивания загрузи все нужные масштабы.
4. Закрой окно операции скачивания и открой THS2 Map Builder.
5. Выбери папку конкретного источника внутри `cache_sqlite`.
6. Укажи `LastSelection.hlg` из корня SAS.Planet.
7. Задай стандартные масштабы PMTiles, например `13–19`.
8. Укажи название, атрибуцию и выходной файл.
9. Нажми **Создать карту THS2**.

SAS.Planet исторически обозначает масштабы на единицу выше стандартной XYZ
схемы: стандартный zoom `13–19` хранится в каталогах SAS `z14`–`z20`.
Помощник учитывает это автоматически.

Для сложного многоугольника MVP использует его прямоугольную оболочку. Это не
теряет пограничные тайлы, но может добавить тайлы за пределами контура.

## Запасной сценарий: MBTiles

Если используется другая версия или другой тип кэша SAS.Planet:

1. Экспортируй выбранную область из SAS.Planet в `.mbtiles` со схемой TMS.
2. В помощнике выбери режим **Экспортированный MBTiles**.
3. Выбери файл и нажми **Создать карту THS2**.

Исходный MBTiles не изменяется. Для добавления названия и атрибуции помощник
работает с временной копией.

## GeoTIFF и KMZ

Выбери режим **GeoTIFF или KMZ — геопривязанный растр**, исходный файл,
масштабы и формат тайлов. PNG сохраняет прозрачность и чёткую графику, JPEG
обычно лучше подходит спутниковым снимкам и занимает меньше места.

Приложение проверяет систему координат, переводит растр в Web Mercator,
нарезает тайлы, создаёт обзорные масштабы и проверяет PMTiles. Обычный JPG/PNG
без геопривязки пока преобразовать нельзя.

Для этого режима требуется GDAL. Приложение автоматически ищет его в `PATH`,
OSGeo4W, QGIS, каталоге из `GDAL_HOME` и в `bin\gdal` рядом с приложением.
Остальные режимы работают без GDAL.

## Папка XYZ

Поддерживаются обычная структура `z/x/y.ext` и вариант с буквенными
префиксами `zZ/xX/yY.ext`, например:

```text
tiles/13/5424/2568.jpg
tiles/z13/x5424/y2568.jpg
```

Масштабы и границы вычисляются автоматически. Координата `y` читается как XYZ
и переводится в TMS для временного MBTiles. Исходные файлы не меняются. Все
тайлы карты должны иметь одинаковый формат: PNG, JPEG, WebP или AVIF.

Перед упаковкой приложение подсчитывает тайлы и их общий размер. Во время
поиска показывается живой счётчик найденных файлов, а во время сборки — точный
процент, `обработано / всего`, скорость и примерное оставшееся время. После
подсчёта дополнительно проверяется свободное место для временного MBTiles и
готового PMTiles.

## Большие карты

Для исходников размером около 1 ГБ и больше открой раздел **Большие карты**:

1. Выбери временную папку на диске с достаточным запасом места. Если поле
   пустое, используется системная `%TEMP%`, обычно на диске `C:`.
2. Для спутниковых и других растров включи **Быстрый режим**. Он передаёт
   PMTiles CLI параметр `--no-deduplication`: обработка обычно быстрее, но
   итоговый файл может быть немного больше.
3. Следи за названием текущего этапа, процентом и временем. При создании
   индекса точный процент недоступен, поэтому индикатор движется без числа.

Перед запуском приложение оценивает свободное место отдельно для временных
файлов и результата. Исходный MBTiles не изменяется: при необходимости индекс
`zoom_level, tile_column, tile_row` создаётся только в рабочей копии.

Рядом с результатом всегда появляется файл `.diagnostic.log`. Если операция
завершилась ошибкой или кажется слишком долгой, пришли этот файл разработчику.
Он содержит этапы и сообщения инструментов, но не содержит сами тайлы.

## Безопасность и приватность

- кэш SAS.Planet открывается только на чтение;
- приложение не скачивает тайлы из интернета;
- приложение не отправляет карты или координаты на сервер;
- перед заменой существующего результата требуется подтверждение;
- временный MBTiles удаляется после завершения;
- рядом с результатом сохраняется понятный JSON-отчёт о сборке.

## Права на карты и атрибуция

SAS.Planet является инструментом доступа к разным картографическим источникам,
но не передаёт права на их данные. Перед созданием и распространением офлайн-
карты проверь условия выбранного источника: разрешены ли скачивание, локальное
хранение, публикация и коммерческое использование. Укажи требуемую атрибуцию —
она будет записана в метаданные PMTiles.

## Формат кэша CacheType=71

Проверенная схема базы:

```sql
CREATE TABLE t (
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    v INTEGER DEFAULT 0 NOT NULL,
    c TEXT,
    s INTEGER DEFAULT 0 NOT NULL,
    h INTEGER DEFAULT 0 NOT NULL,
    d INTEGER NOT NULL,
    b BLOB,
    PRIMARY KEY (x, y, v)
);
```

`x/y` — глобальные XYZ-координаты, `v=0` — основная версия тайла, `b` —
изображение. Каталоги группируют координаты по блокам 1024 и 256 тайлов.
MBTiles хранит строки в TMS, поэтому помощник выполняет преобразование
`tms_y = 2^zoom - 1 - xyz_y`.

Подробное обоснование решения находится в
[ADR-0001](docs/ADR-0001-ths2-map-builder.md). Архитектура универсального
импорта описана в [ADR-0002](docs/ADR-0002-multiformat-import.md), обработка
больших карт — в [ADR-0003](docs/ADR-0003-large-map-processing.md).

## Запуск из исходников

Требования: Windows 10/11 и Python 3.10 или новее. Для GeoTIFF/KMZ дополнительно
нужен GDAL из OSGeo4W или QGIS.

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-PmTiles.ps1
python .\ths2_map_builder.pyw
```

Также можно дважды щёлкнуть `Start-THS2-Map-Builder.cmd`.

## Сборка Windows-пакета

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-PmTiles.ps1
powershell -ExecutionPolicy Bypass -File .\Build-Windows-App.ps1
```

Результат появится в `dist\THS2 Map Builder`. Скрипт использует PyInstaller и
включает официальный PMTiles CLI в автономный пакет.

## Тесты

```powershell
python -m unittest -v test_map_builder.py
```

Прототип проверен на SAS.Planet 260404 x64 и реальном наборе из 4383 JPEG-
тайлов zoom 13–19. Прямая сборка из кэша и конвертация исходного MBTiles дали
одинаковое число тайлов; оба результата прошли `pmtiles verify`.

## Ограничения MVP

- прямое чтение реализовано только для CacheType=71;
- выделения через антимеридиан пока не поддерживаются;
- помощник не докачивает отсутствующие тайлы;
- обычные JPG/PNG без геопривязки пока не поддерживаются;
- векторные SHP, GeoJSON и KML пока не отрисовываются в растр;
- компактная Windows-сборка не включает GDAL и использует установленный
  OSGeo4W/QGIS;
- загрузка в аккаунт THS2.ru потребует отдельного серверного API загрузки;
- автоматическое наблюдение за каталогом экспорта MBTiles пока не включено.

## Лицензия

Исходный код распространяется по лицензии [MIT](LICENSE). Компоненты готовой
Windows-сборки перечислены в [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

## English

THS2 Map Builder is a Windows desktop application for creating verified raster
PMTiles v3 offline maps for the [THS2 Android app](https://github.com/lavAzza2/THS2).
It converts SAS.Planet cache data, MBTiles archives, XYZ tile directories, and
georeferenced GeoTIFF/KMZ rasters into a format that THS2 can open directly.

The application runs **next to an unmodified SAS.Planet installation**. It is
not a SAS.Planet fork or plugin, does not interfere with tile downloads, and
never modifies the original SAS.Planet cache or XYZ source directory.

### Highlights

- modern dark Windows interface;
- separate tabs for SAS.Planet, MBTiles, XYZ, and GeoTIFF/KMZ workflows;
- Russian and English UI with automatic Windows-language detection;
- language override under **Settings** without restarting the application;
- automatic restoration of the last tab, paths, zoom levels, and conversion options;
- direct read-only access to SAS.Planet SQLite CacheType=71;
- raster PNG, JPEG, WebP, and AVIF tile support;
- live conversion progress, elapsed time, XYZ tile count, speed, and ETA;
- free-space preflight checks and a selectable temporary directory;
- fast mode for very large maps using PMTiles `--no-deduplication`;
- PMTiles v3 verification with the official PMTiles CLI;
- `.report.json` and `.diagnostic.log` files next to the output map.

### Download and install

1. Open the [latest release](https://github.com/lavAzza2/PMTiles-Map-Builder/releases/latest).
2. Download `THS2-Map-Builder-Windows-x64-vX.Y.Z.zip`.
3. Extract the complete archive to a folder.
4. Run `THS2 Map Builder.exe`.

Do not move only the EXE out of the extracted folder. The adjacent `_internal`
directory contains Python, Tcl/Tk, and the official `pmtiles.exe`. Python does
not need to be installed for the packaged application.

### Recommended SAS.Planet workflow

1. Select a map source in SAS.Planet.
2. Select a rectangle or polygon.
3. Download all required zoom levels.
4. Open THS2 Map Builder and select the **SAS.Planet** tab.
5. Choose the source folder inside `cache_sqlite` and the `LastSelection.hlg` file.
6. Enter standard XYZ/PMTiles zoom levels. SAS.Planet historically displays
   these levels one number higher.
7. Enter a map name, source attribution, and output file.
8. Select a temporary folder with sufficient free space for a large map.
9. Click **Create THS2 map**.

The source `.sqlitedb` files are opened through SQLite `mode=ro`. The converter
creates all intermediate databases in a separate temporary directory.

### Supported inputs

#### SAS.Planet CacheType=71

Direct conversion reads the fragmented SQLite cache selected by
`LastSelection.hlg`. Missing tiles are reported after the map is created.

#### MBTiles

Existing raster MBTiles files can be converted directly. If a large source
lacks the required `zoom_level, tile_column, tile_row` index, the application
creates the index only in a temporary working copy and leaves the source file
unchanged.

#### XYZ tile directories

Both common naming layouts are supported:

```text
tiles/13/5424/2568.jpg
tiles/z13/x5424/y2568.jpg
```

Zoom levels and bounds are detected automatically. XYZ Y coordinates are
converted to TMS row numbers for the temporary MBTiles database. All tiles in
one map must use the same image format.

#### GeoTIFF and KMZ

Georeferenced `.tif`, `.tiff`, and `.kmz` rasters can be reprojected to Web
Mercator and tiled as PNG or JPEG. This mode requires GDAL from OSGeo4W or QGIS.
Plain JPG/PNG images without georeferencing are not supported.

### Large maps

For sources around 1 GB or larger:

- choose a temporary directory on a drive with several gigabytes of free space;
- enable **Fast mode** for satellite imagery when a slightly larger PMTiles file
  is acceptable;
- follow the displayed stage, percentage, elapsed time, and ETA;
- if conversion fails or appears stalled, share the generated
  `.diagnostic.log` file. It contains tool output, but not the tile images.

### Language and saved settings

On first launch, Russian Windows installations use Russian; all other system
languages use English. Use the **Settings** button in the upper-right corner to
change the language manually.

The application automatically saves the selected converter tab, source paths,
output path, map name, attribution, zoom levels, raster settings, temporary
directory, fast mode, and language. Preferences are stored in:

```text
%APPDATA%\THS2 Map Builder\settings.json
```

### Running from source

Requirements: Windows 10/11 and Python 3.10 or newer. GeoTIFF/KMZ conversion
also requires GDAL.

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-PmTiles.ps1
python .\ths2_map_builder.pyw
```

### Building the Windows package

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-PmTiles.ps1
powershell -ExecutionPolicy Bypass -File .\Build-Windows-App.ps1
```

The standalone package is written to `dist\THS2 Map Builder`.

### Tests

```powershell
python -m unittest -v test_map_builder.py
```

### Current limitations

- direct SAS.Planet access currently supports CacheType=71 only;
- selections crossing the antimeridian are not supported;
- the application does not download missing tiles;
- vector SHP, GeoJSON, and KML data is not rendered to raster tiles;
- the compact Windows package does not bundle GDAL;
- automatic monitoring of an MBTiles export folder is not implemented.

### Attribution and map-source rights

Only convert and distribute map data when the source license permits it. Enter
the required copyright notice and usage terms in the **Attribution** field; the
text is embedded in PMTiles metadata.

### License

Source code is available under the [MIT License](LICENSE). Third-party
components included in the Windows package are documented in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
