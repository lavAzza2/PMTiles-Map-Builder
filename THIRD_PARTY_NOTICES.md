# Third-party notices

THS2 Map Builder source code is distributed under the MIT License. The
prebuilt Windows package also contains the following third-party components.

## PMTiles CLI

The package includes the official `pmtiles` command-line utility from
[protomaps/go-pmtiles](https://github.com/protomaps/go-pmtiles), distributed
under the BSD 3-Clause License. Its source and license are available in that
repository.

## Python

The standalone package contains the Python runtime. Python is distributed
under the Python Software Foundation License. See
[docs.python.org/3/license.html](https://docs.python.org/3/license.html).

## Tcl/Tk

The graphical interface uses Tcl/Tk as shipped with Python. Tcl/Tk is
distributed under its BSD-style license. See
[tcl.tk/software/tcltk/license.html](https://www.tcl.tk/software/tcltk/license.html).

## PyInstaller

The Windows package is produced with PyInstaller. Its bootloader exception
permits distribution of the resulting executable under the application's
license. See [pyinstaller.org](https://pyinstaller.org/).

## GDAL (optional, not included)

GeoTIFF and KMZ conversion can use an independently installed GDAL/OSGeo4W or
QGIS distribution. GDAL is not included in the compact Windows package. GDAL
is distributed under an MIT-style license; individual format drivers and data
packages may have additional notices. See
[gdal.org](https://gdal.org/en/stable/license.html) and the notices supplied by
the installed distribution.
