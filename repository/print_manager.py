import subprocess
import os
import re
import shlex
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Dict
from .config import config

logger = logging.getLogger(__name__)

class PrintManager:
    def __init__(self):
        self.max_copies = config.max_copies

    async def get_available_printers(self) -> List[str]:
        """Fetch available printers asynchronously."""
        try:
            if os.name == 'nt':
                # Windows - Try PowerShell first (more reliable on modern Win10/11)
                ps_cmd = ['powershell', '-NoProfile', '-Command', 'Get-CimInstance Win32_Printer | Select-Object -ExpandProperty Name']
                process = await asyncio.create_subprocess_exec(
                    *ps_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await process.communicate()
                output = stdout.decode().strip()
                if output:
                    return [p.strip() for p in output.split('\r\n') if p.strip()]
                
                # Fallback to wmic
                cmd = ['wmic', 'printer', 'get', 'name']
            else:
                # Linux - Using standard lpstat
                cmd = ['lpstat', '-e']

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"Failed to get printers: {stderr.decode()}")
                return []

            output = stdout.decode().strip()
            if os.name == 'nt':
                return [p.strip() for p in output.split('\n')[1:] if p.strip()]
            else:
                return [p.strip() for p in output.split('\n') if p.strip()]
        except Exception as e:
            logger.error(f"Printer discovery error: {e}")
            return ["Default Printer"]

    async def get_print_queue(self) -> List[str]:
        """Fetch current print queue with structured IDs."""
        try:
            if os.name == 'nt':
                # Windows - Get JobID and Document name
                cmd = ['wmic', 'printjob', 'get', 'jobid,document']
            else:
                # Linux - Get basic job info
                cmd = ['lpstat', '-o']

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            output = stdout.decode().strip()
            
            if not output:
                return []

            jobs = []
            lines = [l.strip() for l in output.split('\n') if l.strip()]
            
            if os.name == 'nt':
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
            if os.name == 'nt':
                # Sanitize: job_id should be numeric
                numeric_id = "".join(filter(str.isdigit, job_id))
                if not numeric_id:
                    return False
                # Use wmic to delete the job
                cmd = ['wmic', 'printjob', 'where', f'jobid={numeric_id}', 'delete']
            else:
                # Linux - Use cancel command
                # job_id is usually 'printer-123'
                cmd = ['cancel', job_id]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
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
            if os.name == 'nt':
                numeric_id = "".join(filter(str.isdigit, job_id))
                if not numeric_id:
                    return False
                cmd = ['wmic', 'printjob', 'where', f'jobid={numeric_id}', 'call', 'resume']
            else:
                cmd = ['lp', '-i', job_id, '-H', 'resume']

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
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

    async def convert_to_pdf(self, file_path: str) -> Optional[str]:
        """Convert document to PDF using LibreOffice headlessly."""
        try:
            file_path_obj = Path(file_path).resolve()
            output_dir = str(file_path_obj.parent)
            output_pdf = str(file_path_obj.with_suffix('.pdf'))
            
            # SAFE: No shell=True, direct argument list
            cmd = [
                'libreoffice', '--headless', '--convert-to', 'pdf',
                '--outdir', output_dir, str(file_path_obj)
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
            
            if process.returncode == 0 and os.path.exists(output_pdf):
                return output_pdf
            else:
                logger.error(f"Conversion failed: {stderr.decode()}")
                return None
        except asyncio.TimeoutError:
            logger.error("PDF conversion timed out after 120s")
            return None
        except Exception as e:
            logger.error(f"Conversion error: {e}")
            return None

    async def print_file(self, file_path: str, settings: Dict, page_range: Optional[str] = None) -> str:
        """Execute print command with specialized engines for Word, Excel, and PDF."""
        try:
            path_obj = Path(file_path).resolve()
            if not path_obj.exists():
                return f"❌ File not found: {path_obj.name}"

            copies = min(settings.get("copies", 1), self.max_copies)
            selected_printer = settings.get("printer")
            # Normalize range: empty or "all" -> None (print all)
            p_range = None if not page_range or page_range.lower() == "all" else page_range.strip()

            if os.name == 'nt':
                ext = path_obj.suffix.lower()
                
                # SPECIALIZED ENGINES FOR WINDOWS
                if ext == ".pdf":
                    return await self._print_pdf_windows(str(path_obj), selected_printer, p_range, copies)
                elif ext in [".docx", ".doc"]:
                    return await self._print_word_windows(str(path_obj), selected_printer, p_range, copies)
                elif ext in [".xlsx", ".xls"]:
                    return await self._print_excel_windows(str(path_obj), selected_printer, p_range, copies)
                elif ext in [".png", ".jpg", ".jpeg", ".bmp"]:
                    return await self._print_image_windows(str(path_obj), selected_printer)
                else:
                    return await self._print_generic_windows(str(path_obj), selected_printer, p_range, copies)

            # LINUX LOGIC (Standard LP)
            else:
                cmd = ['lp']
                if selected_printer:
                    cmd.extend(['-d', selected_printer])
                if copies > 1:
                    cmd.extend(['-n', str(copies)])
                if p_range and re.match(r'^\d+(-\d+)?$', p_range):
                    cmd.extend(['-o', f'page-ranges={p_range}'])
                
                cmd.append(str(path_obj))
                process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
                
                if process.returncode != 0:
                    return f"❌ Print failed: {stderr.decode()}"
                return f"✅ Printed: {path_obj.name}"

        except Exception as e:
            logger.error(f"Print error: {e}")
            return f"❌ Print error: {e}"

    async def _print_pdf_windows(self, file_path: str, printer: Optional[str], p_range: Optional[str], copies: int) -> str:
        """Prints PDF using SumatraPDF if available, else Edge."""
        sumatra_path = Path("bin/SumatraPDF.exe").resolve()
        
        if sumatra_path.exists():
            # SILENT SUMATRA PRINT
            cmd_printer = f'-print-to "{printer}"' if printer else '-print-to-default'
            cmd_range = f'-print-settings "{p_range}"' if p_range else ''
            
            ps_cmd = f'& "{sumatra_path}" -silent {cmd_printer} {cmd_range} "{file_path}"'
            return await self._run_powershell(ps_cmd, "PDF (Sumatra)")
        else:
            # FALLBACK TO EDGE (Requires Default Swap for reliability)
            logger.warning("SumatraPDF not found in bin/, falling back to Edge with System Swap.")
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

    async def _print_word_windows(self, file_path: str, printer: Optional[str], p_range: Optional[str], copies: int) -> str:
        """Prints Word docs using 'System Swap' logic for rock-solid reliability."""
        # Range handling logic for Word
        if p_range and '-' in p_range:
            p_from, p_to = p_range.split('-')[0], p_range.split('-')[1]
            print_cmd = f'$doc.PrintOut($false, $false, 3, $null, "{p_from}", "{p_to}", $null, {copies})'
        else:
            print_cmd = f'$doc.PrintOut($false, $false, 0, $null, $null, $null, $null, {copies})'

        ps_script = f"""
        $oldP = (Get-CimInstance Win32_Printer | Where-Object {{ $_.Default -eq $true }}).Name
        $target = "{printer}"
        try {{
            if ($target) {{
                $p = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $target -or $_.Name -like "*$target*" }} | Select-Object -First 1
                if ($p) {{ $p | Invoke-CimMethod -MethodName SetDefaultPrinter }}
            }}
            
            $word = New-Object -ComObject Word.Application
            $word.Visible = $false
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
        }} finally {{
            if ($oldP) {{
                $rest = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $oldP }}
                if ($rest) {{ $rest | Invoke-CimMethod -MethodName SetDefaultPrinter }}
            }}
        }}
        """
        return await self._run_powershell(ps_script, "Word (Direct-Swap)")

    async def _print_excel_windows(self, file_path: str, printer: Optional[str], p_range: Optional[str], copies: int) -> str:
        """Prints Excel docs using 'System Swap' logic."""
        # Range handling logic for Excel
        if p_range and '-' in p_range:
            p_from, p_to = p_range.split('-')[0], p_range.split('-')[1]
            print_cmd = f'$wb.PrintOut({p_from}, {p_to}, {copies}, $false)'
        else:
            print_cmd = f'$wb.PrintOut($null, $null, {copies}, $false)'

        ps_script = f"""
        $oldP = (Get-CimInstance Win32_Printer | Where-Object {{ $_.Default -eq $true }}).Name
        $target = "{printer}"
        try {{
            if ($target) {{
                $p = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $target -or $_.Name -like "*$target*" }} | Select-Object -First 1
                if ($p) {{ $p | Invoke-CimMethod -MethodName SetDefaultPrinter }}
            }}
            
            $xl = New-Object -ComObject Excel.Application
            $xl.Visible = $false
            $xl.DisplayAlerts = $false
            $wb = $xl.Workbooks.Open("{file_path}")
            {print_cmd}
            
            # Excel doesn't have BackgroundPrintingStatus, but the $false flag above
            # makes PrintOut synchronous. We add a small buffer for safety.
            Start-Sleep -Seconds 1
            
            $wb.Close($false)
            $xl.Quit()
            Write-Output "SUCCESS"
        }} catch {{
            Write-Error $_.Exception.Message
            if($xl) {{ $xl.Quit() }}
        }} finally {{
            if ($oldP) {{
                $rest = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $oldP }}
                if ($rest) {{ $rest | Invoke-CimMethod -MethodName SetDefaultPrinter }}
            }}
        }}
        """
        return await self._run_powershell(ps_script, "Excel (Direct-Swap)")

    async def _print_image_windows(self, file_path: str, printer: Optional[str]) -> str:
        """Prints images using 'System Swap' logic + .NET PrintDocument."""
        ps_script = f"""
        $oldP = (Get-CimInstance Win32_Printer | Where-Object {{ $_.Default -eq $true }}).Name
        $target = "{printer}"
        try {{
            if ($target) {{
                $p = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $target -or $_.Name -like "*$target*" }} | Select-Object -First 1
                if ($p) {{ $p | Invoke-CimMethod -MethodName SetDefaultPrinter }}
            }}
            
            Add-Type -AssemblyName System.Drawing
            $file = "{file_path}"
            $pd = New-Object System.Drawing.Printing.PrintDocument
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
        }} finally {{
            if ($oldP) {{
                $rest = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $oldP }}
                if ($rest) {{ $rest | Invoke-CimMethod -MethodName SetDefaultPrinter }}
            }}
        }}
        """
        return await self._run_powershell(ps_script, "Image (Direct-Swap)")

    async def _print_generic_windows(self, file_path: str, printer: Optional[str], p_range: Optional[str], copies: int) -> str:
        """Fallback for images, txt, etc."""
        # Note: standard shell print doesn't support ranges easily
        escaped_path = file_path.replace("'", "''")
        if printer:
            escaped_printer = printer.replace("'", "''")
            ps_cmd = (
                f"$n = '{escaped_printer}'; "
                f"$p = Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq $n -or $_.Name -like \"*$n*\" }}; "
                f"if ($p) {{ $p | Invoke-CimMethod -MethodName SetDefaultPrinter }}; "
                f"Start-Process -FilePath '{escaped_path}' -Verb Print -WindowStyle Hidden"
            )
        else:
            ps_cmd = f"Start-Process -FilePath '{escaped_path}' -Verb Print -WindowStyle Hidden"
        
        return await self._run_powershell(ps_cmd, "Shell (Generic)")

    async def _run_powershell(self, command: str, engine_name: str) -> str:
        """Helper to run powershell commands safely."""
        try:
            cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command]
            process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
