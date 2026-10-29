# -*- coding: utf-8 -*-
"""Turn a filled .xlsm into a PDF. Engines are tried in order; the first that works wins.

1. Microsoft Excel through pywin32     (original behaviour, pixel-exact)
2. Microsoft Excel through PowerShell  (no Python package needed -> immune to pywin32/Python mismatches)
3. LibreOffice (headless)              (works without Excel; uses a coordinate-rebased copy, see template.py)

All engines work on ASCII-named files in a private temp folder, so Arabic paths can never break
COM/PowerShell/LibreOffice command lines.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("facturedz.export")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class EngineError(Exception):
    pass


def looks_like_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return path.stat().st_size > 1500 and fh.read(5) == b"%PDF-"
    except OSError:
        return False


class Engine:
    name = "?"
    needs_rebased_template = False
    timeout = 120

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def convert(self, xlsm: Path, pdf: Path) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class ExcelComEngine(Engine):
    name = "Microsoft Excel (pywin32)"

    def available(self) -> bool:
        return os.name == "nt" and importlib.util.find_spec("win32com") is not None

    def convert(self, xlsm: Path, pdf: Path) -> None:
        error: List[BaseException] = []

        def work():
            import pythoncom                      # type: ignore
            import win32com.client                # type: ignore
            pythoncom.CoInitialize()
            excel = workbook = None
            try:
                excel = win32com.client.DispatchEx("Excel.Application")
                excel.Visible = False
                excel.DisplayAlerts = False
                try:
                    excel.AutomationSecurity = 3  # msoAutomationSecurityForceDisable: never run macros
                except Exception:
                    pass
                workbook = excel.Workbooks.Open(str(xlsm), 0, True)   # UpdateLinks=0, ReadOnly=True
                workbook.Worksheets(1).ExportAsFixedFormat(0, str(pdf))  # 0 = xlTypePDF
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                error.append(exc)
            finally:
                try:
                    if workbook is not None:
                        workbook.Close(False)
                except Exception:
                    pass
                try:
                    if excel is not None:
                        excel.Quit()
                except Exception:
                    pass
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            raise EngineError("انتهت مهلة Excel (لم يستجب).")
        if error:
            raise EngineError(f"{type(error[0]).__name__}: {error[0]}")


_PS_SCRIPT = r"""param([Parameter(Mandatory=$true)][string]$InFile,[Parameter(Mandatory=$true)][string]$OutFile)
$ErrorActionPreference = 'Stop'
$excel = $null
$wb = $null
try {
  $excel = New-Object -ComObject Excel.Application
  $excel.Visible = $false
  $excel.DisplayAlerts = $false
  try { $excel.AutomationSecurity = 3 } catch {}
  $wb = $excel.Workbooks.Open($InFile, 0, $true)
  $ws = $wb.Worksheets.Item(1)
  $ws.ExportAsFixedFormat(0, $OutFile)
} finally {
  if ($wb -ne $null) { try { $wb.Close($false) } catch {} }
  if ($excel -ne $null) {
    try { $excel.Quit() } catch {}
    try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) } catch {}
  }
}
"""


class ExcelPowerShellEngine(Engine):
    name = "Microsoft Excel (PowerShell)"

    def available(self) -> bool:
        return os.name == "nt" and shutil.which("powershell") is not None

    def convert(self, xlsm: Path, pdf: Path) -> None:
        script = xlsm.parent / "export_excel.ps1"
        script.write_text(_PS_SCRIPT, encoding="ascii")
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(script), "-InFile", str(xlsm), "-OutFile", str(pdf)]
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=self.timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise EngineError("انتهت مهلة Excel (PowerShell).") from exc
        except OSError as exc:
            raise EngineError(str(exc)) from exc
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()[-400:]
            raise EngineError(f"PowerShell exit {done.returncode}: {tail}")


def find_soffice() -> Optional[str]:
    env = os.environ.get("FACTUREDZ_SOFFICE")
    if env and Path(env).is_file():
        return env
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")):
            cand = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if cand.is_file():
                return str(cand)
    return None


class LibreOfficeEngine(Engine):
    name = "LibreOffice"
    needs_rebased_template = True
    timeout = 180

    def __init__(self, runner: Optional[Callable[[List[str]], "subprocess.CompletedProcess"]] = None):
        self._runner = runner      # injectable for sandboxes/tests

    def available(self) -> bool:
        return self._runner is not None or find_soffice() is not None

    def convert(self, xlsm: Path, pdf: Path) -> None:
        outdir = xlsm.parent
        profile = outdir / "lo_profile"
        args = ["--headless", "--norestore", "--nolockcheck", "--nodefault",
                f"-env:UserInstallation={profile.resolve().as_uri()}",
                "--convert-to", "pdf", "--outdir", str(outdir), str(xlsm)]
        try:
            if self._runner:
                done = self._runner(args)
            else:
                done = subprocess.run([find_soffice()] + args, capture_output=True, timeout=self.timeout,
                                      creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise EngineError("انتهت مهلة LibreOffice.") from exc
        except OSError as exc:
            raise EngineError(str(exc)) from exc
        produced = outdir / (xlsm.stem + ".pdf")
        if produced.exists() and produced != pdf:
            os.replace(produced, pdf)
        if done.returncode != 0 and not pdf.exists():
            raise EngineError(f"LibreOffice exit {done.returncode}")


def default_engines() -> List[Engine]:
    return [ExcelComEngine(), ExcelPowerShellEngine(), LibreOfficeEngine()]


def convert_to_pdf(make_xlsm: Callable[[Path, bool], Path], engines: List[Engine], workdir: Path,
                   progress: Optional[Callable[[str], None]] = None):
    """Try each available engine. ``make_xlsm(path, for_libreoffice)`` writes the filled workbook.

    Returns (pdf_path | None, engine_name | None, [error strings]).
    """
    errors: List[str] = []
    usable = [e for e in engines if e.available()]
    if not usable:
        errors.append("لا يوجد Microsoft Excel ولا LibreOffice على هذا الجهاز.")
    for engine in usable:
        if progress:
            progress(f"إنشاء PDF عبر {engine.name}…")
        stem = "invoice_lo" if engine.needs_rebased_template else "invoice_x"
        xlsm = make_xlsm(workdir / f"{stem}.xlsm", engine.needs_rebased_template)
        pdf = workdir / f"{stem}.pdf"
        try:
            pdf.unlink(missing_ok=True)
            engine.convert(xlsm, pdf)
            if looks_like_pdf(pdf):
                return pdf, engine.name, errors
            errors.append(f"{engine.name}: لم ينتج ملف PDF صالحًا.")
        except Exception as exc:  # noqa: BLE001
            log.warning("engine %s failed: %s", engine.name, exc)
            errors.append(f"{engine.name}: {exc}")
    return None, None, errors


def make_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="fdz_"))
