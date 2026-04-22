import subprocess
import os
import re
import shlex
import shutil
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from .config import config

logger = logging.getLogger(__name__)


def _ps_single_quoted(path: str) -> str:
    """Escape a path for use inside a PowerShell single-quoted (literal) string."""
    return path.replace("'", "''")


def _pdf_output_path(path_obj: Path) -> str:
    """Absolute path for the PDF next to the source file (same directory, same stem)."""
    return str(path_obj.parent.resolve() / f"{path_obj.stem}.pdf")


# Office + ODF + common text/web — LibreOffice converts these; Word/Excel COM used on
# Windows only for a subset.
CONVERT_TO_PDF_EXTENSIONS = frozenset(
    {
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".csv",
        ".ppt",
        ".pptx",
        ".ppsx",
        ".odt",
        ".ods",
        ".odp",
        ".odg",
        ".rtf",
        ".txt",
        ".html",
        ".htm",
    }
)


def _existing_sibling_pdf(path_obj: Path) -> Optional[str]:
    """
    If a usable PDF is already next to the source, return its path.
    If the file itself is a PDF, return that path.
    """
    if not path_obj.is_file() or path_obj.stat().st_size <= 0:
        return None
    if path_obj.suffix.lower() == ".pdf":
        return str(path_obj)
    out = _pdf_output_path(path_obj)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    return None


def sibling_pdf_if_any(file_path: str) -> Optional[str]:
    """Public: path to a PDF to print/convert, if the file is PDF or a sibling .pdf exists."""
    return _existing_sibling_pdf(Path(file_path).resolve())


def _find_soffice_executable() -> Optional[str]:
    """Resolve LibreOffice / OpenOffice headless converter if installed."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_key)
            if not root:
                continue
            candidate = Path(root) / "LibreOffice" / "program" / "soffice.exe"
            if candidate.is_file():
                return str(candidate)
    return None


class PrintManager:
    def __init__(self):
        self.max_copies = config.max_copies

    async def get_available_printers(self) -> List[str]:
        """Fetch available printers asynchronously."""
        try:
            if os.name == "nt":
                # Windows - Try PowerShell first (more reliable on modern Win10/11)
                ps_cmd = [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Printer | Select-Object -ExpandProperty Name",
                ]
                process = await asyncio.create_subprocess_exec(
                    *ps_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await process.communicate()
                output = stdout.decode().strip()
                if output:
                    return [p.strip() for p in output.split("\r\n") if p.strip()]

                # Fallback to wmic
                cmd = ["wmic", "printer", "get", "name"]
            else:
                # Linux - Using standard lpstat
                cmd = ["lpstat", "-e"]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"Failed to get printers: {stderr.decode()}")
                return []

            output = stdout.decode().strip()
            if os.name == "nt":
                return [p.strip() for p in output.split("\n")[1:] if p.strip()]
            else:
                return [p.strip() for p in output.split("\n") if p.strip()]
        except Exception as e:
            logger.error(f"Printer discovery error: {e}")
            return ["Default Printer"]

    async def get_print_queue(self) -> List[str]:
        """Fetch current print queue with structured IDs."""
        try:
            if os.name == "nt":
                # Windows - Get JobID and Document name
                cmd = ["wmic", "printjob", "get", "jobid,document"]
            else:
                # Linux - Get basic job info
                cmd = ["lpstat", "-o"]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            output = stdout.decode().strip()

            if not output:
                return []

            jobs = []
            lines = [l.strip() for l in output.split("\n") if l.strip()]

            if os.name == "nt":
                # Skip header 'Document  JobId'
                for line in lines[1:]:
                    # wmic output is often fixed-width or space-separated
                    parts = line.split()
                    if len(parts) >= 2:
                        job_id = parts[-1]
                        doc = " ".join(parts[:-1])
                        jobs.append(f"#{job_id}: {doc}")
            else:
                for line in lines:
                    # Linux format: 'printer-123 user size ...'
                    parts = line.split()
                    if parts:
                        job_id = parts[0]
                        jobs.append(f"{job_id}")

            return jobs
        except Exception as e:
            logger.error(f"Queue fetch error: {e}")
            return []

    async def cancel_print_job(self, job_id: str) -> bool:
        """Cancel a print job by ID."""
        try:
            if os.name == "nt":
                # Sanitize: job_id should be numeric
                numeric_id = "".join(filter(str.isdigit, job_id))
                if not numeric_id:
                    return False
                # Use wmic to delete the job
                cmd = ["wmic", "printjob", "where", f"jobid={numeric_id}", "delete"]
            else:
                # Linux - Use cancel command
                # job_id is usually 'printer-123'
                cmd = ["cancel", job_id]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

            if process.returncode == 0:
                logger.info(f"Cancelled job: {job_id}")
                return True
            else:
                logger.error(f"Failed to cancel job {job_id}: {stderr.decode()}")
                return False
        except asyncio.TimeoutError:
            logger.error(f"Timeout cancelling job {job_id}")
            return False
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            return False

    async def resume_print_job(self, job_id: str) -> bool:
        """Force resume a print job by ID (Windows/Linux)."""
        try:
            if os.name == "nt":
                numeric_id = "".join(filter(str.isdigit, job_id))
                if not numeric_id:
                    return False
                cmd = [
                    "wmic",
                    "printjob",
                    "where",
                    f"jobid={numeric_id}",
                    "call",
                    "resume",
                ]
            else:
                cmd = ["lp", "-i", job_id, "-H", "resume"]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

            if process.returncode == 0:
                logger.info(f"Resumed job: {job_id}")
                return True
            else:
                logger.error(f"Failed to resume job {job_id}: {stderr.decode()}")
                return False
        except asyncio.TimeoutError:
            logger.error(f"Timeout resuming job {job_id}")
            return False
        except Exception as e:
            logger.error(f"Resume error: {e}")
            return False

    async def _convert_via_office_com(
        self, path_obj: Path, output_pdf: str, ext: str
    ) -> Tuple[Optional[str], str]:
        """Windows-only Word/Excel COM export. Returns (pdf_path, failure_detail)."""
        if os.name != "nt":
            return None, ""
        try:
            if ext in [".docx", ".doc"]:
                # Word: ExportAsFixedFormat(OutputFileName, ExportFormat); wdExportFormatPDF=17
                w_in = _ps_single_quoted(str(path_obj))
                w_out = _ps_single_quoted(output_pdf)
                ps_script = f"""
                $word = $null
                $doc = $null
                try {{
                    $word = New-Object -ComObject Word.Application
                    $word.Visible = $false
                    $word.DisplayAlerts = 0
                    $in = '{w_in}'
                    $out = '{w_out}'
                    $doc = $word.Documents.Open($in, $false, $false, $false)
                    try {{
                        $doc.ExportAsFixedFormat($out, 17)
                    }} catch {{
                        $doc.SaveAs2($out, 17)
                    }}
                    $doc.Close($false)
                    $doc = $null
                    $word.Quit()
                    $word = $null
                    Write-Output "SUCCESS"
                }} catch {{
                    Write-Error $_.Exception.Message
                }} finally {{
                    if ($null -ne $doc) {{ try {{ $doc.Close($false) }} catch {{}} }}
                    if ($null -ne $word) {{ try {{ $word.Quit() }} catch {{}} }}
                }}
                """
                engine = "Word-PDF"
            elif ext in [".xlsx", ".xls", ".xlsm", ".csv"]:
                e_in = _ps_single_quoted(str(path_obj))
                e_out = _ps_single_quoted(output_pdf)
                ps_script = f"""
                $xl = $null
                $wb = $null
                try {{
                    $xl = New-Object -ComObject Excel.Application
                    $xl.Visible = $false
                    $xl.DisplayAlerts = $false
                    $in = '{e_in}'
                    $out = '{e_out}'
                    $wb = $xl.Workbooks.Open($in)
                    $wb.ExportAsFixedFormat(0, $out)
                    $wb.Close($false)
                    $wb = $null
                    $xl.Quit()
                    $xl = $null
                    Write-Output "SUCCESS"
                }} catch {{
                    Write-Error $_.Exception.Message
                }} finally {{
                    if ($null -ne $wb) {{ try {{ $wb.Close($false) }} catch {{}} }}
                    if ($null -ne $xl) {{ try {{ $xl.Quit() }} catch {{}} }}
                }}
                """
                engine = "Excel-PDF"
            else:
                return None, ""

            result = await self._run_powershell(ps_script, engine)
            if os.path.exists(output_pdf) and os.path.getsize(output_pdf) > 0:
                return output_pdf, ""
            detail = (result or "").strip()[:500] or "COM finished but no PDF file."
            logger.error(
                "%s did not produce a valid PDF. PowerShell result: %s",
                engine,
                (result or "")[:800],
            )
            return None, detail
        except Exception as e:
            logger.error("Native conversion error: %s", e)
            return None, str(e)[:300]

    async def _convert_via_libreoffice(
        self, path_obj: Path, output_pdf: str
    ) -> Tuple[Optional[str], str]:
        exe = _find_soffice_executable()
        if not exe:
            return None, "Install LibreOffice (soffice) for conversion without Word, or fix Office COM."

        src = str(path_obj.resolve())
        outdir = str(path_obj.parent.resolve())
        target_pdf = Path(output_pdf).resolve()
        try:
            if os.path.exists(output_pdf):
                os.remove(output_pdf)
        except OSError as e:
            return None, f"Cannot replace PDF: {e}"

        cmd = [
            exe,
            "--headless",
            "--norestore",
            "--nologo",
            "--convert-to",
            "pdf",
            "--outdir",
            outdir,
            src,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=120
            )
        except asyncio.TimeoutError:
            return None, "LibreOffice conversion timed out."
        except Exception as e:
            return None, f"LibreOffice: {e}"[:300]

        # Some soffice builds write to cwd; move PDF beside the source if needed
        cwd_candidate = Path.cwd() / f"{path_obj.stem}.pdf"
        try:
            if (
                cwd_candidate.is_file()
                and cwd_candidate.resolve() != target_pdf
                and cwd_candidate.stat().st_size > 0
            ):
                if target_pdf.exists():
                    target_pdf.unlink()
                shutil.move(str(cwd_candidate), str(target_pdf))
        except OSError as e:
            logger.warning("LibreOffice cwd PDF relocate: %s", e)

        if os.path.exists(output_pdf) and os.path.getsize(output_pdf) > 0:
            return output_pdf, ""

        err = (stderr or b"").decode(errors="replace").strip()
        out = (stdout or b"").decode(errors="replace").strip()
        tail = (err or out)[:400] or f"exit {process.returncode}"
        logger.error("LibreOffice PDF failed: %s", tail)
        return None, tail

    async def convert_office_to_pdf(self, file_path: str) -> Tuple[Optional[str], str]:
        """
        Convert a document to a PDF beside the source (same name, .pdf) when needed.
        Skips if the file is already PDF or a valid sibling PDF already exists.
        On Windows, tries Word/Excel COM for supported types, then LibreOffice.
        """
        path_obj = Path(file_path).resolve()
        if not path_obj.is_file() or path_obj.stat().st_size <= 0:
            return None, "Original file not found or is empty."
        ext = path_obj.suffix.lower()
        output_pdf = _pdf_output_path(path_obj)
        if ext == ".pdf":
            return str(path_obj), ""
        if ext not in CONVERT_TO_PDF_EXTENSIONS:
            return None, f"Cannot convert {ext} to PDF. Use Word, Excel, PPT, OpenDocument, text, or HTML formats."

        existing = _existing_sibling_pdf(path_obj)
        if existing:
            return existing, ""

        com_detail = ""
        com_supported = (ext in {".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv"})
        if os.name == "nt" and com_supported:
            pdf, com_detail = await self._convert_via_office_com(
                path_obj, output_pdf, ext
            )
            if pdf:
                return pdf, ""

        pdf_lo, lo_detail = await self._convert_via_libreoffice(path_obj, output_pdf)
        if pdf_lo:
            return pdf_lo, ""

        parts = []
        if os.name == "nt" and com_detail:
            parts.append(f"Office: {com_detail}")
        if lo_detail:
            parts.append(f"LibreOffice: {lo_detail}")
        msg = " ".join(parts).strip()[:450]
        return None, msg or "Conversion failed."

    async def convert_to_pdf_win(self, file_path: str) -> Optional[str]:
        """Backward-compatible: returns PDF path only, or None."""
        path, _ = await self.convert_office_to_pdf(file_path)
        return path

    async def _lp_print(
        self,
        file_path: str,
        selected_printer: Optional[str],
        copies: int,
        p_range: Optional[str],
    ) -> str:
        path_obj = Path(file_path)
        cmd = ["lp"]
        if selected_printer:
            cmd.extend(["-d", selected_printer])
        if copies > 1:
            cmd.extend(["-n", str(copies)])
        if p_range and re.match(r"^\d+(-\d+)?$", p_range):
            cmd.extend(["-o", f"page-ranges={p_range}"])
        cmd.append(str(path_obj))
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode != 0:
            return f"❌ Print failed: {stderr.decode()}"
        return f"✅ Printed: {path_obj.name}"

    async def print_file(
        self, file_path: str, settings: Dict, page_range: Optional[str] = None
    ) -> str:
        """Print: use existing or converted PDF for convertible types, else native/lp."""
        try:
            path_obj = Path(file_path).resolve()
            if not path_obj.is_file() or path_obj.stat().st_size <= 0:
                return f"❌ File not found or empty: {file_path}"
            ext = path_obj.suffix.lower()
            copies = min(settings.get("copies", 1), self.max_copies)
            selected_printer = settings.get("printer")
            p_range = (
                None
                if not page_range or page_range.lower() == "all"
                else page_range.strip()
            )

            if ext == ".pdf":
                if os.name == "nt":
                    return await self._print_pdf_windows(
                        str(path_obj), selected_printer, p_range, copies
                    )
                return await self._lp_print(
                    str(path_obj), selected_printer, copies, p_range
                )

            if ext in CONVERT_TO_PDF_EXTENSIONS:
                pdf_for_print = _existing_sibling_pdf(path_obj)
                conv_err = ""
                if not pdf_for_print:
                    pth, conv_err = await self.convert_office_to_pdf(str(path_obj))
                    pdf_for_print = pth
                if pdf_for_print:
                    if os.name == "nt":
                        r = await self._print_pdf_windows(
                            pdf_for_print, selected_printer, p_range, copies
                        )
                        return f"✅ {path_obj.name} printed (PDF): {r}"
                    return await self._lp_print(
                        pdf_for_print, selected_printer, copies, p_range
                    )
                if conv_err:
                    logger.warning(
                        "PDF not available, falling back to direct print: %s — %s",
                        path_obj.name,
                        conv_err,
                    )

            if os.name == "nt":
                if ext in [".docx", ".doc"]:
                    return await self._print_word_windows(
                        str(path_obj), selected_printer, p_range, copies
                    )
                elif ext in [".xlsx", ".xls", ".csv"]:
                    return await self._print_excel_windows(
                        str(path_obj), selected_printer, p_range, copies
                    )
                elif ext in [".png", ".jpg", ".jpeg", ".bmp"]:
                    return await self._print_image_windows(
                        str(path_obj), selected_printer
                    )
                else:
                    return await self._print_generic_windows(
                        str(path_obj), selected_printer, p_range, copies
                    )

            return await self._lp_print(
                str(path_obj), selected_printer, copies, p_range
            )

        except Exception as e:
            logger.error(f"Print error: {e}")
            return f"❌ Print error: {e}"

    async def _print_pdf_windows(
        self,
        file_path: str,
        printer: Optional[str],
        p_range: Optional[str],
        copies: int,
    ) -> str:
        """Prints PDF using SumatraPDF if available, else Edge."""
        sumatra_path = Path("bin/SumatraPDF.exe").resolve()

        if sumatra_path.exists():
            # SILENT SUMATRA PRINT
            cmd_printer = f'-print-to "{printer}"' if printer else "-print-to-default"
            cmd_range = f'-print-settings "{p_range}"' if p_range else ""

            ps_cmd = (
                f'& "{sumatra_path}" -silent {cmd_printer} {cmd_range} "{file_path}"'
            )
            return await self._run_powershell(ps_cmd, "PDF (Sumatra)")
        else:
            # FALLBACK TO EDGE (Requires Default Swap for reliability)
            logger.warning(
                "SumatraPDF not found in bin/, falling back to Edge with System Swap."
            )
            ps_script = f"""
            $oldP = (Get-CimInstance Win32_Printer | Where-Object {{ $_.Default -eq $true }}).Name
            $target = "{printer}"
            try {{
                if ($target) {{
                    $p = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $target -or $_.Name -like "*$target*" }} | Select-Object -First 1
                    if ($p) {{ $p | Invoke-CimMethod -MethodName SetDefaultPrinter }}
                }}
                Start-Process -FilePath "{file_path}" -Verb Print -WindowStyle Hidden
                Start-Sleep -Seconds 2
            }} finally {{
                if ($oldP) {{
                    $rest = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $oldP }}
                    if ($rest) {{ $rest | Invoke-CimMethod -MethodName SetDefaultPrinter }}
                }}
            }}
            """
            return await self._run_powershell(ps_script, "PDF (Shell-Swap)")

    async def _print_word_windows(
        self,
        file_path: str,
        printer: Optional[str],
        p_range: Optional[str],
        copies: int,
    ) -> str:
        """Prints Word docs using Precision Targeting (Printer Name + Port)."""
        # Range handling logic for Word
        if p_range and "-" in p_range:
            p_from, p_to = p_range.split("-")[0], p_range.split("-")[1]
            print_cmd = f'$doc.PrintOut($false, $false, 3, $null, "{p_from}", "{p_to}", $null, {copies})'
        else:
            print_cmd = f"$doc.PrintOut($false, $false, 0, $null, $null, $null, $null, {copies})"

        ps_script = f"""
        $target = "{printer}"
        try {{
            $word = New-Object -ComObject Word.Application
            $word.Visible = $false
            
            # Precision Targeting: Find the exact Name + Port string Word requires
            if ($target) {{
                $p = Get-CimInstance Win32_Printer | Where-Object {{ 
                    ($_.Name -eq $target -or $_.Name -like "*$target*") -and 
                    ($_.Name -notlike "*OneNote*" -and $_.Name -notlike "*Fax*" -and $_.Name -notlike "*PDF*")
                }} | Select-Object -First 1
                
                if ($p) {{
                    $word.ActivePrinter = "$($p.Name) on $($p.PortName)"
                }}
            }}
            
            $doc = $word.Documents.Open("{file_path}", $false, $true)
            {print_cmd}
            
            # Wait for spooler to receive document
            while($word.BackgroundPrintingStatus -gt 0) {{ Start-Sleep -Milliseconds 250 }}
            
            $doc.Close($false)
            $word.Quit()
            Write-Output "SUCCESS"
        }} catch {{
            Write-Error $_.Exception.Message
            if($word) {{ $word.Quit() }}
        }}
        """
        return await self._run_powershell(ps_script, "Word (Precision)")

    async def _print_excel_windows(
        self,
        file_path: str,
        printer: Optional[str],
        p_range: Optional[str],
        copies: int,
    ) -> str:
        """Prints Excel docs using Precision Targeting."""
        # Range handling logic for Excel
        if p_range and "-" in p_range:
            p_from, p_to = p_range.split("-")[0], p_range.split("-")[1]
            print_cmd = f"$wb.PrintOut({p_from}, {p_to}, {copies}, $false)"
        else:
            print_cmd = f"$wb.PrintOut($null, $null, {copies}, $false)"

        ps_script = f"""
        $target = "{printer}"
        try {{
            $xl = New-Object -ComObject Excel.Application
            $xl.Visible = $false
            $xl.DisplayAlerts = $false
            
            # Precision Targeting for Excel
            if ($target) {{
                $p = Get-CimInstance Win32_Printer | Where-Object {{ 
                    ($_.Name -eq $target -or $_.Name -like "*$target*") -and 
                    ($_.Name -notlike "*OneNote*" -and $_.Name -notlike "*Fax*" -and $_.Name -notlike "*PDF*")
                }} | Select-Object -First 1
                
                if ($p) {{
                    $xl.ActivePrinter = "$($p.Name) on $($p.PortName)"
                }}
            }}

            $wb = $xl.Workbooks.Open("{file_path}")
            {print_cmd}
            
            # Synchronous wait buffer
            Start-Sleep -Seconds 2
            
            $wb.Close($false)
            $xl.Quit()
            Write-Output "SUCCESS"
        }} catch {{
            Write-Error $_.Exception.Message
            if($xl) {{ $xl.Quit() }}
        }}
        """
        return await self._run_powershell(ps_script, "Excel (Precision)")

    async def _print_image_windows(self, file_path: str, printer: Optional[str]) -> str:
        """Prints images using Precision Targeting + .NET PrintDocument."""
        ps_script = f"""
        $target = "{printer}"
        try {{
            Add-Type -AssemblyName System.Drawing
            $file = "{file_path}"
            $pd = New-Object System.Drawing.Printing.PrintDocument
            
            # Precision Targeting for Images
            if ($target) {{
                $p = Get-CimInstance Win32_Printer | Where-Object {{ 
                    ($_.Name -eq $target -or $_.Name -like "*$target*") -and 
                    ($_.Name -notlike "*OneNote*" -and $_.Name -notlike "*Fax*" -and $_.Name -notlike "*PDF*")
                }} | Select-Object -First 1
                
                if ($p) {{
                    $pd.PrinterSettings.PrinterName = $p.Name
                }}
            }}

            $pd.DocumentName = (Split-Path $file -Leaf)
            $image = [System.Drawing.Image]::FromFile($file)
            $pd.add_PrintPage({{
                $rect = $_.MarginBounds
                if ($image.Width / $image.Height -gt $rect.Width / $rect.Height) {{
                    $rect.Height = $image.Height * ($rect.Width / $image.Width)
                }} else {{
                    $rect.Width = $image.Width * ($rect.Height / $image.Height)
                }}
                $_.Graphics.DrawImage($image, $rect)
            }})
            $pd.Print()
            $image.Dispose()
            Write-Output "SUCCESS"
        }} catch {{
            Write-Error $_.Exception.Message
            if ($image) {{ $image.Dispose() }}
        }}
        """
        return await self._run_powershell(ps_script, "Image (Precision)")

    async def _print_generic_windows(
        self,
        file_path: str,
        printer: Optional[str],
        p_range: Optional[str],
        copies: int,
    ) -> str:
        """Fallback for images, txt, etc."""
        # Note: standard shell print doesn't support ranges easily
        escaped_path = file_path.replace("'", "''")
        if printer:
            escaped_printer = printer.replace("'", "''")
            ps_cmd = (
                f"$n = '{escaped_printer}'; "
                f'$p = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $n -or $_.Name -like "*$n*" }}; '
                f"if ($p) {{ $p | Invoke-CimMethod -MethodName SetDefaultPrinter }}; "
                f"Start-Process -FilePath '{escaped_path}' -Verb Print -WindowStyle Hidden"
            )
        else:
            ps_cmd = f"Start-Process -FilePath '{escaped_path}' -Verb Print -WindowStyle Hidden"

        return await self._run_powershell(ps_cmd, "Shell (Generic)")

    async def _run_powershell(self, command: str, engine_name: str) -> str:
        """Helper to run powershell commands safely."""
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)

            if process.returncode == 0:
                logger.info(f"{engine_name} print successful")
                return f"✅ Print successful via {engine_name}"
            else:
                err = stderr.decode().strip() or stdout.decode().strip()
                logger.error(f"{engine_name} print failed: {err}")
                return f"❌ {engine_name} Error: {err[:100]}"
        except Exception as e:
            return f"❌ {engine_name} System Error: {e}"


# Singleton
print_manager = PrintManager()
