"""Convert Word documents to PDF via LibreOffice or Microsoft Word.

Backends (tried in order):
1. **LibreOffice** ``soffice --headless --convert-to pdf`` — preferred for
   servers / Docker; supports ``.docx`` and ``.doc``.
2. **Microsoft Word COM** (Windows only) — used when LibreOffice is not
   installed but Word is available (via ``docx2pdf`` or direct COM).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

# Common LibreOffice install locations on Windows.
_WIN_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)

SUPPORTED_EXTENSIONS = {".docx", ".doc"}

# Headless conversion can be slow for large files.
DEFAULT_TIMEOUT_SEC = 180


class ConversionError(Exception):
    """Raised when Word → PDF conversion fails or no engine is available."""


def _which_soffice() -> Optional[str]:
    """Locate the LibreOffice ``soffice`` binary.

    Order: ``LIBREOFFICE_PATH`` / ``SOFFICE_PATH`` env → PATH → common
    Windows install dirs → common Linux container paths.
    """
    env = os.environ.get("LIBREOFFICE_PATH") or os.environ.get("SOFFICE_PATH")
    if env:
        # Accept either a file or a directory that contains soffice.
        if os.path.isfile(env):
            return env
        for name in ("soffice", "soffice.exe", "libreoffice"):
            candidate = os.path.join(env, name)
            if os.path.isfile(candidate):
                return candidate

    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found

    if platform.system() == "Windows":
        for path in _WIN_SOFFICE_CANDIDATES:
            if os.path.isfile(path):
                return path
    else:
        for path in (
            "/usr/bin/soffice",
            "/usr/bin/libreoffice",
            "/usr/lib/libreoffice/program/soffice",
        ):
            if os.path.isfile(path):
                return path
    return None


def _word_com_available() -> bool:
    """Return True if Microsoft Word can be driven on this machine."""
    if platform.system() != "Windows":
        return False
    try:
        import win32com.client  # type: ignore  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import docx2pdf  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def available_engines() -> List[str]:
    """Return names of conversion backends currently usable."""
    engines: List[str] = []
    if _which_soffice():
        engines.append("libreoffice")
    if _word_com_available():
        engines.append("msword")
    return engines


def engine_info() -> dict:
    """Diagnostic info for UI / health checks."""
    soffice = _which_soffice()
    engines = available_engines()
    return {
        "engines": engines,
        "preferred": engines[0] if engines else None,
        "libreoffice_path": soffice,
        "ready": bool(engines),
    }


def _validate_input(input_path: str) -> Path:
    path = Path(input_path)
    if not path.is_file():
        raise ConversionError(f"File not found: {input_path}")
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ConversionError(
            f"Unsupported format '{ext}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    if path.stat().st_size == 0:
        raise ConversionError("Empty file")
    return path


def _convert_libreoffice(
    input_path: Path,
    output_pdf: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> None:
    soffice = _which_soffice()
    if not soffice:
        raise ConversionError("LibreOffice not found")

    out_dir = output_pdf.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # LibreOffice writes <stem>.pdf into --outdir; we may rename afterwards.
    # Use a private user profile so concurrent conversions don't clash.
    with tempfile.TemporaryDirectory(prefix="lo_profile_") as profile:
        profile_uri = Path(profile).resolve().as_uri()
        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(out_dir),
            str(input_path.resolve()),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(
                f"LibreOffice timed out after {timeout}s"
            ) from exc
        except OSError as exc:
            raise ConversionError(f"Failed to launch LibreOffice: {exc}") from exc

        produced = out_dir / (input_path.stem + ".pdf")
        if proc.returncode != 0 and not produced.is_file():
            err = (proc.stderr or proc.stdout or "").strip()
            raise ConversionError(
                f"LibreOffice conversion failed (code {proc.returncode})"
                + (f": {err[:400]}" if err else "")
            )

        if not produced.is_file():
            raise ConversionError(
                "LibreOffice finished but PDF was not created"
            )

        if produced.resolve() != output_pdf.resolve():
            if output_pdf.exists():
                output_pdf.unlink()
            produced.replace(output_pdf)


def _convert_msword(input_path: Path, output_pdf: Path) -> None:
    """Convert via Microsoft Word (Windows). Prefers docx2pdf, else COM."""
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    src = str(input_path.resolve())
    dst = str(output_pdf.resolve())

    # docx2pdf is a thin wrapper around Word COM.
    try:
        from docx2pdf import convert as d2p_convert  # type: ignore
    except ImportError:
        d2p_convert = None  # type: ignore

    if d2p_convert is not None:
        try:
            d2p_convert(src, dst)
            if output_pdf.is_file() and output_pdf.stat().st_size > 0:
                return
            raise ConversionError("docx2pdf finished but PDF is missing/empty")
        except ConversionError:
            raise
        except Exception:
            # Fall through to raw COM.
            pass

    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError as exc:
        raise ConversionError(
            "Microsoft Word backend requires pywin32 or docx2pdf "
            "(and a local Microsoft Word install)"
        ) from exc

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(src, ReadOnly=True)
        # 17 = wdFormatPDF
        doc.SaveAs(dst, FileFormat=17)
    except Exception as exc:
        raise ConversionError(f"Microsoft Word conversion failed: {exc}") from exc
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    if not output_pdf.is_file() or output_pdf.stat().st_size == 0:
        raise ConversionError("Microsoft Word finished but PDF is missing/empty")

def convert_to_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    *,
    engine: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> Tuple[str, str]:
    """Convert a Word document to PDF.

    Parameters
    ----------
    input_path:
        Path to ``.docx`` or ``.doc``.
    output_path:
        Destination ``.pdf``. Defaults to ``<input_stem>.pdf`` next to the input.
    engine:
        Force ``"libreoffice"`` or ``"msword"``. Default: first available.
    timeout:
        LibreOffice subprocess timeout in seconds.

    Returns
    -------
    (pdf_path, engine_used)
    """
    src = _validate_input(input_path)
    if output_path:
        dst = Path(output_path)
        if dst.suffix.lower() != ".pdf":
            dst = dst.with_suffix(".pdf")
    else:
        dst = src.with_suffix(".pdf")

    engines = available_engines()
    if engine:
        chosen = engine.lower().strip()
        if chosen not in ("libreoffice", "msword"):
            raise ConversionError(
                f"Unknown engine '{engine}'. Use 'libreoffice' or 'msword'."
            )
        if chosen not in engines:
            raise ConversionError(
                f"Engine '{chosen}' is not available on this machine. "
                f"Available: {', '.join(engines) or 'none'}"
            )
        order = [chosen]
    else:
        # Prefer LibreOffice (headless-friendly), then Word COM.
        order = [e for e in ("libreoffice", "msword") if e in engines]

    if not order:
        raise ConversionError(
            "No conversion engine available. Install LibreOffice "
            "(recommended for servers) or Microsoft Word (Windows). "
            "You can set LIBREOFFICE_PATH to the soffice binary."
        )

    last_err: Optional[Exception] = None
    for name in order:
        try:
            if name == "libreoffice":
                _convert_libreoffice(src, dst, timeout=timeout)
            else:
                _convert_msword(src, dst)
            return str(dst), name
        except ConversionError as exc:
            last_err = exc
            continue

    raise ConversionError(str(last_err) if last_err else "Conversion failed")
