"""Generate PyInstaller ``--version-file`` for Windows EXE properties."""

from __future__ import annotations

from pathlib import Path

from sayink.version import __version__, file_version_tuple


def write_version_file(dest: Path) -> Path:
    """Write UTF-8 version resource file; return *dest*."""
    a, b, c, d = file_version_tuple()
    text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, {d}),
    prodvers=({a}, {b}, {c}, {d}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
    ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'SayInk'),
        StringStruct(u'FileDescription', u'SayInk'),
        StringStruct(u'FileVersion', u'{__version__}'),
        StringStruct(u'InternalName', u'SayInk'),
        StringStruct(u'LegalCopyright', u'SayInk'),
        StringStruct(u'OriginalFilename', u'SayInk.exe'),
        StringStruct(u'ProductName', u'SayInk'),
        StringStruct(u'ProductVersion', u'{__version__}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest
