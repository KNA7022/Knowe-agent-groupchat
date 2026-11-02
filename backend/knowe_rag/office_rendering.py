"""Optional bounded native Office export. No cloud/OCR, no user path input.

Only macro-free OOXML packages without external relationships/active objects are
eligible. Automation uses an isolated, hidden Office instance; an existing Office
process causes an explicit unavailable status rather than touching user windows.
Transient input/PDF files are deleted after reading the bounded result.
"""
from __future__ import annotations
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import zipfile
from xml.etree import ElementTree

MAX_PDF_BYTES = 40 * 1024 * 1024
OFFICE_SUFFIXES = {".docx": "WINWORD", ".pptx": "POWERPNT", ".xlsx": "EXCEL"}


class OfficeRenderingUnavailable(ValueError):
    pass


def office_rendering_available(suffix):
    if os.name != "nt" or suffix not in OFFICE_SUFFIXES:
        return False
    roots = [Path(os.environ.get("ProgramFiles", "C:/Program Files")), Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))]
    return any((root / "Microsoft Office/root/Office16" / (OFFICE_SUFFIXES[suffix] + ".EXE")).exists() for root in roots)


def _validate_package(raw, *, depth=0):
    if depth > 2 or len(raw) > 20 * 1024 * 1024:
        raise OfficeRenderingUnavailable("office_package_limit")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            for item in package.infolist():
                name = item.filename.casefold()
                if any(part in name for part in ("vbaproject", "activex/", "externallinks/")):
                    raise OfficeRenderingUnavailable("active_office_content_unsupported")
                if "embeddings/" in name:
                    if not name.endswith(".xlsx") or item.file_size > 16 * 1024 * 1024:
                        raise OfficeRenderingUnavailable("active_office_content_unsupported")
                    _validate_package(package.read(item), depth=depth + 1)
                if name.endswith(".rels"):
                    if item.file_size > 2_000_000:
                        raise OfficeRenderingUnavailable("office_relationship_limit")
                    for relationship in ElementTree.fromstring(package.read(item)).iter():
                        if relationship.get("TargetMode", "").casefold() == "external":
                            raise OfficeRenderingUnavailable("external_office_relationship_unsupported")
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise OfficeRenderingUnavailable("invalid_office_package") from exc


_SCRIPT = r"""
param([string]$InputFile,[string]$OutputFile,[string]$Suffix,[string]$PidFile)
$ErrorActionPreference = 'Stop'
$names = @{'.docx'='WINWORD';'.pptx'='POWERPNT';'.xlsx'='EXCEL'}
$app = $null; $document = $null; $ownedPid = $null
try {
    if (Get-Process -Name $names[$Suffix] -ErrorAction SilentlyContinue) { exit 3 }
    $prog = @{'.docx'='Word.Application';'.pptx'='PowerPoint.Application';'.xlsx'='Excel.Application'}[$Suffix]
    $app = New-Object -ComObject $prog
    $created = @(Get-Process -Name $names[$Suffix] -ErrorAction SilentlyContinue)
    if ($created.Count -ne 1) { exit 4 }
    $ownedPid = $created[0].Id
    Set-Content -LiteralPath $PidFile -Value $ownedPid -Encoding UTF8
    $app.AutomationSecurity = 3
    if ($Suffix -eq '.docx') {
        $app.Visible = $false; $app.DisplayAlerts = 0
        $document = $app.Documents.Open($InputFile,$false,$true,$false)
        $document.ExportAsFixedFormat($OutputFile,17)
    } elseif ($Suffix -eq '.xlsx') {
        $app.Visible = $false; $app.DisplayAlerts = $false; $app.AskToUpdateLinks = $false
        $document = $app.Workbooks.Open($InputFile,0,$true)
        $document.ExportAsFixedFormat(0,$OutputFile)
    } else {
        $app.DisplayAlerts = 1
        $document = $app.Presentations.Open($InputFile,-1,0,0)
        $document.SaveAs($OutputFile,32)
    }
    if (!(Test-Path -LiteralPath $OutputFile)) { exit 5 }
} catch { exit 2 }
finally {
    if ($document) { try { if ($Suffix -eq '.pptx') { $document.Close() } else { $document.Close($false) } } catch {} }
    if ($app -and $ownedPid) { try { $app.Quit() } catch {} }
    if ($document) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) }
    if ($app) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app) }
}
"""


def convert_office_to_pdf(raw: bytes, suffix: str, *, timeout=45) -> bytes:
    if not office_rendering_available(suffix):
        raise OfficeRenderingUnavailable("office_renderer_not_installed")
    _validate_package(raw)
    shell = shutil.which("pwsh") or str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    with tempfile.TemporaryDirectory(prefix="studycrew-rag-office-") as directory:
        root = Path(directory).resolve()
        # Checked absolute cleanup target: exactly the temporary directory allocated
        # for this call, underneath the system temporary root.
        if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("studycrew-rag-office-"):
            raise OfficeRenderingUnavailable("invalid_temporary_root")
        source, output, script, pidfile = root / ("input" + suffix), root / "output.pdf", root / "convert.ps1", root / "office.pid"
        source.write_bytes(raw)
        script.write_text(_SCRIPT, encoding="utf-8-sig")
        try:
            result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-InputFile", str(source), "-OutputFile", str(output), "-Suffix", suffix, "-PidFile", str(pidfile)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            if result.returncode != 0 or not output.exists():
                raise OfficeRenderingUnavailable("office_renderer_busy_or_failed")
            if not 0 < output.stat().st_size <= MAX_PDF_BYTES:
                raise OfficeRenderingUnavailable("office_pdf_output_limit")
            pdf = output.read_bytes()
            if not pdf.startswith(b"%PDF-"):
                raise OfficeRenderingUnavailable("invalid_office_pdf_output")
            return pdf
        except subprocess.TimeoutExpired as exc:
            raise OfficeRenderingUnavailable("office_renderer_timeout") from exc
        finally:
            # The script writes only an instance it started after checking no Office
            # process existed. Never terminate a user's pre-existing application.
            if pidfile.exists():
                try:
                    owned_pid = int(pidfile.read_text(encoding="utf-8-sig").strip())
                    os.kill(owned_pid, signal.SIGTERM)
                except (ValueError, OSError):
                    pass
